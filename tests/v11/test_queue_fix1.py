"""Q2a review regressions using sealed studies and an offline fake transport."""

import asyncio
import shutil
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.live_review import _labels, export_live_review
from swarm_auth_bench.peer_reporting_v11.live_runtime import run_live_observer
from swarm_auth_bench.peer_reporting_v11.queue_checks import smoke_episode
from swarm_auth_bench.peer_reporting_v11.queue_evidence import (
    eligibility_inputs,
    export_fields,
    verify_result,
)
from swarm_auth_bench.peer_reporting_v11.queue_protocol import QUEUE_OPTIONS, caps_record
from swarm_auth_bench.peer_reporting_v11.queue_storage import atomic_json
from swarm_auth_bench.peer_reporting_v11.queue_study import order_rows, verify_order
from swarm_auth_bench.peer_reporting_v11.queue_templates import load_template
from swarm_auth_bench.peer_reporting_v11.queue_text import build_instructions
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_Q

from .live_fakes import FakeTransport, authorization, run_phase
from .receipt_helpers import commit_receipt
from .test_queue_live import QueueHarness, response, sample, steps
from .test_queue_live import study as study


def files(directory):
    return {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def selection(study, phase="screening"):
    rows, fixtures, source = live.load_study(study, phase)
    return rows, fixtures, source, live.read_study_manifest(study)


def build(study, *, phase="screening", rows=None, ledger=None, receipts=None):
    canonical, fixtures, source, manifest = selection(study, phase)
    return live.build_assignment_plan(phase, canonical if rows is None else rows, fixtures, caps_record(),
        revision="queue-population-check", source=source, gate_evidence={}, study_manifest=manifest,
        study_directory=study, consumed_attempts=ledger, receipt_directory=receipts)


def omit(rows, whole_group):
    target = next(row for row in rows if row["template_id"] == "stock-count" and row["block"] == 1
                  and row["model"] == "gpt-6-sol" and row["effort"] == "low" and row["pressure"] == "P3")
    return [row for row in rows if not (row["template_id"] == target["template_id"] and row["block"] == target["block"]
            and row["model"] == target["model"] and row["effort"] == target["effort"]
            and (whole_group or row["assignment_id"] == target["assignment_id"]))]


@pytest.mark.parametrize("whole_group", [False, True])
def test_missing_screening_population_refused_at_build_and_prepare(tmp_path, study, whole_group):
    rows, fixtures, source, manifest = selection(study)
    incomplete = omit(rows, whole_group)
    before = files(study)
    with pytest.raises(ValueError, match="complete required phase population"):
        verify_order(order_rows(incomplete, manifest["protocol"]["seeds"]["calibration"], "screening"),
                     manifest["protocol"]["seeds"]["calibration"], "screening", required_rows=rows)
    with pytest.raises(ValueError, match="complete required phase population"):
        build(study, rows=incomplete)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    entries = live.validate_assignment_rows("screening", incomplete, fixtures, caps_record(), bundle)
    unchecked = live._assemble("screening", "incomplete-check", caps_record(), bundle, entries, fixtures,
                                source=source, gate_evidence={}, consumed_attempts=live._empty_ledger())
    with pytest.raises(ValueError, match="complete required phase population"):
        live.prepare_live_root(study / "roots/incomplete-check", unchecked, study_directory=study)
    assert files(study) == before


def reseal_incomplete_root(root, whole_group):
    top = live.read_live_plan(root)
    old_hash = top["seal_hash"]
    all_entries = [entry for lane in top["lanes"] for entry in
                   read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"]]
    target = next(entry for entry in all_entries if entry["template_id"] == "stock-count" and entry["block"] == 1
                  and entry["model"] == "gpt-6-sol" and entry["reasoning_effort"] == "low" and entry["pressure"] == "P3")
    for lane in top["lanes"]:
        path = root / lane["path"] / "phase-plan.json"
        plan = read_sealed(path)
        entries = [entry for entry in plan["planned_order"] if not (
            entry["template_id"] == target["template_id"] and entry["block"] == target["block"]
            and entry["model"] == target["model"] and entry["reasoning_effort"] == target["reasoning_effort"]
            and (whole_group or entry["entry_id"] == target["entry_id"]))]
        plan["planned_order"] = [{**entry, "planned_index": index} for index, entry in enumerate(entries)]
        plan["maximum_live_calls"] = lane["planned_calls"] = len(entries)
        plan["caps"] = lane_caps(top["caps"], top["phase"], len(entries))
        sealed = seal({key: value for key, value in plan.items() if key != "seal_hash"})
        atomic_json(path, sealed)
        lane["plan_hash"] = sealed["seal_hash"]
    top["calls_by_lane"] = {lane["lane_id"]: lane["planned_calls"] for lane in top["lanes"]}
    top["maximum_live_calls"] = sum(top["calls_by_lane"].values())
    top = seal({key: value for key, value in top.items() if key != "seal_hash"})
    atomic_json(root / live.LIVE_PLAN_FILE, top)
    study_directory = root.parent.parent
    for relative in (live.STUDY_REGISTRY, live.STUDY_REGISTRY + "/" + live.FINALIZED_DIRECTORY):
        path = study_directory / relative / (old_hash + ".json")
        record = read_sealed(path)
        record["plan_hash"] = top["seal_hash"]
        if "live_plan_hash" in record:
            record["live_plan_hash"] = top["seal_hash"]
        atomic_json(path.with_name(top["seal_hash"] + ".json"), seal({key: value for key, value in record.items() if key != "seal_hash"}))
        path.unlink()


@pytest.mark.parametrize("whole_group", [False, True])
def test_missing_population_refused_at_verify_export_and_successor(tmp_path, study, whole_group):
    directory = tmp_path / "study"
    shutil.copytree(study, directory)
    root = directory / "roots/screening"
    live.prepare_live_root(root, build(directory), study_directory=directory)
    study_primary = {path: data for path, data in files(directory).items() if str(path).startswith("fixtures")
                     or str(path) == live.STUDY_MANIFEST}
    reseal_incomplete_root(root, whole_group)
    receipts = commit_receipt(root, tmp_path / "receipts", study_directory=directory)
    before = files(directory)
    kwargs = {"study_directory": directory, "receipt_directory": receipts}
    with pytest.raises(ValueError, match="complete required phase population"):
        live.verify_live_root(root, **kwargs)
    with pytest.raises(ValueError, match="complete required phase population"):
        export_live_review(root, tmp_path / "export", **kwargs)
    with pytest.raises(ValueError, match="complete required phase population"):
        live.prior_root_ledger([root], phase="screening", source=live.read_live_plan(root)["source"],
                              bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q), **kwargs)
    assert not (tmp_path / "export").exists()
    assert files(directory) == before
    assert all(files(directory)[path] == data for path, data in study_primary.items())


def test_compatibility_population_is_complete_at_prepare(tmp_path):
    built = live.build_compatibility_plan(caps_record(), revision="compat-population", tool_schema_version=TOOL_SCHEMA_VERSION_Q)
    top, lanes, _ = built
    removed = top["lanes"].pop()
    del lanes[removed["lane_id"]]
    del top["calls_by_lane"][removed["lane_id"]]
    top["maximum_live_calls"] -= 1
    with pytest.raises(ValueError, match="complete required phase population"):
        live.prepare_live_root(tmp_path / "compat", built)
    assert not (tmp_path / "compat").exists()


def test_complete_population_still_requires_group_order_at_build_and_prepare(study):
    rows, _, _, _ = selection(study)
    reordered = deepcopy(rows)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    before = files(study)
    with pytest.raises(ValueError, match="consecutively"):
        build(study, rows=reordered)
    top, lanes, fixtures = build(study)
    lane = next(lane for lane in top["lanes"] if lane["lane_id"] == "gpt-6-sol-low")
    lane_plan = lanes[lane["lane_id"]]
    lane_plan["planned_order"][0], lane_plan["planned_order"][1] = lane_plan["planned_order"][1], lane_plan["planned_order"][0]
    for index, entry in enumerate(lane_plan["planned_order"]):
        entry["planned_index"] = index
    lane["plan_hash"] = seal(lane_plan)["seal_hash"]
    with pytest.raises(ValueError, match="consecutive order"):
        live.prepare_live_root(study / "roots/reordered", (top, lanes, fixtures), study_directory=study)
    assert files(study) == before


async def test_valid_successor_retains_remaining_group_and_checks_primary_starts(tmp_path, study, monkeypatch):
    directory = tmp_path / "study"
    shutil.copytree(study, directory)
    root = directory / "roots/screening"
    live.prepare_live_root(root, build(directory), study_directory=directory)
    top = live.read_live_plan(root)
    # The full Q chain test covers gates. This test isolates the prior-start population.
    monkeypatch.setattr(live, "check_phase_gates", lambda *a, **k: {"passed": True, "evidence": {}, "failures": []})

    def script_for(model, effort):
        return lambda fixture: [("call", lambda: (root / "STOP").write_bytes(b"offline stop\n")), *steps(fixture)]

    harness = QueueHarness(tmp_path / "homes", script_for)
    await run_phase(root, top, harness, auth=authorization(top, deadline=12000),
                    bundle=load_bundle(), study_directory=directory)
    receipts = commit_receipt(root, tmp_path / "receipts", study_directory=directory)
    rows, _, source, _ = selection(directory)
    ledger = live.prior_root_ledger([root], phase="screening", source=source, study_directory=directory,
                                   bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q), receipt_directory=receipts)
    assert ledger["consumed_attempt_ids"]
    ledger["excluded_assignment_ids"] = sorted(row["assignment_id"] for row in rows
                                               if row["assignment_id"] + live.ATTEMPT_SUFFIX in ledger["consumed_attempt_ids"])
    remaining = [row for row in rows if row["assignment_id"] not in ledger["excluded_assignment_ids"]]
    assert len([row for row in remaining if row["model"] == "gpt-6-sol" and row["effort"] == "low"
                and row["template_id"] == "stock-count" and row["block"] == 1]) == 1
    successor = build(directory, rows=remaining, ledger=ledger, receipts=receipts)
    next_root = directory / "roots/successor"
    live.prepare_live_root(next_root, successor, study_directory=directory, prior_roots=[root], receipt_directory=receipts)
    report = live.verify_live_root(next_root, study_directory=directory, prior_roots=[root], receipt_directory=receipts)
    assert report["live_model_call_starts"] == 0
    assert successor[0]["maximum_live_calls"] + len(ledger["consumed_attempt_ids"]) == 40
    forged = deepcopy(ledger)
    removed = remaining[0]
    forged["prior_roots"][0]["consumed_attempt_ids"] = sorted([*forged["prior_roots"][0]["consumed_attempt_ids"],
                                                             removed["assignment_id"] + live.ATTEMPT_SUFFIX])
    forged["consumed_attempt_ids"] = forged["prior_roots"][0]["consumed_attempt_ids"]
    forged["excluded_assignment_ids"] = sorted([*forged["excluded_assignment_ids"], removed["assignment_id"]])
    before = files(directory)
    with pytest.raises(ValueError, match="verified primary starts"):
        build(directory, rows=remaining[1:], ledger=forged, receipts=receipts)
    assert files(directory) == before


async def observe(tmp_path, script, *, caps=None, runtime_class=FakeTransport):
    fixture = sample(queue_length=4, block=0)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    caps = caps or lane_caps(caps_record(), "smoke", 1)
    home = tmp_path / "home"
    home.mkdir()
    runtime = runtime_class("gpt-6-sol", "low", script, home, options=QUEUE_OPTIONS)
    preflight = await live.manifest_preflight(runtime, runtime.model, caps, reasoning_effort="low", bundle=bundle,
                                             version_reader=lambda _: asyncio.sleep(0, result="codex-cli 0.158.0"))
    directory = tmp_path / "attempt"
    result = await run_live_observer(fixture, build_instructions(load_template("ledger-close")), directory=directory,
        attempt_id="queue-fix1-check", requested_model=runtime.model, reasoning_effort="low", world_mode="normal",
        bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    payload = {"observer_result": result, "fixture_hash": content_hash(fixture),
               **{key: result[key] for key in ("reasoning_items", "runtime_recording") if key in result}}
    verify_result(payload, directory, bundle)
    return result, fixture, runtime


@pytest.mark.parametrize("field", ["input", "output"])
@pytest.mark.parametrize("missing", ["opposite", "earlier", "both"])
async def test_independent_token_stops_preserve_missing_usage_and_deduplication(tmp_path, field, missing):
    caps = lane_caps(caps_record(), "smoke", 1)
    cap = caps["trial_" + field + "_token_stop"]
    counts = {field + "_tokens": cap // 2}
    first, second = response(1, **counts), response(2, **counts)
    if missing in ("opposite", "both"):
        opposite = "outputTokens" if field == "input" else "inputTokens"
        first[1]["usage"].pop(opposite)
        second[1]["usage"].pop(opposite)
    earlier = [("response", {"responseId": "response-missing", "model": "gpt-6-sol"})] if missing in ("earlier", "both") else []
    script = [*earlier, first, deepcopy(first), second, ("sleep", 1)]
    result, fixture, runtime = await observe(tmp_path, script, caps=caps)
    assert "turn/interrupt" in runtime.methods
    assert result["termination_kind"] == "per_trial_limit"
    assert result["boundary"]["reason"] == "trial_" + field + "_token_limit"
    assert eligibility_inputs(result, fixture)["cap_ended"]
    assert result["usage"]["total_tokens"] is None
    assert result["usage"]["availability"] == "unavailable_or_unreconciled"
    calls = result["runtime_recording"]["model_calls"]
    assert len(calls) == 2 + len(earlier)
    assert sum(call[field + "_tokens"] for call in calls if type(call[field + "_tokens"]) is int) == cap
    stops = eligibility_inputs(result, fixture)["token_stops"]
    assert stops[-1]["token_kind"] == field and stops[-1]["cumulative_tokens"] == cap
    assert not result["infrastructure_failures"]


async def test_wall_stop_after_finish_binds_smoke_and_excludes_cleanup_time(tmp_path):
    class SlowClose(FakeTransport):
        async def close(self):
            await asyncio.sleep(0.1)
            await super().close()

    caps = lane_caps(caps_record(), "smoke", 1)
    caps["max_trial_wall_seconds"] = 1
    result, fixture, runtime = await observe(tmp_path, [response(),
        ("tool", "agent_finish", {"reason": "completed", "summary": "Record processing ended."}),
        ("sleep", 2)], caps=caps, runtime_class=SlowClose)
    assert "turn/interrupt" in runtime.methods
    assert result["boundary"]["reason"] == "agent_finish"
    assert result["termination_kind"] == "per_trial_limit"
    assert eligibility_inputs(result, fixture)["cap_ended"]
    assert result["queue_reconciled"] and result["runtime_closed"] and not result["infrastructure_failures"]
    exported = export_fields(result, fixture)
    assert exported["generation_stops"][0]["reason"] == "trial_wall_limit"
    checked = smoke_episode(result, caps)
    assert checked["passed"] and checked["resources"]["caps_binding"]
    assert checked["resources"]["wall_seconds"] >= 1
    assert checked["resources"]["wall_seconds"] == exported["generation_elapsed_seconds"]
    assert checked["resources"]["total_wall_seconds"] > checked["resources"]["wall_seconds"] + 0.05


async def test_cleanup_after_wall_deadline_does_not_bind_a_finished_generation(tmp_path):
    class SlowClose(FakeTransport):
        async def close(self):
            await asyncio.sleep(1.1)
            await super().close()

    caps = lane_caps(caps_record(), "smoke", 1)
    caps["max_trial_wall_seconds"] = 1
    result, fixture, runtime = await observe(tmp_path, [response(),
        ("tool", "agent_finish", {"reason": "completed", "summary": "Record processing ended."}),
        ("sleep", 0.1), ("message", "Record processing completed.")], caps=caps, runtime_class=SlowClose)
    assert "turn/interrupt" not in runtime.methods
    assert result["termination_kind"] == "natural_end"
    assert not eligibility_inputs(result, fixture)["cap_ended"]
    checked = smoke_episode(result, caps)
    assert checked["passed"] and not checked["resources"]["caps_binding"]
    assert checked["resources"]["total_wall_seconds"] >= 1
    assert checked["resources"]["wall_seconds"] < 1
    assert checked["resources"]["wall_seconds"] > result["boundary"]["elapsed_seconds"]


@pytest.mark.parametrize("before,after", [(False, False), (True, False), (False, True), (True, True)])
async def test_compaction_flags_are_independent_against_focal_serving(tmp_path, before, after):
    fixture = sample(queue_length=4, block=0)
    script = steps(fixture, case="after" if after else None)
    if before:
        script.insert(2, ("compaction",))
    result, fixture, _ = await observe(tmp_path, script)
    inputs = eligibility_inputs(result, fixture)
    assert inputs["reached"]
    assert inputs["compaction_before_focal"] is before
    assert inputs["compaction_after_focal"] is after
    assert inputs["ineligibility_reason"] == ("context_compacted" if before else None)


def test_candidate_and_executed_memberships_are_separate(study):
    manifest = live.read_study_manifest(study)
    for row in manifest["assignments"]:
        assert row["candidate_arm_memberships"] == row["arm_memberships"]
        assert "executed_arm_memberships" not in row
    candidates = [row for row in manifest["assignments"] if "queue_attention" in row["candidate_arm_memberships"]]
    assert {(row["model"], row["effort"]) for row in candidates} == {
        ("gpt-6-sol", "low"), ("gpt-6-sol", "xhigh"), ("gpt-6-luna", "low"), ("gpt-6-luna", "xhigh")}
    for phase in ("smoke", "screening"):
        top, lanes, _ = build(study, phase=phase)
        for lane in top["lanes"]:
            for entry in lanes[lane["lane_id"]]["planned_order"]:
                labels = _labels(entry, phase, lane["lane_id"])
                assert labels["candidate_arm_memberships"] == entry["arm_memberships"]
                assert labels["executed_arm_memberships"] == [entry["arm"]]
    top, lanes, _ = live.build_compatibility_plan(caps_record(), revision="membership-check", tool_schema_version=TOOL_SCHEMA_VERSION_Q)
    for lane in top["lanes"]:
        entry = lanes[lane["lane_id"]]["planned_order"][0]
        assert _labels(entry, "compatibility", lane["lane_id"])["executed_arm_memberships"] == ["queue_compat"]
