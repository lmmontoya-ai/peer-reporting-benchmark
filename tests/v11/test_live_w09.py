"""W09 live-layer findings: consumed attempts, bounded usage, dispatch order, binding, and freeze. No model calls."""

import asyncio
import functools
import shutil
from pathlib import Path

import pytest

from swarm_auth_bench.long_events import iter_events
from swarm_auth_bench.peer_reporting.live import _exclusive
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import collection
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review

from .live_fakes import (
    Harness,
    build_plan,
    caps_record,
    compat_fixture,
    compat_root,
    fake_bundle,
    fake_version,
    qualified_root,
    qualifier_steps,
    report_steps,
    review_plan_for,
    study_rows,
    write_study,
)
from .live_fakes import run_phase as run

NATIVE = ("raw", "item/started", {"item": {"id": "native-1", "type": "commandExecution"}})
SERIAL = caps_record(global_max_concurrency=1)


def journal(lane_dir):
    return list(iter_events(Path(lane_dir) / "journal.jsonl"))


def records(root, kind):
    return [record for lane in sorted((Path(root) / "lanes").iterdir()) for record in journal(lane)
            if record["kind"] == kind]


def payloads(root):
    return {payload["attempt_id"]: payload for payload in (
        read_sealed(path) for path in sorted(Path(root).glob("lanes/*/attempts/*/attempt.json")))}


def scripted(fixtures, special=None):
    """Script per (model, effort, level, variant); every other attempt reads, reports, submits, and finishes."""
    special = special or {}
    by_packet = {fixture["packet"]: fixture for fixture in fixtures.values()}

    def script_for(model, effort):
        def steps(packet):
            fixture = by_packet[packet]
            key = (model, effort, fixture["parameters"]["level"], fixture["parameters"]["variant"])
            return special[key](fixture) if key in special else report_steps(fixture)
        return steps
    return script_for


def smoke_study(directory, caps=None):
    """Twelve smoke rows plus collection rows, so the collection gate can be built from the same study."""
    rows, fixtures = study_rows("smoke")
    collection_rows, collection_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    study = write_study(directory, rows + collection_rows, {**fixtures, **collection_fixtures}, caps=caps)
    return study, rows, fixtures


def prepare(root, built, study, prior_roots=()):
    v11_live.prepare_live_root(root, built, study_directory=study, prior_roots=prior_roots)
    return v11_live.read_live_plan(root)


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("w09-compat"))[0]


# B1: the study-level consumed-attempt ledger


async def test_a_new_root_never_reruns_a_consumed_assignment(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study", caps=SERIAL)
    v1 = study / "roots" / "smoke-v1"
    plan_v1 = prepare(v1, build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat]), study)
    failing = scripted(fixtures, {("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [NATIVE, *report_steps(f)]})
    status = await run(v1, plan_v1, Harness(tmp_path / "h1", failing), compatibility_directories=[compat],
                       study_directory=study)
    consumed = sorted(record["data"]["attempt_id"] for record in records(v1, "attempt_started"))
    assert status["status"] == "held" and len(consumed) == 3
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    assert (await run(v1, plan_v1, resumed, compatibility_directories=[compat],
                      study_directory=study))["status"] == "held"
    assert resumed.created == []

    with pytest.raises(ValueError, match="registered in this study"):
        build_plan("smoke", study, revision="smoke-v2", caps=SERIAL, compatibility_directories=[compat])
    built = build_plan("smoke", study, revision="smoke-v2", caps=SERIAL, compatibility_directories=[compat],
                       prior_roots=[v1])
    ledger = built[0]["consumed_attempts"]
    assert ledger["prior_roots"] == [{"plan_hash": plan_v1["seal_hash"], "revision": "smoke-v1",
                                      "consumed_attempt_ids": consumed}]
    assert ledger["excluded_assignment_ids"] == sorted(attempt[:-len("-live-1")] for attempt in consumed)
    assert built[0]["maximum_live_calls"] == 9
    v2 = study / "roots" / "smoke-v2"
    plan_v2 = prepare(v2, built, study, prior_roots=[v1])
    assert v11_live.superseded_by(v1, plan_v1) == [plan_v2["seal_hash"]]

    # The superseded root never runs again, and the new root refuses without its prior root.
    again = Harness(tmp_path / "h3", scripted(fixtures))
    with pytest.raises(v11_live.LivePhaseError, match="superseded"):
        await run(v1, plan_v1, again, compatibility_directories=[compat], study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="prior root"):
        await run(v2, plan_v2, again, compatibility_directories=[compat], study_directory=study)
    assert again.created == []
    status = await run(v2, plan_v2, again, compatibility_directories=[compat], prior_roots=[v1],
                       study_directory=study)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 9
    started_v2 = {record["data"]["attempt_id"] for record in records(v2, "attempt_started")}
    assert len(started_v2) == 9 and not started_v2 & set(consumed)
    assert {row["assignment_id"] for row in rows} == {attempt[:-len("-live-1")]
                                                      for attempt in started_v2 | set(consumed)}

    # Verify and export recheck the ledger.
    report = v11_live.verify_live_root(v2, bundle=fake_bundle(), prior_roots=[v1], study_directory=study)
    assert report["consumed_attempt_ledger"] == {"checked": True, "applicable": True, "prior_roots": 1,
                                                 "consumed_attempts": 3, "excluded_assignments": 3, "overlap": []}
    with pytest.raises(v11_live.EvidenceError, match="missing"):
        v11_live.verify_live_root(v2, bundle=fake_bundle(), prior_roots=[], study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="missing"):
        export_live_review(v2, tmp_path / "export-refused", bundle=fake_bundle(), study_directory=study)
    exported = export_live_review(v2, tmp_path / "export", prior_roots=[v1], bundle=fake_bundle(),
                                  study_directory=study)
    assert exported["attempts"] == 9
    assert read_sealed(tmp_path / "export" / "index.json")["consumed_attempt_ledger"]["consumed_attempts"] == 3

    # A third root must name both earlier roots; the collection gate needs one complete smoke root.
    with pytest.raises(ValueError, match="registered in this study"):
        build_plan("smoke", study, revision="smoke-v3", caps=SERIAL, compatibility_directories=[compat],
                   prior_roots=[v1])
    with pytest.raises(v11_live.GateError, match="differ from the study's smoke rows"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=v2)


async def test_prepare_rechecks_prior_roots_under_their_locks(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    v1 = study / "roots" / "smoke-v1"
    plan_v1 = prepare(v1, build_plan("smoke", study, compatibility_directories=[compat]), study)
    built = build_plan("smoke", study, revision="smoke-v2", compatibility_directories=[compat], prior_roots=[v1])
    assert built[0]["consumed_attempts"]["consumed_attempt_ids"] == [] and built[0]["maximum_live_calls"] == 12
    with _exclusive(v1 / v11_live.COORDINATOR_LOCK):  # the prior root is running
        with pytest.raises(v11_live.LivePhaseError, match="another process"):
            v11_live.prepare_live_root(study / "roots" / "smoke-v2", built, study_directory=study, prior_roots=[v1])
    with pytest.raises(ValueError, match="registered in its study"):
        v11_live.prepare_live_root(study / "roots" / "smoke-v2", built, prior_roots=[v1])
    await run(v1, plan_v1, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
              study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="started attempts after this plan was built"):
        v11_live.prepare_live_root(study / "roots" / "smoke-v2", built, study_directory=study, prior_roots=[v1])
    assert not (study / "roots" / "smoke-v2").exists() and v11_live.superseded_by(v1, plan_v1) == []
    with pytest.raises(ValueError, match="already consumed"):
        build_plan("smoke", study, revision="smoke-v2", compatibility_directories=[compat], prior_roots=[v1])


# M1: unknown final usage after a clean close


async def test_limit_hits_settle_at_the_reservation_bound_and_pass_the_smoke_gate(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study", caps=SERIAL)
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat]), study)
    now = [0.0]

    def advance():
        now[0] += 10_000

    read = ("tool", "read_channel", {"after_event_id": None, "limit": 8})
    special = {
        ("gpt-6-luna", "xhigh", "L1", "violation"): lambda f: [read, ("usage", 61000), ("stall", lambda: None)],
        ("gpt-6-sol", "xhigh", "L1", "violation"): lambda f: [("usage", 5000), *[read] * 40],
        ("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [read, ("usage", 3000), ("stall", advance)],
        ("gpt-6-luna", "low", "L1", "violation"): lambda f: [read, ("usage", 80000), ("stall", lambda: None)],
    }
    harness = Harness(tmp_path / "homes", scripted(fixtures, special))
    status = await run(root, plan, harness, compatibility_directories=[compat], clock=lambda: now[0],
                       sleep=lambda seconds: asyncio.sleep(min(seconds, 0.01)), study_directory=study)
    assert status["status"] == "complete" and status["holds"] == [] and status["live_model_call_starts"] == 12
    expected = {"gpt-6-luna-xhigh": ("trial_observed_token_limit", 61000, 75000),
                "gpt-6-sol-xhigh": ("tool_request_limit", 5000, 75000),
                "gpt-6-astra-xhigh": ("trial_wall_limit", 3000, 75000),
                "gpt-6-luna-low": ("trial_observed_token_limit", 80000, 80000)}
    bounded = {payload["lane_id"]: payload for payload in payloads(root).values()
               if payload["orchestrator"]["usage_settlement"]["status"] == "bounded_by_reservation"}
    assert set(bounded) == set(expected)
    for lane, (reason, observed, bound) in expected.items():
        payload = bounded[lane]
        result = payload["observer_result"]
        assert result["boundary"] == {**result["boundary"], "termination_kind": "per_trial_limit", "reason": reason}
        assert result["usage"]["total_tokens"] is None and result["usage"]["observed_total_tokens"] == observed
        assert payload["orchestrator"]["usage_settlement"] == {
            "status": "bounded_by_reservation", "actual_tokens": bound, "observed_tokens": observed,
            "reservation_tokens": max(observed, 75000)}
        check = payload["check"]
        assert check["passed"] is True and check["failure_reasons"] == [] and check["checks"]["usage_known"] is False
        assert check["usage_bounded_by_reservation"] is True and check["checks"]["valid_close"] is True
        settled = [record["data"] for record in journal(root / "lanes" / lane) if record["kind"] == "usage_settled"
                   and record["data"]["attempt_id"] == payload["attempt_id"]]
        assert settled == [{**settled[0], "status": "settled", "actual_tokens": bound,
                            "usage_settlement": "bounded_by_reservation"}]
    # The lane ledgers charge exactly the archived settlements.
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    for lane, lane_report in report["lanes"].items():
        charged = sum(row["usage_total_tokens"] for row in lane_report["entries"])
        assert lane_report["ledger"]["settled_tokens"] == charged and lane_report["ledger"]["reserved_tokens"] == 0
    assert report["lanes"]["gpt-6-luna-xhigh"]["ledger"]["settled_tokens"] == 75000 + 2000 + 2000
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    assert len(built[0]["gate_evidence"]["smoke"]["attempt_hashes"]) == 12
    exported = export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study)
    assert exported["attempts"] == 12
    index = read_sealed(tmp_path / "export" / "index.json")
    attempt = read_sealed(tmp_path / "export" / next(row["attempt_path"] for row in index["rows"]
                                                     if row["lane_id"] == "gpt-6-sol-xhigh" and row["level"] == "L1"))
    assert attempt["usage"] == {"total_tokens": 75000, "observed_total_tokens": 5000,
                                "settlement": "bounded_by_reservation"}
    assert attempt["eligible"] is True and attempt["termination_kind"] == "per_trial_limit"


# m1: provisional hold before any await


async def test_a_failed_observer_result_holds_before_another_lane_can_start(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=2))
    sample = compat_fixture(root, plan)
    released = asyncio.Event()

    async def preflight(runtime, model, caps, *, reasoning_effort, bundle):
        if (model, reasoning_effort) == ("gpt-6-sol", "xhigh"):
            await released.wait()  # sol's preflight completes exactly when luna's observer returns
        return await v11_live.manifest_preflight(runtime, model, caps, reasoning_effort=reasoning_effort,
                                                 bundle=bundle, version_reader=fake_version)

    async def observer(*args, **kwargs):
        result = await v11_phase.live_runtime.run_live_observer(*args, **kwargs)
        if kwargs["requested_model"] == "gpt-6-luna":
            result["runtime_closed"] = False  # a failed execution check
            released.set()
        return result

    harness = Harness(tmp_path / "homes", lambda model, effort: qualifier_steps(sample))
    status = await run(root, plan, harness, preflight=preflight, observer=observer)
    assert "execution_check_failure:compat-v1-gpt-6-luna-xhigh-live-1" in status["holds"]
    sol = [record["kind"] for record in journal(root / "lanes" / "gpt-6-sol-xhigh")]
    assert "preflight_passed" in sol and "attempt_started" not in sol
    held = [record["data"] for record in journal(root / "lanes" / "gpt-6-sol-xhigh")
            if record["kind"] == "admission_held"]
    assert held[0]["after_preflight"] is True
    assert status["live_model_call_starts"] == 1


# M6: one global dispatcher


async def test_cancelling_the_coordinator_archives_active_attempts_and_closes_every_lane(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=2))
    stalled = []
    read = ("tool", "read_channel", {"after_event_id": None, "limit": 8})
    harness = Harness(tmp_path / "homes", lambda model, effort: [read, ("stall", lambda: stalled.append(model))])
    task = asyncio.create_task(run(root, plan, harness))
    while len(stalled) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(harness.created) == 2
    report = v11_live.verify_live_root(root, bundle=fake_bundle())
    assert report["live_model_call_starts"] == 2 and report["unreconciled_starts"] == []
    assert report["status_counts"] == {"archived": 2, "unrun": 4}
    for lane in plan["lanes"]:
        assert journal(root / lane["path"])[-1]["kind"] == "run_closed"
    for payload in payloads(root).values():
        assert payload["check"]["passed"] is False
        assert "observer adapter was cancelled" in payload["observer_result"]["infrastructure_failures"]
    resumed = Harness(tmp_path / "homes-2", lambda model, effort: [])
    status = await run(root, plan, resumed)
    assert resumed.created == [] and status["status"] == "held"


def test_next_dispatch_takes_the_lowest_planned_order_whose_lane_is_idle():
    pending = [(0, "a", {}), (1, "a", {}), (2, "b", {}), (3, "c", {})]
    assert v11_phase.next_dispatch(pending, lambda lane: True)[0] == 0
    assert v11_phase.next_dispatch(pending, lambda lane: lane != "a")[0] == 2
    assert v11_phase.next_dispatch(pending, lambda lane: lane == "c")[0] == 3
    assert v11_phase.next_dispatch(pending, lambda lane: False) is None


async def test_realized_start_order_follows_planned_order_across_lanes(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study", caps=SERIAL)
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat]), study)
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures)),
                       compatibility_directories=[compat], study_directory=study)
    starts = sorted(records(root, "attempt_started"), key=lambda record: record["data"]["dispatch_seq"])
    assert [record["data"]["planned_order"] for record in starts] == list(range(12))
    assert [record["data"]["dispatch_seq"] for record in starts] == list(range(1, 13))
    assert status["realized_order"] == [record["data"]["attempt_id"] for record in starts]


async def test_parallel_dispatch_starts_the_lowest_order_of_each_idle_lane(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, compatibility_directories=[compat]), study)
    slow = {("gpt-6-luna", "xhigh", level, variant): (lambda f: [("sleep", 0.05), *report_steps(f)])
            for level, variant in (("L1", "violation"), ("L3", "violation"), ("L4", "twin"))}
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures, slow)),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["peak_active_attempts"] <= 6
    order = {record["data"]["attempt_id"]: record["data"]["planned_order"]
             for record in records(root, "attempt_started")}
    realized = [order[attempt] for attempt in status["realized_order"]]
    assert realized[:6] == [0, 1, 2, 9, 10, 11]  # every xhigh lane is busy, so the low lanes start next
    for lane in plan["lanes"]:
        lane_orders = [order[record["data"]["attempt_id"]] for record in journal(root / lane["path"])
                       if record["kind"] == "attempt_started"]
        assert lane_orders == sorted(lane_orders)


# M7 and m7: binding to the study


def test_a_plan_must_match_the_studys_caps_tools_and_protocol(wp6_study, tmp_path):
    directory, manifest, _ = wp6_study
    other_caps = caps_record(global_max_concurrency=1, lane_wall_seconds={phase: 30 for phase in
                                                                          ("compatibility", "calibration", "smoke",
                                                                           "collection")})
    with pytest.raises(ValueError, match="caps_hash"):
        v11_live.build_phase_plan("smoke", other_caps, revision="smoke-v1", study_directory=directory,
                                  bundle=load_bundle())
    tampered = tmp_path / "study"
    shutil.copytree(directory, tampered)
    edited = read_sealed(tampered / STUDY_MANIFEST)
    edited.pop("seal_hash")
    edited["tool_manifest_hash"] = "0" * 64
    atomic_json(tampered / STUDY_MANIFEST, seal(edited))
    with pytest.raises(ValueError, match="tool_manifest_hash"):
        v11_live.build_phase_plan("smoke", caps_record(), revision="smoke-v1", study_directory=tampered,
                                  bundle=load_bundle())


def test_building_a_plan_runs_the_full_study_verification(wp6_study, monkeypatch):
    directory, _, _ = wp6_study
    calls = []
    original = collection.verify_study

    def recording(study_directory, **kwargs):
        calls.append((Path(study_directory), sorted(kwargs)))
        return original(study_directory, **kwargs)

    monkeypatch.setattr(collection, "verify_study", recording)
    with pytest.raises(v11_live.GateError, match="compatibility"):  # verification passed; the gate comes next
        v11_live.build_phase_plan("smoke", caps_record(), revision="smoke-v1", study_directory=directory,
                                  bundle=load_bundle())
    assert calls == [(Path(directory), ["caps_record", "protocol", "templates"])]
    monkeypatch.setattr(collection, "verify_study", lambda study_directory, **kwargs: {
        "valid": False, "errors": ["fixture differs from recomputed fixture"]})
    with pytest.raises(v11_live.EvidenceError, match="study verification failed"):
        v11_live.build_phase_plan("smoke", caps_record(), revision="smoke-v1", study_directory=directory,
                                  bundle=load_bundle())


def test_fake_study_binding_and_verifier_result_are_enforced(compat, tmp_path):
    rows, fixtures = study_rows("smoke")
    study = write_study(tmp_path / "study", rows, fixtures, tool_manifest_hash="0" * 64)
    with pytest.raises(ValueError, match="tool_manifest_hash"):
        build_plan("smoke", study, compatibility_directories=[compat])
    good = write_study(tmp_path / "good", rows, fixtures)
    with pytest.raises(v11_live.EvidenceError, match="verification failed"):
        build_plan("smoke", good, compatibility_directories=[compat],
                   study_verifier=lambda directory, caps: {"valid": False, "errors": ["bad"]})


async def test_the_collection_gate_refuses_a_partial_smoke_root(compat, tmp_path):
    collection_rows, collection_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    rows, fixtures = study_rows("smoke")
    study = write_study(tmp_path / "study", rows + collection_rows, {**fixtures, **collection_fixtures})
    smoke_rows, smoke_fixtures, source = v11_live.load_study(study, "smoke")
    partial = v11_live.build_assignment_plan("smoke", smoke_rows[:1], smoke_fixtures, caps_record(),
                                             revision="smoke-partial", source=source,
                                             study_manifest=v11_live.read_study_manifest(study), gate_evidence={
                                                 "qualification": {}}, bundle=fake_bundle())
    root = study / "roots" / "smoke"
    assert prepare(root, partial, study)["maximum_live_calls"] == 1
    with pytest.raises(v11_live.GateError, match="differ from the study's smoke rows"):
        build_plan("collection", study, compatibility_directories=[compat], smoke_directory=root,
                   review_plan=review_plan_for(study))


# m2: settlement conflicts


async def test_a_settlement_conflict_is_a_failure_and_holds(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=SERIAL)
    sample = compat_fixture(root, plan)

    async def observer(*args, **kwargs):
        result = await v11_phase.live_runtime.run_live_observer(*args, **kwargs)
        result["usage"]["total_tokens"] = 1000  # below the 4321 tokens the ledger already observed
        return result

    harness = Harness(tmp_path / "homes", lambda model, effort: qualifier_steps(sample))
    status = await run(root, plan, harness, observer=observer)
    attempt = "compat-v1-gpt-6-luna-xhigh-live-1"
    assert len(harness.created) == 1 and status["status"] == "held"
    assert {f"execution_check_failure:{attempt}", f"unknown_final_usage:{attempt}"} <= set(status["holds"])
    payload = payloads(root)[attempt]
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    assert "smaller than observed" in payload["orchestrator"]["usage_settlement"]["conflict"]
    assert any(failure.startswith("usage settlement conflict") for failure in payload["orchestrator"][
        "evidence_failures"])
    assert payload["check"]["checks"]["orchestrator_evidence_intact"] is False


# m3: a retained ledger stop


async def test_a_retained_ledger_stop_holds_before_any_start(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study", caps=SERIAL)
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat]), study)
    now = [1000.0]

    async def stop_after_first(*args, **kwargs):
        result = await v11_phase.live_runtime.run_live_observer(*args, **kwargs)
        (root / "STOP").touch()
        now[0] += 10 * 3600  # the open run itself outlasts the started lane's 3600-second wall
        return result

    first = await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                      observer=stop_after_first, study_directory=study, ledger_clock=lambda: now[0])
    assert first["live_model_call_starts"] == 1 and "soft_stop" in first["holds"]
    (root / "STOP").unlink()
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    status = await run(root, plan, resumed, compatibility_directories=[compat], ledger_clock=lambda: now[0],
                       study_directory=study)
    assert resumed.created == [] and status["status"] == "held"
    assert "retained_ledger_stop:gpt-6-luna-xhigh:collection_wall_limit" in status["holds"]


# m4: freeze


async def test_a_changed_sealed_file_refuses_the_run_and_is_reported(compat, tmp_path, monkeypatch):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, compatibility_directories=[compat]), study)
    original = v11_phase.implementation_hashes
    monkeypatch.setattr(v11_phase, "implementation_hashes",
                        lambda: {**original(), "peer_reporting_v11/world.py": "0" * 64})
    harness = Harness(tmp_path / "homes", scripted(fixtures))
    with pytest.raises(v11_live.LivePhaseError, match="changed after sealing"):
        await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    assert harness.created == []
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["implementation_changes"] == ["peer_reporting_v11/world.py"]


def test_no_execution_module_imports_a_post_hoc_module():
    import re
    from pathlib import Path

    package = Path(v11_phase.__file__).parent
    exempt = {Path(name).stem for name in v11_phase.POST_HOC_MODULES}
    anywhere = re.compile(r"^\s*(?:from\s+\.(?:\w+\.)*(?P<a>\w+)\s+import|from\s+\.\s+import\s+(?P<b>[\w, ]+))", re.M)
    top_level = re.compile(r"^(?:from\s+\.(?:\w+\.)*(?P<a>\w+)\s+import|from\s+\.\s+import\s+(?P<b>[\w, ]+))", re.M)
    for path in package.glob("*.py"):
        if path.stem in exempt:
            continue
        # The CLI dispatches offline commands too; it may import post-hoc code only inside their branches.
        pattern = top_level if path.stem == "cli" else anywhere
        imported = set()
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            imported.add(match["a"]) if match["a"] else imported.update(n.strip() for n in match["b"].split(","))
        assert not imported & exempt, f"{path.name} imports post-hoc module(s) {imported & exempt}"


async def test_a_changed_scorer_is_reported_but_does_not_gate_the_run(compat, tmp_path, monkeypatch):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, compatibility_directories=[compat]), study)
    original = v11_phase.implementation_hashes
    monkeypatch.setattr(v11_phase, "implementation_hashes",
                        lambda: {**original(), "peer_reporting_v11/score.py": "0" * 64})
    harness = Harness(tmp_path / "homes", scripted(fixtures))
    await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    assert harness.created
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["implementation_changes"] == ["peer_reporting_v11/score.py"]


# m5: world mode from the world's own record


def test_world_mode_bound_reads_the_durable_world_state(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, compatibility_directories=[compat]), study)
    asyncio.run(run(root, plan, Harness(tmp_path / "homes", scripted(fixtures)), compatibility_directories=[compat],
                    study_directory=study))
    lane = root / "lanes" / "gpt-6-sol-low"
    (path,) = lane.glob("attempts/*/attempt.json")
    payload = read_sealed(path)
    entry = read_sealed(lane / "phase-plan.json")["planned_order"][0]
    fixture = v11_live.read_root_fixture(root, plan, entry["fixture_id"])
    result = payload["observer_result"]
    assert result["world_state"]["world_mode"] == "report_store_unavailable" == entry["world_mode"]
    evaluate = functools.partial(v11_live.evaluate_transport, fixture=fixture, entry=entry,
                                 preflight=payload["preflight"], bundle=fake_bundle(), orchestrator_failures=[],
                                 usage_settlement="settled")
    assert evaluate(result)["passed"] is True
    for change in ({"world_mode": "normal"}, {"max_tool_requests": 80}):
        relabeled = {**result, "world_state": {**result["world_state"], **change}}
        check = evaluate(relabeled)
        assert check["checks"]["world_mode_bound"] is False and check["passed"] is False


# m6: the authorization in lane evidence


async def test_runs_and_starts_journal_the_authorization_hash(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root = study / "roots" / "smoke"
    plan = prepare(root, build_plan("smoke", study, compatibility_directories=[compat]), study)
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures)),
                       compatibility_directories=[compat], study_directory=study)
    expected = status["authorization_hash"]
    assert {record["data"]["authorization_hash"] for record in records(root, "run_opened")} == {expected}
    assert {record["data"]["authorization_hash"] for record in records(root, "attempt_started")} == {expected}
    assert {payload["authorization_hash"] for payload in payloads(root).values()} == {expected}
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["authorization_hashes"] == [expected]
    (root / "authorizations" / f"{expected}.json").unlink()
    with pytest.raises(v11_live.EvidenceError, match="not retained"):
        v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)


# N6 and N7


async def test_an_injected_bundle_must_carry_the_v11_tools(tmp_path):
    bad = fake_bundle(input_schemas={**fake_bundle().input_schemas, "read_channel": {
        **fake_bundle().input_schemas["read_channel"], "properties": {
            **fake_bundle().input_schemas["read_channel"]["properties"], "limit": {"type": "integer",
                                                                                   "maximum": 64}}}})
    with pytest.raises(ValueError, match="tool manifest binding failed"):
        v11_live.build_compatibility_plan(caps_record(), revision="compat-v1", bundle=bad)
    root, plan = compat_root(tmp_path / "compat")
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    with pytest.raises(ValueError, match="tool manifest binding failed"):
        await run(root, plan, harness, bundle=bad)
    assert harness.created == []


async def test_root_stop_is_watched_even_with_a_stop_file_flag(tmp_path):
    root, plan = compat_root(tmp_path / "compat")
    (root / "STOP").touch()
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    status = await run(root, plan, harness, stop_file=tmp_path / "elsewhere-STOP")
    assert harness.created == [] and status["holds"] == ["soft_stop"]
    other, other_plan = compat_root(tmp_path / "compat-2")
    (tmp_path / "flag-STOP").touch()
    status = await run(other, other_plan, harness, stop_file=tmp_path / "flag-STOP")
    assert harness.created == [] and status["holds"] == ["soft_stop"]
    hard, hard_plan = compat_root(tmp_path / "compat-3")
    (hard / "HARD_STOP").touch()
    status = await run(hard, hard_plan, harness, stop_file=tmp_path / "elsewhere-STOP")
    assert harness.created == [] and status["holds"] == ["hard_stop"]
    flagged, flagged_plan = compat_root(tmp_path / "compat-4")
    (tmp_path / "flag-HARD_STOP").touch()
    status = await run(flagged, flagged_plan, harness, hard_stop_file=tmp_path / "flag-HARD_STOP")
    assert harness.created == [] and status["holds"] == ["hard_stop"]
