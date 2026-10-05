"""Specification revision 3, live layer: the provider-overload pause and calibration arm selection. No model calls.

The fake transport replays the observed event sequence of the held calibration run: packet delivery, a thread
``systemError`` status, an ``error`` notification with ``codexErrorInfo: "serverOverloaded"``, a failed
``turn/completed``, and ``runtime/disconnected`` while the runtime closes, with no tool request, no output, and no
usage total. A ``PauseClock`` stands in for the wall clock, so a 10-minute pause takes a few dispatcher polls.
"""

import asyncio
import functools
import json
from pathlib import Path

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting import live as p1_live
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.lanes import (
    PROVIDER_PAUSE_SECONDS,
    ProviderPause,
    attempt_hold_kinds,
    validate_pause_record,
)
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
from swarm_auth_bench.peer_reporting_v11.resources import _ignore_reason

from .live_fakes import (
    Harness,
    PauseClock,
    authorization,
    build_plan,
    caps_record,
    fake_bundle,
    fake_version,
    overload_steps,
    qualified_root,
    study_rows,
    write_study,
)
from .live_fakes import run_phase as run
from .test_live_r2 import (
    NATIVE,
    READ,
    SERIAL,
    amendment,
    attempt_of,
    journal,
    payloads,
    records,
    scripted,
    smoke_study,
)

SOL_L1 = ("gpt-6-sol", "xhigh", "L1", "violation")
LUNA_L1 = ("gpt-6-luna", "xhigh", "L1", "violation")
ASTRA_L1 = ("gpt-6-astra", "xhigh", "L1", "violation")
INVALID_TURN = "turn result identity, status, or error invalid"


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("overload-compat"))[0]


def long_authorization(plan):
    """The fake wall clock advances through pauses and windows, so the cutoff and deadline lie far ahead."""
    return authorization(plan, cutoff=10**6, deadline=2 * 10**6)


def sealed_root(study, compat, *, caps=SERIAL, revision="smoke-v1", phase="smoke", **kwargs):
    root = Path(study) / "roots" / revision
    built = build_plan(phase, study, caps=caps, revision=revision, compatibility_directories=[compat], **kwargs)
    v11_live.prepare_live_root(root, built, study_directory=study, prior_roots=kwargs.get("prior_roots", ()))
    return root, v11_live.read_live_plan(root)


def started_at(clock, log):
    """A harness observer recording the wall-clock time of every session start."""
    return lambda kind, runtime: log.append((runtime.model, runtime.reasoning_effort, clock.now)) \
        if kind == "thread/start" else None


async def run_paused(root, plan, harness, clock, compat, study, **kwargs):
    return await run(root, plan, harness, auth=long_authorization(plan), wall_clock=clock, pause_sleep=clock.sleep,
                     compatibility_directories=[compat], study_directory=study, **kwargs)


def lane_records(root, lane, kind):
    return [record["data"] for record in journal(Path(root) / "lanes" / lane) if record["kind"] == kind]


# Revision 3: one overload pauses admission, then the run resumes


async def test_the_observed_overload_is_provider_unavailable_and_pauses_admission_then_resumes(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, sessions = PauseClock(), []
    harness = Harness(tmp_path / "homes", scripted(fixtures, {SOL_L1: lambda f: overload_steps()}),
                      observe=started_at(clock, sessions))
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    assert status["status"] == "complete" and status["holds"] == []
    assert status["live_model_call_starts"] == 12 and status["status_counts"] == {"archived": 12}
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    # The adapter classifies the capacity refusal instead of failing the turn result.
    assert result["termination_kind"] == "provider_unavailable" and result["infrastructure_failures"] == []
    assert result["provider_overload"]["code"] == "serverOverloaded"
    assert [error["code"] for error in result["provider_overload"]["error_notifications"]] == ["serverOverloaded"]
    assert result["tool_requests"] == [] and result["observer_outputs"] == [] and result["exposure_confirmed"] is True
    assert result["usage"]["total_tokens"] is None and result["usage"]["observed_total_tokens"] is None
    assert result["runtime_closed"] is True and result["queue_reconciled"] is True
    methods = [(event["event"].get("raw") or {}).get("method") for event in result["events"]
               if event["kind"] == "runtime_event"]
    assert methods[-3:] == ["error", "turn/completed", "runtime/disconnected"]
    assert "thread/status/changed" in methods
    assert any(event["kind"] == "provider_overload_observed" for event in result["events"])
    # Consumed, ineligible, settled at its reservation, and not an execution failure.
    check = payload["check"]
    assert check["classification"] == "provider_unavailable" and check["passed"] is False
    assert check["failure_reasons"] == ["valid_close"] and check["usage_bounded_by_reservation"] is True
    assert payload["orchestrator"]["usage_settlement"] == {
        "status": "bounded_by_reservation", "actual_tokens": 75000, "observed_tokens": 0,
        "reservation_tokens": 75000, "settlement_reason": "provider_unavailable"}
    pause = payload["orchestrator"]["provider_pause"]
    assert pause["attempt_id"] == attempt and pause["window_count"] == 1 and pause["holds_admission"] is False
    assert pause["resume_at"] - pause["paused_at"] == PROVIDER_PAUSE_SECONDS
    # The pause is journaled in the attempt's lane, and so is its end.
    assert lane_records(root, "gpt-6-sol-xhigh", "provider_pause_started") == [pause]
    (ended,) = lane_records(root, "gpt-6-sol-xhigh", "provider_pause_ended")
    assert ended["attempt_id"] == attempt and ended["ended_at"] >= pause["resume_at"]
    # Nothing started during the pause; the next attempt started after it, and the overloaded one ran once.
    after = [time for model, effort, time in sessions if time > pause["paused_at"]]
    assert after and min(after) >= pause["resume_at"] and len(clock.waits) == 10
    assert [record["data"]["attempt_id"] for record in records(root, "attempt_started")].count(attempt) == 1
    assert status["realized_order"].count(attempt) == 1
    assert status["provider_pauses"]["events"] == [pause] and status["provider_pauses"]["active"] is None
    # The lane report labels the settlement distinctly.
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    lane = report["lanes"]["gpt-6-sol-xhigh"]
    (row,) = [row for row in lane["entries"] if row["attempt_id"] == attempt]
    assert row["classification"] == "provider_unavailable" and row["settlement_reason"] == "provider_unavailable"
    assert lane["ledger"]["bounded_tokens_by_settlement_reason"] == {
        "observed_usage_after_clean_close": 0, "provider_unavailable": 75000, "provider_stalled": 0}
    assert report["provider_pauses"] == [pause] and report["unreconciled_starts"] == []
    # Export: consumed and excluded from analysis, not quarantined.
    export_live_review(root, tmp_path / "export", study_directory=study, bundle=fake_bundle())
    index = read_sealed(tmp_path / "export" / "index.json")
    exported = next(row for row in index["rows"] if row["assignment_id"] + v11_live.ATTEMPT_SUFFIX == attempt)
    assert exported["status"] == "archived" and exported["excluded_from_analysis"] is True
    assert exported["exclusion_reason"] == "provider_unavailable" and exported["evidence_error"] is None
    assert index["analysis_exclusions"] == [exported["assignment_id"]]
    sealed = read_sealed(tmp_path / "export" / exported["attempt_path"])
    assert sealed["eligible"] is False and sealed["excluded_from_analysis"] is True
    assert sealed["termination_kind"] == "provider_unavailable"
    assert sealed["usage"] == {"total_tokens": 75000, "observed_total_tokens": None,
                               "settlement": "bounded_by_reservation"}
    others = [row for row in index["rows"] if row is not exported]
    assert not any(row["excluded_from_analysis"] for row in others)
    # It is no valid smoke evidence: as with a stop truncation, the collection gate needs an amendment for it.
    gate = functools.partial(v11_live.smoke_evidence, root, bundle=fake_bundle(), source=plan["source"],
                             smoke_assignment_ids=sorted(row["assignment_id"] for row in rows),
                             study_directory=study, required_lanes=[lane["lane_id"] for lane in plan["lanes"]])
    evidence, failures = gate()
    assert evidence is None and failures == ["smoke: 11 of 12 smoke records are valid and 0 failed records are "
                                             "accepted by an amendment"]
    accepted = amendment(study, [attempt])
    v11_live.record_amendment(study, accepted, smoke_roots=[root], bundle=fake_bundle())
    evidence, failures = gate()
    assert failures == [] and evidence["accepted_failed_attempts"] == {attempt: [accepted["seal_hash"]]}


async def test_a_third_overload_within_an_hour_holds_and_a_resume_waits_out_the_window(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    special = {key: lambda f: overload_steps() for key in (LUNA_L1, SOL_L1, ASTRA_L1)}
    clock = PauseClock()
    start = clock.now
    status = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures, special)), clock, compat, study)
    luna, sol, astra = (attempt_of(rows, model, "L1", "violation")
                        for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"))
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    assert status["holds"] == [f"provider_unavailable_limit:{astra}"]
    pauses = {payload["attempt_id"]: payload["orchestrator"]["provider_pause"] for payload in payloads(root).values()}
    assert [pauses[attempt]["window_count"] for attempt in (luna, sol, astra)] == [1, 2, 3]
    assert [pauses[attempt]["holds_admission"] for attempt in (luna, sol, astra)] == [False, False, True]
    assert pauses[sol]["paused_at"] >= pauses[luna]["resume_at"]
    assert pauses[astra]["paused_at"] - start < 3600
    assert all(payload["check"]["classification"] == "provider_unavailable" for payload in payloads(root).values())
    # A resumed run inside the window holds from its start and starts nothing.
    clock.now += 600
    held = await run_paused(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), clock, compat, study)
    assert held["status"] == "held" and held["holds"] == [f"retained_provider_unavailable_limit:{astra}"]
    assert held["live_model_call_starts"] == 3 and held["realized_order"] == []
    # Once the window no longer holds three refusals, the run resumes and finishes the other rows.
    clock.now = pauses[astra]["paused_at"] + 3600 + 1
    resumed = await run_paused(root, plan, Harness(tmp_path / "h3", scripted(fixtures)), clock, compat, study)
    assert resumed["status"] == "complete" and resumed["holds"] == []
    assert resumed["live_model_call_starts"] == 12 and len(resumed["realized_order"]) == 9
    assert {luna, sol, astra}.isdisjoint(resumed["realized_order"])
    ended = [record["data"]["attempt_id"] for record in records(root, "provider_pause_ended")]
    assert sorted(ended) == sorted([luna, sol, astra])
    starts = [record["data"]["attempt_id"] for record in records(root, "attempt_started")]
    assert len(starts) == len(set(starts)) == 12


@pytest.mark.parametrize("case", ["after_tool_request", "after_output", "other_code", "retry_announced",
                                  "no_packet_delivery"])
async def test_every_other_overload_or_error_stays_an_execution_failure(compat, tmp_path, case):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    steps = {"after_tool_request": [READ, *overload_steps()],
             "after_output": [("message", "Checking the records."), *overload_steps()],
             "other_code": overload_steps("usageLimitExceeded"),
             "retry_announced": overload_steps(will_retry=True),
             "no_packet_delivery": [("no_packet_delivery",), *overload_steps()]}[case]
    clock = PauseClock()
    status = await run_paused(root, plan, Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps})),
                              clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    assert f"execution_check_failure:{attempt}" in status["holds"]
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    expected = "initial exposure unverified" if case == "no_packet_delivery" else INVALID_TURN
    assert expected in result["infrastructure_failures"]
    assert payload["check"]["classification"] is None
    assert payload["orchestrator"]["provider_pause"] is None and payload["orchestrator"]["usage_settlement"][
        "status"] == "unresolved"
    assert records(root, "provider_pause_started") == [] and clock.waits == []


# Restarts, crashes, and the post-preflight check


@pytest.mark.parametrize("interruption", ["soft_stop", "crash"])
async def test_a_restart_during_a_pause_respects_it_and_never_reruns_the_attempt(compat, tmp_path, interruption):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)

    def interrupt(clock):
        clock.on_wait = None
        if interruption == "soft_stop":
            (root / v11_live.STOP_FILE).touch()
        else:
            raise RuntimeError("simulated coordinator crash during the pause")

    clock = PauseClock(on_wait=interrupt)
    first = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures, {SOL_L1: lambda f: overload_steps()})),
                             clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    pause = payloads(root)[attempt]["orchestrator"]["provider_pause"]
    assert first["status"] == "held" and first["live_model_call_starts"] == 2
    assert first["holds"] == (["soft_stop"] if interruption == "soft_stop" else
                              ["coordinator_error:RuntimeError:simulated coordinator crash during the pause"])
    assert clock.now < pause["resume_at"] and records(root, "provider_pause_ended") == []
    (root / v11_live.STOP_FILE).unlink(missing_ok=True)
    sessions = []
    resumed = await run_paused(root, plan, Harness(tmp_path / "h2", scripted(fixtures),
                                                   observe=started_at(clock, sessions)), clock, compat, study)
    assert resumed["status"] == "complete" and resumed["holds"] == []
    assert resumed["live_model_call_starts"] == 12 and attempt not in resumed["realized_order"]
    assert sessions and min(time for _, _, time in sessions) >= pause["resume_at"]
    # One interrupted poll (a crash raises before the clock moves), then polls until the pause ended.
    assert len(clock.waits) == (10 if interruption == "soft_stop" else 11)
    assert [record["data"]["attempt_id"] for record in records(root, "attempt_started")].count(attempt) == 1
    assert [record["data"]["attempt_id"] for record in records(root, "provider_pause_ended")] == [attempt]


async def test_a_crash_before_the_pause_is_journaled_recovers_it_from_the_sealed_attempt(compat, tmp_path,
                                                                                         monkeypatch):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    original = p1_live._Journal.append

    def crashing(self, kind, **data):
        if kind == "provider_pause_started":
            raise OSError("simulated crash between the archive and its pause record")
        return original(self, kind, **data)

    clock = PauseClock()
    with monkeypatch.context() as patched:
        patched.setattr(p1_live._Journal, "append", crashing)
        crashed = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures,
                                                                                  {SOL_L1: lambda f: overload_steps()})),
                                   clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    assert crashed["status"] == "held" and crashed["live_model_call_starts"] == 2
    assert records(root, "provider_pause_started") == []
    pause = payloads(root)[attempt]["orchestrator"]["provider_pause"]
    # The interrupted index holds the next run as an unreconciled start; that run reconciles it and journals the
    # sealed pause.
    reconciling = await run_paused(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), clock, compat, study)
    assert reconciling["status"] == "held" and f"retained_unreconciled_start:{attempt}" in reconciling["holds"]
    (recovered,) = [record["data"] for record in records(root, "provider_pause_started")]
    assert recovered == {**pause, "recovered": True}
    sessions = []
    resumed = await run_paused(root, plan, Harness(tmp_path / "h3", scripted(fixtures),
                                                   observe=started_at(clock, sessions)), clock, compat, study)
    assert resumed["status"] == "complete" and resumed["live_model_call_starts"] == 12
    assert min(time for _, _, time in sessions) >= pause["resume_at"] and clock.waits
    assert [record["data"]["attempt_id"] for record in records(root, "attempt_started")].count(attempt) == 1


async def test_a_pause_that_begins_during_preflight_refuses_the_start_and_offers_it_again(compat, tmp_path):
    caps = caps_record()  # six global slots: every lane starts at once
    study, rows, fixtures = smoke_study(tmp_path / "study", caps=caps)
    root, plan = sealed_root(study, compat, caps=caps)
    luna, sol = attempt_of(rows, "gpt-6-luna", "L1", "violation"), attempt_of(rows, "gpt-6-sol", "L1", "violation")
    luna_archive = root / "lanes" / "gpt-6-luna-xhigh" / "attempts" / luna / "attempt.json"
    base = functools.partial(v11_live.manifest_preflight, version_reader=fake_version)
    held = {"done": False}

    async def preflight(runtime, model, caps_value, **kwargs):
        if (model, kwargs["reasoning_effort"]) == ("gpt-6-sol", "xhigh") and not held["done"]:
            held["done"] = True
            for _ in range(2000):  # sol's first preflight ends only after luna's refusal was archived
                if luna_archive.exists():
                    break
                await asyncio.sleep(0.005)
        return await base(runtime, model, caps_value, **kwargs)

    clock = PauseClock()
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: overload_steps()}))
    status = await run_paused(root, plan, harness, clock, compat, study, preflight=preflight)
    assert status["status"] == "complete" and status["holds"] == [] and status["live_model_call_starts"] == 12
    assert status["realized_order"].count(sol) == 1
    sol_lane = [record for record in journal(root / "lanes" / "gpt-6-sol-xhigh")
                if record["data"].get("entry_id") == sol[:-len(v11_live.ATTEMPT_SUFFIX)]]
    kinds = [record["kind"] for record in sol_lane]
    assert kinds.count("preflight_passed") == 2 and kinds.count("attempt_started") == 1
    (paused,) = [record["data"] for record in sol_lane if record["kind"] == "admission_paused"]
    assert paused["paused_by_attempt_id"] == luna and paused["after_preflight"] is True
    assert kinds.index("admission_paused") < kinds.index("attempt_started")
    sol_runtimes = [runtime for runtime in harness.created if runtime.thread_id == "thread-gpt-6-sol-xhigh"]
    assert "thread/start" not in sol_runtimes[0].methods  # the refused start never opened a session
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["lanes"]["gpt-6-sol-xhigh"]["halted"] is None
    starts = [record["data"]["attempt_id"] for record in records(root, "attempt_started")]
    assert len(starts) == len(set(starts)) == 12


# The pause rule and its consumers


def test_the_pause_rule_counts_refusals_in_any_sixty_minute_window():
    clock = PauseClock(start=1000.0)
    pauses = ProviderPause(wall_clock=clock)
    first = pauses.record("a-live-1", "lane")
    assert (first["window_count"], first["holds_admission"], first["resume_at"]) == (1, False, 1600.0)
    assert pauses.active()["paused_by_attempt_id"] == "a-live-1" and pauses.active()["remaining_seconds"] == 600
    clock.now = 1600.0
    assert pauses.active() is None and [event["attempt_id"] for event in pauses.expired_unended()] == ["a-live-1"]
    clock.now = 4599.0
    assert pauses.record("b-live-1", "lane")["window_count"] == 2
    clock.now = 4600.0  # the first refusal has left the window
    assert pauses.record("c-live-1", "lane")["window_count"] == 2
    clock.now = 4601.0
    third = pauses.record("d-live-1", "lane")
    assert (third["window_count"], third["holds_admission"]) == (3, True)
    with pytest.raises(ValueError, match="already paused"):
        pauses.record("d-live-1", "lane")
    restored = ProviderPause(wall_clock=clock)
    restored.restore([{**event, "recovered": True} for event in pauses.events], ended=["a-live-1"])
    assert restored.events == pauses.events and restored.limit_reached() == {"attempt_id": "d-live-1",
                                                                             "window_count": 3}
    assert restored.active()["paused_by_attempt_id"] == "d-live-1"
    clock.now = 4599.0 + 3600
    assert restored.limit_reached() is None
    for change in ({"window_count": 2}, {"holds_admission": False}, {"resume_at": third["paused_at"] + 60}):
        with pytest.raises(ValueError):
            validate_pause_record({**third, **change})


def test_provider_unavailable_holds_nothing_and_resource_proposals_ignore_it():
    assert attempt_hold_kinds(False, ["valid_close"], "bounded_by_reservation", "provider_unavailable") == []
    assert attempt_hold_kinds(False, ["valid_close"], "unresolved", "provider_unavailable") == [
        "execution_check_failure", "unknown_final_usage"]
    assert attempt_hold_kinds(False, ["valid_close"], "bounded_by_reservation", None) == ["execution_check_failure"]
    row = {"status": "archived", "classification": "provider_unavailable", "usage_settlement": "settled",
           "usage_total_tokens": 75000, "elapsed_seconds": 7.0}
    assert _ignore_reason(row) == "classification:provider_unavailable"
    assert _ignore_reason({**row, "classification": None}) is None


# Calibration arm selection


EXTENSION = ["calibration_extension_low", "calibration_extension_xhigh"]
CALIBRATION_ARMS = (
    ("calibration", (("L1", "violation", "guided", "xhigh", "normal"), ("L2", "twin", "guided", "xhigh", "normal"))),
    ("calibration_extension_xhigh", (("L2", "violation", "neutral", "xhigh", "normal"),
                                     ("L3", "twin", "discouraged", "xhigh", "normal"))),
    ("calibration_extension_low", (("L2", "violation", "neutral", "low", "normal"),
                                   ("L4", "twin", "neutral", "low", "normal"))),
)


def calibration_study(directory):
    """Six calibration rows per arm: the original arm and both revision 3 extension arms, in one split."""
    rows, fixtures = [], {}
    for arm_index, (arm, cells) in enumerate(CALIBRATION_ARMS):
        arm_rows, arm_fixtures = study_rows("calibration", cells=cells, template_id="release-request", seed=1102)
        for row in arm_rows:
            row.update(arm=arm, assignment_id=f"ca-{content_hash([arm, row['assignment_id']])[:20]}",
                       round=row["round"] + 2 * arm_index)
        rows += arm_rows
        fixtures.update(arm_fixtures)
    for position, row in enumerate(rows):
        row["planned_order"] = position
    return write_study(directory, rows, fixtures, caps=SERIAL), rows, fixtures


def plan_arms(built):
    _, lanes, _ = built
    return {entry["arm"] for lane in lanes.values() for entry in lane["planned_order"]}


async def test_a_calibration_root_selects_arms_and_other_phases_refuse_a_selection(compat, tmp_path):
    study, rows, fixtures = calibration_study(tmp_path / "study")
    everything = build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat])
    assert everything[0]["selected_arms"] is None and everything[0]["maximum_live_calls"] == 18
    extension = build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat],
                           arms=list(reversed(EXTENSION)))
    assert extension[0]["selected_arms"] == EXTENSION and plan_arms(extension) == set(EXTENSION)
    assert extension[0]["maximum_live_calls"] == 12
    lanes = sorted(lane["lane_id"] for lane in extension[0]["lanes"])
    assert lanes == sorted(f"{model}-{effort}" for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")
                           for effort in ("xhigh", "low"))
    assert sorted(extension[0]["gate_evidence"]["qualification"]) == lanes  # the low lanes need qualification too
    xhigh_only = build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat],
                            arms=["calibration_extension_xhigh"])
    assert {lane["reasoning_effort"] for lane in xhigh_only[0]["lanes"]} == {"xhigh"}
    with pytest.raises(ValueError, match="no calibration rows of arms"):
        build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat], arms=["collection"])
    for arms in ([], ["calibration", "calibration"], "calibration"):
        with pytest.raises(ValueError, match="one or more distinct arm names"):
            build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat], arms=arms)
    smoke, _, _ = smoke_study(tmp_path / "smoke-study")
    for phase in ("smoke", "collection"):
        with pytest.raises(ValueError, match="only a calibration root may select arms"):
            build_plan(phase, smoke, caps=SERIAL, compatibility_directories=[compat], arms=["smoke"])
    with pytest.raises(ValueError, match="only a calibration root may select arms"):
        v11_live.build_phase_plan("compatibility", SERIAL, revision="c", bundle=fake_bundle(), arms=["calibration"])

    # The held original arm stays consumed; the extension runs without repeating it, and a later root finishes it.
    original, original_plan = sealed_root(study, compat, phase="calibration", revision="calibration-v1",
                                          arms=["calibration"])
    failing = scripted(fixtures, {LUNA_L1: lambda f: [NATIVE]})
    held = await run(original, original_plan, Harness(tmp_path / "h1", failing), compatibility_directories=[compat],
                     study_directory=study)
    assert held["status"] == "held" and held["live_model_call_starts"] == 1
    consumed = [record["data"]["attempt_id"] for record in records(original, "attempt_started")]
    root, plan = sealed_root(study, compat, phase="calibration", revision="calibration-ext-v1", arms=EXTENSION,
                             prior_roots=[original])
    assert plan["selected_arms"] == EXTENSION and plan["consumed_attempts"]["consumed_attempt_ids"] == consumed
    assert plan["consumed_attempts"]["excluded_assignment_ids"] == []
    status = await run(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), compatibility_directories=[compat],
                       study_directory=study, prior_roots=[original])
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study, prior_roots=[original])
    assert report["selected_arms"] == EXTENSION
    export_live_review(root, tmp_path / "export", study_directory=study, prior_roots=[original], bundle=fake_bundle())
    index = read_sealed(tmp_path / "export" / "index.json")
    assert index["selected_arms"] == EXTENSION and {row["arm"] for row in index["rows"]} == set(EXTENSION)
    finish, finish_plan = sealed_root(study, compat, phase="calibration", revision="calibration-v2",
                                      arms=["calibration"], prior_roots=[original, root])
    assert finish_plan["consumed_attempts"]["excluded_assignment_ids"] == [consumed[0][:-len(v11_live.ATTEMPT_SUFFIX)]]
    assert finish_plan["maximum_live_calls"] == 5
    prior = [original, root, finish]
    with pytest.raises(ValueError, match="in the selected arms is already consumed"):
        build_plan("calibration", study, caps=SERIAL, revision="calibration-ext-v2", compatibility_directories=[compat],
                   arms=["calibration_extension_low"], prior_roots=prior)
    with pytest.raises(ValueError, match=r"selected arms \['calibration_extension_low'\] is already consumed"):
        build_plan("calibration", study, caps=SERIAL, revision="calibration-v3", compatibility_directories=[compat],
                   arms=["calibration", "calibration_extension_low"], prior_roots=prior)


def test_a_closed_arm_is_never_planned_and_its_split_needs_an_explicit_selection(compat, tmp_path):
    rows, fixtures = [], {}
    for arm_index, (arm, cells) in enumerate(CALIBRATION_ARMS):
        arm_rows, arm_fixtures = study_rows("calibration", cells=cells, template_id="release-request", seed=1102)
        for row in arm_rows:
            row.update(arm=arm, assignment_id=f"cl-{content_hash([arm, row['assignment_id']])[:20]}",
                       round=row["round"] + 2 * arm_index)
        rows += arm_rows
        fixtures.update(arm_fixtures)
    for position, row in enumerate(rows):
        row["planned_order"] = position
    study = write_study(tmp_path / "study", rows, fixtures, caps=SERIAL, protocol={"closed_arms": ["calibration"]})
    with pytest.raises(ValueError, match="select the open arms"):
        build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat])
    with pytest.raises(ValueError, match="are closed and never run again"):
        build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat],
                   arms=["calibration", "calibration_extension_low"])
    extension = build_plan("calibration", study, caps=SERIAL, compatibility_directories=[compat], arms=EXTENSION)
    assert plan_arms(extension) == set(EXTENSION)


def test_verify_and_export_refuse_a_selection_that_differs_from_the_planned_rows():
    plan = {"phase": "calibration", "selected_arms": ["calibration_extension_xhigh"]}
    assert v11_live.check_arm_selection(plan, {"calibration_extension_xhigh"}) == ["calibration_extension_xhigh"]
    assert v11_live.check_arm_selection({"phase": "smoke", "selected_arms": None}, {"smoke"}) is None
    with pytest.raises(v11_live.EvidenceError, match="not its selected arms"):
        v11_live.check_arm_selection(plan, {"calibration_extension_xhigh", "calibration"})
    with pytest.raises(v11_live.EvidenceError, match="only a calibration root"):
        v11_live.check_arm_selection({**plan, "phase": "smoke"}, {"calibration_extension_xhigh"})
    with pytest.raises(v11_live.EvidenceError, match="not sorted"):
        v11_live.check_arm_selection({**plan, "selected_arms": EXTENSION[::-1]}, set(EXTENSION))


def test_the_cli_build_takes_repeatable_arms_and_refuses_them_outside_calibration(compat, tmp_path, monkeypatch,
                                                                                 capsys):
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    study, _, _ = smoke_study(tmp_path / "study")
    caps = tmp_path / "caps.json"
    caps.write_bytes(json.dumps(SERIAL).encode("utf-8"))
    for phase in ("smoke", "collection", "compatibility"):
        code = main(["build", str(study / "roots" / phase), "--phase", phase, "--caps", str(caps), "--revision",
                     f"{phase}-v1", "--study", str(study), "--compatibility", str(compat), "--arm", "smoke",
                     "--arm", "calibration"])
        assert code == 2
        assert "only a calibration root may select arms" in json.loads(capsys.readouterr().out)["error"]
    assert not (study / "roots" / "smoke").exists() and v11_live.registered_roots(study) == []
