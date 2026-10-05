"""W09 round-2 live-layer findings: observed usage, soft and hard stops, open-run lane walls, the study
instance, root registration and abandonment, amendments, and the round barrier. No model calls."""

import asyncio
import json
import shutil
import time
from collections import defaultdict
from pathlib import Path

import pytest

from swarm_auth_bench.long_events import iter_events
from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import PROTOCOL_ID
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review

from .live_fakes import (
    Harness,
    build_plan,
    caps_record,
    fake_bundle,
    qualified_root,
    report_steps,
    study_rows,
    utc,
    write_study,
)
from .live_fakes import run_phase as run

NATIVE = ("raw", "item/started", {"item": {"id": "native-1", "type": "commandExecution"}})
READ = ("tool", "read_channel", {"after_event_id": None, "limit": 8})
SERIAL = caps_record(global_max_concurrency=1)
LUNA_L1 = ("gpt-6-luna", "xhigh", "L1", "violation")


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


def smoke_study(directory, caps=SERIAL, instance_nonce="0" * 32):
    rows, fixtures = study_rows("smoke")
    collection_rows, collection_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    study = write_study(directory, rows + collection_rows, {**fixtures, **collection_fixtures}, caps=caps,
                        instance_nonce=instance_nonce)
    return study, rows, fixtures


def smoke_root(base, compat, study, *, revision="smoke-v1", prior_roots=()):
    """A sealed smoke root at its place in the study, ``STUDY/roots/<revision>``; ``base`` is unused."""
    root = Path(study) / "roots" / revision
    built = build_plan("smoke", study, caps=SERIAL, revision=revision, compatibility_directories=[compat],
                       prior_roots=list(prior_roots))
    v11_live.prepare_live_root(root, built, study_directory=study, prior_roots=prior_roots)
    return root, v11_live.read_live_plan(root)


def attempt_of(rows, model, level, variant, effort="xhigh"):
    (row,) = [row for row in rows if (row["model"], row["level"], row["variant"], row["effort"])
              == (model, level, variant, effort)]
    return row["assignment_id"] + v11_live.ATTEMPT_SUFFIX


def amendment(study, attempt_ids, **changes):
    record = {"kind": v11_live.AMENDMENT_KIND, "protocol_id": PROTOCOL_ID,
              "study_manifest_hash": read_sealed(Path(study) / v11_live.STUDY_MANIFEST)["seal_hash"],
              "reason": "The failed smoke attempt was an infrastructure fault unrelated to the protocol.",
              "approval": {"status": "approved", "text": "Test-only approval of this amendment."},
              "action": v11_live.AMENDMENT_ACTION, "attempt_ids": sorted(attempt_ids), "recorded_utc": utc(0)}
    record.update(changes)
    return seal(record)


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("r2-compat"))[0]


# R2-M1: bounded settlement needs observed usage


def test_usage_is_observed_only_with_an_integer_total_and_no_notification_without_one():
    observed = {"usage": {"observed_total_tokens": 1500}}
    assert v11_phase.usage_unobserved(observed, {"with_total": 1, "without_total": []}) is None
    assert v11_phase.usage_unobserved(observed, {"with_total": 2, "without_total": ["n-2"]}) == \
        "usage_notification_without_total"
    assert v11_phase.usage_unobserved({"usage": {"observed_total_tokens": None}},
                                      {"with_total": 0, "without_total": []}) == "usage_never_observed"
    assert v11_phase.usage_unobserved(observed, {"with_total": 0, "without_total": []}) == "usage_never_observed"
    assert v11_phase.usage_unobserved(None, {"with_total": 0, "without_total": []}) == "usage_never_observed"


async def test_a_usage_notification_without_a_total_is_unresolved_and_fails_the_smoke_gate(compat, tmp_path):
    # The reviewer's scenario: the only usage notification carries no total, and the trial ends naturally.
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    blind = ("raw", "thread/tokenUsage/updated", {"tokenUsage": {"total": {"inputTokens": 0}}})
    special = {LUNA_L1: lambda f: [READ, blind, *report_steps(f, usage=None)]}
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures, special)),
                       compatibility_directories=[compat], study_directory=study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    assert f"unknown_final_usage:{attempt}" in status["holds"]
    payload = payloads(root)[attempt]
    assert payload["observer_result"]["termination_kind"] == "natural_end"
    assert payload["observer_result"]["usage"] == {**payload["observer_result"]["usage"], "total_tokens": None,
                                                   "observed_total_tokens": None}
    assert payload["orchestrator"]["usage_settlement"] == {"status": "unresolved", "actual_tokens": None,
                                                           "reason": "usage_notification_without_total"}
    assert payload["check"]["passed"] is False and "usage_known" in payload["check"]["failure_reasons"]
    with pytest.raises(v11_live.GateError, match="0 of 12 smoke records are valid"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)


# R2-M2: soft and hard stops, and open-run lane walls


async def test_a_soft_stop_lets_the_active_attempt_finish_and_the_resumed_smoke_root_passes_the_gate(
        compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    release = asyncio.Event()

    def soft_stop():
        (root / "STOP").touch()
        asyncio.get_running_loop().call_later(0.1, release.set)  # the coordinator sees STOP mid-attempt

    special = {LUNA_L1: lambda f: [READ, ("usage", 1500), ("call", soft_stop), ("wait", release),
                                   *report_steps(f)]}
    harness = Harness(tmp_path / "h1", scripted(fixtures, special))
    first = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    assert first["status"] == "held" and first["holds"] == ["soft_stop"] and first["live_model_call_starts"] == 1
    assert "turn/interrupt" not in harness.created[0].methods
    (payload,) = payloads(root).values()
    assert payload["observer_result"]["termination_kind"] == "natural_end" and payload["check"]["passed"] is True
    assert payload["orchestrator"]["collection_stop_reasons"] == []
    (root / "STOP").unlink()
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    status = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["holds"] == [] and len(resumed.created) == 11
    assert len(status["verification"]["authorization_hashes"]) == 2  # resumed under a new authorization
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    assert len(built[0]["gate_evidence"]["smoke"]["attempt_hashes"]) == 12


async def test_a_hard_stop_truncates_the_active_attempt_without_wedging_the_plan(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    special = {LUNA_L1: lambda f: [READ, ("usage", 1500), ("stall", lambda: (root / "HARD_STOP").touch())]}
    harness = Harness(tmp_path / "h1", scripted(fixtures, special))
    first = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    truncated = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert first["status"] == "held" and first["holds"] == ["hard_stop"] and first["live_model_call_starts"] == 1
    assert "turn/interrupt" in harness.created[0].methods
    payload = payloads(root)[truncated]
    assert payload["observer_result"]["termination_kind"] == "collection_forced_truncation"
    assert payload["orchestrator"]["collection_stop_reasons"] == ["hard_stop_or_forced_deadline"]
    assert payload["orchestrator"]["usage_settlement"]["status"] == "bounded_by_reservation"
    check = payload["check"]
    assert check["classification"] == "stop_truncation" and check["passed"] is False
    assert check["failure_reasons"] == ["valid_close"]  # consumed and behaviorally ineligible

    (root / "HARD_STOP").unlink()
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    status = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["holds"] == [] and len(resumed.created) == 11
    assert status["live_model_call_starts"] == 12  # the truncated attempt is never rerun
    exported = export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study)
    index = read_sealed(tmp_path / "export" / "index.json")
    row = next(row for row in index["rows"] if row["assignment_id"] == truncated[:-len("-live-1")])
    assert exported["attempts"] == 12
    assert read_sealed(tmp_path / "export" / row["attempt_path"])["termination_kind"] == "collection_forced_truncation"
    # A truncated smoke attempt is not valid smoke evidence; only an approved amendment resolves it.
    with pytest.raises(v11_live.GateError, match="11 of 12 smoke records are valid"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    v11_live.record_amendment(study, amendment(study, [truncated]), smoke_roots=[root], bundle=fake_bundle())
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    assert list(built[0]["gate_evidence"]["smoke"]["accepted_failed_attempts"]) == [truncated]


async def test_a_resume_after_a_long_pause_does_not_exhaust_the_lane_wall(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    special = {LUNA_L1: lambda f: [READ, ("call", lambda: (root / "STOP").touch()), *report_steps(f)]}
    first = await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures, special)),
                      compatibility_directories=[compat], study_directory=study)
    assert first["holds"] == ["soft_stop"] and first["live_model_call_starts"] == 1
    (root / "STOP").unlink()
    paused = 10 * 3600  # ten hours between runs; the lane wall is 3600 seconds
    status = await run(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), compatibility_directories=[compat],
                       study_directory=study, ledger_clock=lambda: time.time() + paused)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    lane = root / "lanes" / "gpt-6-luna-xhigh"
    opened = [record["data"]["lane_clock_seconds"] for record in journal(lane) if record["kind"] == "run_opened"]
    closed = [record["data"]["lane_clock_seconds"] for record in journal(lane) if record["kind"] == "run_closed"]
    assert len(opened) == 2 and closed[0] <= opened[1] < 600  # the pause is not lane time
    ledger = read_sealed(lane / "budget-ledger.json")
    assert not ledger["stop_generation"] and max(attempt["admitted_at"] for attempt in ledger["attempts"].values()) \
        < 600


def test_the_lane_clock_resumes_from_the_latest_recorded_lane_time():
    now = [5000.0]
    clock = v11_phase.LaneClock(lambda: now[0], 120.0)
    assert clock() == 120.0
    now[0] += 30
    assert clock() == 150.0


# R2-M3: the study instance, registration, and amendments


async def test_a_copied_study_cannot_lend_its_smoke_root_to_the_original(compat, tmp_path):
    # The reviewer's scenario: smoke-v1 holds after a failed attempt, and the study is copied without live-roots/.
    study, _, fixtures = smoke_study(tmp_path / "study")
    v1, plan_v1 = smoke_root(tmp_path, compat, study)
    failing = scripted(fixtures, {("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [NATIVE, *report_steps(f)]})
    status = await run(v1, plan_v1, Harness(tmp_path / "h1", failing), compatibility_directories=[compat],
                       study_directory=study)
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    # A copy that keeps the study's start ledger shows the starts that no registered root explains.
    partial = Path(shutil.copytree(study, tmp_path / "study-partial", ignore=shutil.ignore_patterns("live-roots")))
    with pytest.raises(v11_live.EvidenceError, match="start ledger"):
        build_plan("smoke", partial, caps=SERIAL, compatibility_directories=[compat])
    copy = Path(shutil.copytree(study, tmp_path / "study-copy",
                                ignore=shutil.ignore_patterns("live-roots", "live-starts", "roots")))
    # A deliberate copy keeps the instance seal and starts an empty ledger (spec 10: move, never copy) ...
    copied, plan_copy = smoke_root(tmp_path / "copy", compat, copy)
    assert plan_copy["maximum_live_calls"] == 12 and plan_copy["source"] == plan_v1["source"]
    status = await run(copied, plan_copy, Harness(tmp_path / "h2", scripted(fixtures)),
                       compatibility_directories=[compat], study_directory=copy)
    assert status["status"] == "complete"
    # ... but nothing bound to the original study accepts the copy's roots.
    with pytest.raises(v11_live.GateError, match="not registered in the supplied study directory"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=copied)
    with pytest.raises(v11_live.GateError, match="2 of 12 smoke records are valid"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=v1)
    harness = Harness(tmp_path / "h3", scripted(fixtures))
    with pytest.raises(v11_live.EvidenceError, match="not registered"):
        await run(copied, plan_copy, harness, compatibility_directories=[compat], study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="study directory"):
        await run(copied, plan_copy, harness, compatibility_directories=[compat])
    assert harness.created == []
    with pytest.raises(v11_live.EvidenceError, match="not registered"):
        v11_live.verify_live_root(copied, bundle=fake_bundle(), study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="study directory"):
        v11_live.verify_live_root(copied, bundle=fake_bundle())
    with pytest.raises(ValueError, match="not registered"):
        export_live_review(copied, tmp_path / "export", bundle=fake_bundle(), study_directory=study)


async def test_a_rebuilt_study_is_a_new_instance_that_old_roots_cannot_serve(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    status = await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                       study_directory=study)
    assert status["status"] == "complete"
    rebuilt, _, _ = smoke_study(tmp_path / "rebuilt", instance_nonce="1" * 32)  # same rows, a new instance
    seals = [read_sealed(directory / v11_live.STUDY_MANIFEST)["seal_hash"] for directory in (study, rebuilt)]
    assert seals[0] != seals[1]
    with pytest.raises(v11_live.GateError, match="another phase or study"):
        build_plan("collection", rebuilt, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    with pytest.raises(v11_live.EvidenceError, match="another study instance"):
        v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=rebuilt)
    # The rebuilt study needs its own smoke; the original study still accepts its own smoke root.
    assert build_plan("smoke", rebuilt, caps=SERIAL, compatibility_directories=[compat])[0][
        "maximum_live_calls"] == 12
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    assert built[0]["gate_evidence"]["smoke"]["accepted_failed_attempts"] == {}


async def test_an_amendment_accepts_a_failed_smoke_attempt_which_stays_consumed_and_excluded(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    failing = scripted(fixtures, {("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [NATIVE, *report_steps(f)]})
    first = await run(root, plan, Harness(tmp_path / "h1", failing), compatibility_directories=[compat],
                      study_directory=study)
    failed = attempt_of(rows, "gpt-6-astra", "L1", "violation")
    valid = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    unstarted = attempt_of(rows, "gpt-6-luna", "L3", "violation")
    assert first["status"] == "held" and first["live_model_call_starts"] == 3

    bad = [(amendment(study, [failed], study_manifest_hash="0" * 64), "another study seal"),
           (amendment(study, [failed], approval={"status": "proposed", "text": "Not yet."}), "approved"),
           (amendment(study, [failed], action="rerun_failed_smoke_attempts"), "only amendment action"),
           (amendment(study, ["v11-unknown-live-1"]), "smoke rows"),
           (amendment(study, [failed, valid]), "not a failed attempt"),
           (amendment(study, [failed, unstarted]), "not a failed attempt")]
    for record, message in bad:
        with pytest.raises(ValueError, match=message):
            v11_live.record_amendment(study, record, smoke_roots=[root], bundle=fake_bundle())
    with pytest.raises(ValueError, match="artifact seal"):
        v11_live.record_amendment(study, {**amendment(study, [failed]), "reason": "Edited."}, smoke_roots=[root])
    assert v11_live.study_amendments(study) == []
    with pytest.raises(v11_live.GateError, match="2 of 12 smoke records are valid"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)

    recorded = v11_live.record_amendment(study, amendment(study, [failed]), smoke_roots=[root],
                                         bundle=fake_bundle())
    accepted = {failed: [recorded["amendment_hash"]]}
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    status = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["holds"] == [] and len(resumed.created) == 9, status["holds"]
    assert status["accepted_failed_attempts"] == accepted and status["live_model_call_starts"] == 12
    assert failed not in {record["data"]["attempt_id"] for record in records(root, "attempt_started")
                          if record["data"]["authorization_hash"] == status["authorization_hash"]}
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root)
    assert built[0]["gate_evidence"]["smoke"]["accepted_failed_attempts"] == accepted
    assert len(built[0]["gate_evidence"]["smoke"]["attempt_hashes"]) == 12

    export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study)
    index = read_sealed(tmp_path / "export" / "index.json")
    assert index["analysis_exclusions"] == [failed[:-len("-live-1")]]
    row = next(row for row in index["rows"] if row["excluded_from_analysis"])
    assert row["amendment_hashes"] == accepted[failed]
    exported = read_sealed(tmp_path / "export" / row["attempt_path"])
    assert exported["excluded_from_analysis"] is True and exported["eligible"] is False
    assert [record["seal_hash"] for record in index["study_registry"]["amendments"]] == accepted[failed]
    assert [(entry["plan_hash"], entry["state"]) for entry in index["study_registry"]["roots"]] == [
        (plan["seal_hash"], "finalized")]


# R2-m2: pending registration and abandonment


async def test_a_crash_between_registration_and_plan_write_is_abandoned_not_wedged(compat, tmp_path, monkeypatch):
    study, _, fixtures = smoke_study(tmp_path / "study")
    built = build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat])
    crashed = study / "roots" / "smoke-v1"

    def crash(*args, **kwargs):
        raise OSError("simulated crash while writing the lanes")

    with monkeypatch.context() as patched:
        patched.setattr(v11_live, "create_lane_phase", crash)
        with pytest.raises(OSError, match="simulated crash"):
            v11_live.prepare_live_root(crashed, built, study_directory=study)
    (pending,) = v11_live.registered_roots(study)
    assert pending["state"] == "pending" and not (crashed / v11_live.LIVE_PLAN_FILE).exists()
    with pytest.raises(ValueError, match="abandon a pending one"):
        build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat])
    with pytest.raises(v11_live.LivePhaseError, match="registered"):
        v11_live.prepare_live_root(study / "roots" / "smoke-v1b", built, study_directory=study)
    abandoned = v11_live.abandon_root(study, pending["plan_hash"], reason="Crashed while writing the plan.",
                                      root=crashed)
    assert abandoned["prior_state"] == "pending" and abandoned["root_journals_checked"] is True
    assert [entry["state"] for entry in v11_live.registered_roots(study)] == ["abandoned"]
    with pytest.raises(ValueError, match="already abandoned"):
        v11_live.abandon_root(study, pending["plan_hash"], reason="Again.")
    root, plan = smoke_root(tmp_path, compat, study, revision="smoke-v2")
    assert plan["consumed_attempts"]["prior_roots"] == [] and plan["maximum_live_calls"] == 12
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures)),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete"


async def test_only_a_root_that_never_started_can_be_abandoned_and_then_it_never_runs(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    v1, plan_v1 = smoke_root(tmp_path, compat, study)
    special = {LUNA_L1: lambda f: [READ, ("call", lambda: (v1 / "STOP").touch()), *report_steps(f)]}
    first = await run(v1, plan_v1, Harness(tmp_path / "h1", scripted(fixtures, special)),
                      compatibility_directories=[compat], study_directory=study)
    assert first["live_model_call_starts"] == 1
    (v1 / "STOP").unlink()
    with pytest.raises(v11_live.LivePhaseError, match="consumed attempts are never abandoned"):
        v11_live.abandon_root(study, plan_v1["seal_hash"], reason="Started already.", root=v1)
    # v2 supersedes v1 and never runs; abandoning it requires its directory and lets v1 resume.
    v2, plan_v2 = smoke_root(tmp_path, compat, study, revision="smoke-v2", prior_roots=[v1])
    assert v11_live.superseded_by(v1, plan_v1) == [plan_v2["seal_hash"]]
    with pytest.raises(ValueError, match="needs its directory"):
        v11_live.abandon_root(study, plan_v2["seal_hash"], reason="Built with the wrong compatibility roots.")
    v11_live.abandon_root(study, plan_v2["seal_hash"], reason="Built with the wrong compatibility roots.", root=v2)
    harness = Harness(tmp_path / "h2", scripted(fixtures))
    with pytest.raises(v11_live.LivePhaseError, match="abandoned"):
        await run(v2, plan_v2, harness, compatibility_directories=[compat], prior_roots=[v1], study_directory=study)
    assert harness.created == []
    report = v11_live.verify_live_root(v1, bundle=fake_bundle(), study_directory=study)
    assert report["superseded_by"] == [plan_v2["seal_hash"]] and report["superseded_by_active"] == []
    status = await run(v1, plan_v1, harness, compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and len(harness.created) == 11


# R2-m4: the round barrier


def test_the_round_barrier_blocks_a_lane_two_rounds_ahead_within_its_effort():
    def item(order, lane, round_index, effort="xhigh"):
        return (order, lane, {"round": round_index, "reasoning_effort": effort})

    pending = [item(5, "sol", 0), item(6, "luna", 2), item(7, "luna", 3), item(8, "sol-low", 4, "low")]
    assert v11_phase.next_dispatch(pending, lambda lane: lane == "luna") is None  # sol still has round 0
    assert v11_phase.next_dispatch(pending, lambda lane: lane in {"luna", "sol-low"})[0] == 8  # another effort
    assert v11_phase.next_dispatch(pending, lambda lane: True)[0] == 5
    assert v11_phase.next_dispatch([item(1, "sol", 1), item(2, "luna", 2)], lambda lane: lane == "luna")[0] == 2
    assert v11_phase.next_dispatch([item(1, "luna", 0), item(2, "luna", 4)], lambda lane: True)[0] == 1
    assert v11_phase.next_dispatch([(0, "a", {}), (1, "b", {})], lambda lane: lane == "b")[0] == 1


def simulate(rows, *, durations, slots=6, barrier=True, stop_lane="gpt-6-astra-xhigh", stop_fraction=0.25):
    """The reviewer's speed simulation over the real dispatcher: returns the rows started per lane at the stop."""
    pending = sorted(((row["planned_order"], f"{row['model']}-{row['effort']}",
                       {"round": row["round"], "reasoning_effort": row["effort"], "row": row}) for row in rows),
                     key=lambda item: (item[0], item[1]))
    total = sum(item[1] == stop_lane for item in pending)
    now, active, finished, started = 0.0, {}, defaultdict(int), defaultdict(list)
    while finished[stop_lane] < stop_fraction * total:
        while len(active) < slots:
            if barrier:
                floors = v11_phase.round_floors(pending)
                choice = v11_phase.next_dispatch(pending, lambda lane: lane not in active)
            else:
                choice = next((item for item in pending if item[1] not in active), None)
            if choice is None:
                break
            _, lane, entry = choice
            if barrier:  # spec 9 at the moment of every start
                assert all(floor > entry["round"] - 2 for other, floor in floors[entry["reasoning_effort"]].items()
                           if other != lane)
            pending.remove(choice)
            active[lane] = now + durations[lane]
            started[lane].append(entry["row"])
        lane = min(active, key=active.get)
        now = active.pop(lane)
        finished[lane] += 1
    return started, pending


def test_the_round_barrier_bounds_model_drift_in_the_real_collection_order(wp6_study):
    _, manifest, _ = wp6_study
    rows = [row for row in manifest["assignments"] if row["split"] == "collection"]
    # Luna twice as fast as astra, sol 1.5 times, and low effort twice as fast as xhigh.
    speeds = {"gpt-6-luna": 2.0, "gpt-6-sol": 1.5, "gpt-6-astra": 1.0}
    durations = {f"{model}-{effort}": (1.0 if effort == "xhigh" else 0.5) / speed
                 for model, speed in speeds.items() for effort in ("xhigh", "low")}
    free, _ = simulate(rows, durations=durations, barrier=False)
    started, pending = simulate(rows, durations=durations)
    xhigh = {model: len(started[f"{model}-xhigh"]) for model in speeds}
    unbounded = {model: len(free[f"{model}-xhigh"]) for model in speeds}
    assert unbounded["gpt-6-luna"] >= 1.8 * unbounded["gpt-6-astra"]  # the drift the reviewer measured
    floors = v11_phase.round_floors(pending)
    per_round = max(sum(1 for row in rows if (row["model"], row["effort"], row["round"]) == key)
                    for key in {(row["model"], row["effort"], row["round"]) for row in rows})
    for effort, lanes in floors.items():
        highest = max(row["round"] for lane, items in started.items() if lane.endswith(effort) for row in items)
        assert highest <= min(lanes.values()) + 1  # nothing two rounds past any lane's first unstarted round
    assert max(xhigh.values()) - min(xhigh.values()) <= 2 * per_round < max(unbounded.values()) - min(
        unbounded.values())
    # A lane runs a violation and its twin back to back, so at most one pair per lane is open at the stop.
    for lane, items in started.items():
        keys = defaultdict(set)
        for row in [*items, *(entry["row"] for _, other, entry in pending if other == lane)]:
            if row["variant"] in {"violation", "twin"}:
                keys[(row["arm"], row["template_id"], row["level"], row["near_miss_type"], row["prompt_condition"],
                      row["round"])].add(row["assignment_id"])
        begun = {row["assignment_id"] for row in items}
        assert sum(0 < len(ids & begun) < len(ids) for ids in keys.values()) <= 1


async def test_fast_lanes_wait_at_the_round_barrier_in_a_live_run(compat, tmp_path):
    cells = (("L1", "violation", "guided"), ("L1", "twin", "guided"), ("L2", "violation", "neutral"),
             ("L2", "twin", "neutral"), ("L3", "violation", "discouraged"), ("L3", "twin", "discouraged"))
    rows, fixtures = study_rows("smoke", cells=tuple((*cell, "xhigh", "normal") for cell in cells))
    study = write_study(tmp_path / "study", rows, fixtures)
    built = build_plan("smoke", study, compatibility_directories=[compat])
    root = study / "roots" / "smoke"
    v11_live.prepare_live_root(root, built, study_directory=study)
    plan = v11_live.read_live_plan(root)
    delays = {"gpt-6-luna": 0.0, "gpt-6-sol": 0.02, "gpt-6-astra": 0.08}
    by_packet = {fixture["packet"]: fixture for fixture in fixtures.values()}
    harness = Harness(tmp_path / "homes", lambda model, effort: lambda packet: [
        ("sleep", delays[model]), *report_steps(by_packet[packet])])
    status = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 18
    starts = sorted(records(root, "attempt_started"), key=lambda record: record["data"]["dispatch_seq"])
    lane_of = {f"{row['assignment_id']}-live-1": f"{row['model']}-xhigh" for row in rows}
    pending = {(lane_of[f"{row['assignment_id']}-live-1"], row["round"]) for row in rows}
    pending = defaultdict(list)
    for row in rows:
        pending[lane_of[f"{row['assignment_id']}-live-1"]].append(row["round"])
    order = {}
    for record in starts:
        lane, round_index = lane_of[record["data"]["attempt_id"]], record["data"]["round"]
        assert all(min(rounds) > round_index - 2 for other, rounds in pending.items() if other != lane and rounds)
        pending[lane].remove(round_index)
        order[(lane, round_index)] = record["data"]["dispatch_seq"]
    # Without the barrier the fast lane would finish all six rounds before astra started its second.
    assert order[("gpt-6-luna-xhigh", 5)] > order[("gpt-6-astra-xhigh", 3)]


# N-a and N-c: settlement labels in resource summaries, and lane token headroom


async def test_bounded_charges_keep_their_label_and_a_lane_admits_its_last_row_after_overshoots(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    overshoot = lambda f: [READ, ("usage", 80000), ("stall", lambda: None)]  # noqa: E731
    special = {("gpt-6-luna", "xhigh", level, variant): overshoot
               for level, variant in (("L1", "violation"), ("L3", "violation"), ("L4", "twin"))}
    status = await run(root, plan, Harness(tmp_path / "homes", scripted(fixtures, special)),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)["lanes"]["gpt-6-luna-xhigh"]
    # Three bounded charges of 80,000 exceed three reservations; the fourth reservation of headroom admits them.
    assert report["ledger"]["settled_tokens"] == 240000 > 3 * 75000
    assert report["ledger"]["settled_tokens_by_usage_settlement"] == {"settled": 0, "bounded_by_reservation": 240000}
    assert {row["usage_settlement"] for row in report["resource_observations"]} == {"bounded_by_reservation"}
    lane_plan = read_sealed(root / "lanes" / "gpt-6-luna-xhigh" / "phase-plan.json")
    assert lane_plan["caps"]["collection_observed_token_stop_target"] == 4 * 75000
    with pytest.raises(ValueError, match="exceed its planned reservations"):
        v11_phase.create_lane_phase(tmp_path / "tight", {**{key: value for key, value in lane_plan.items()
                                                            if key != "seal_hash"},
                                                         "caps": {**lane_plan["caps"],
                                                                  "collection_observed_token_stop_target": 225000}})


# CLI: --study, --amendment, and abandon-root


async def test_the_cli_requires_the_study_and_abandons_a_registered_root(compat, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    study, _, _ = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    assert main(["verify", str(root)]) == 2
    assert "study directory" in json.loads(capsys.readouterr().out)["error"]
    assert main(["verify", str(root), "--study", str(study)]) == 0
    assert json.loads(capsys.readouterr().out)["study_registration"]["state"] == "finalized"
    assert main(["smoke", str(root), "--caps", str(tmp_path / "missing.json"), "--authorization",
                 str(tmp_path / "missing.json"), "--amendment", str(tmp_path / "missing.json")]) == 2
    assert "--amendment requires --study" in json.loads(capsys.readouterr().out)["error"]
    assert main(["abandon-root", str(study), "--plan-hash", plan["seal_hash"], "--root", str(root),
                 "--reason", "Rebuilt under another revision."]) == 0
    assert json.loads(capsys.readouterr().out)["prior_state"] == "finalized"
    assert [entry["state"] for entry in v11_live.registered_roots(study)] == ["abandoned"]
