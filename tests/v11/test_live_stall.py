"""Specification revision 4, live layer: the silent provider stall, and the Astra review of revision 3, round 2.

The fake transport replays the observed stall of the revision 3 calibration extension: the packet's user-message
item is delivered, then no model event of any kind arrives until the trial wall closes the attempt, and the
controller's interrupt ends the turn. A step that jumps the adapter's monotonic clock past the trial wall stands in
for the 360 s of silence; a ``PauseClock`` stands in for the wall clock of the provider pause.

R1: a pause whose classification is durable in a lane journal but missing at study level is recorded before any
admission, so a successor root that selects another arm waits for it and counts it.
R2: a clean overload whose only notification arrives at the last drain is still classified as an overload.
R3: the study pause verifier checks the recorded root path against the claim and the registry.
No model calls.
"""

import asyncio
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting import live as p1_live
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.lanes import (
    PROVIDER_PAUSE_SECONDS,
    attempt_hold_kinds,
)
from swarm_auth_bench.peer_reporting_v11.resources import _ignore_reason

from .live_fakes import (
    OVERLOAD_MESSAGE,
    Harness,
    PauseClock,
    authorization,
    compat_fixture,
    compat_root,
    fake_bundle,
    overload_steps,
    qualified_root,
    qualifier_steps,
)
from .live_fakes import run_phase as run
from .test_live_astra_r3 import LateHarness, export_row, first_attempt_overloads, sealed_pause
from .test_live_overload import (
    ASTRA_L1,
    INVALID_TURN,
    LUNA_L1,
    SOL_L1,
    calibration_study,
    lane_records,
    long_authorization,
    run_paused,
    sealed_root,
    started_at,
)
from .test_live_r2 import SERIAL, attempt_of, payloads, records, scripted, smoke_study

LUNA_LOW = ("gpt-6-luna", "low", "L1", "violation")
PROVIDER_ERROR = "non-retryable provider error notification"


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("stall-compat"))[0]


class TrialClock:
    """The adapter's monotonic clock, standing in for 360 s of provider silence.

    The fake transport's ``stall`` step calls ``silence``. Once the controller of the stalled attempt has confirmed
    the packet's delivery, the next poll of any adapter or coordinator sleep jumps the clock past any trial wall, so
    the delivery precedes the silence, as it did 3.8 s into the observed attempt. Runs that stall use one global
    slot, so the newest runtime of ``harness`` is the stalled one.
    """

    def __init__(self):
        self.now = 0.0
        self.silent = False
        self.harness = None

    def __call__(self):
        return self.now

    def silence(self):
        self.silent = True

    def delivered(self):
        runtime = self.harness.created[-1]
        controller = runtime._peer_controllers.get(runtime.thread_id)
        return controller is not None and controller.exposure_receipt is not None

    async def sleep(self, seconds):
        await asyncio.sleep(min(seconds, 0.01))
        if self.silent and self.delivered():
            self.silent = False
            self.now += 10_000


def stall_steps(trial_clock, model="gpt-6-luna", effort="low"):
    """The observed silent stall (guest, luna low): the thread settings, the active status and the turn start, then
    silence until the trial wall."""
    turn = {"id": f"turn-{model}-{effort}", "status": "inProgress", "items": [],
            "itemsView": "notLoaded", "error": None}
    settings = {"threadSettings": {"approvalPolicy": "never", "collaborationMode": {"mode": "default", "settings": {
        "developer_instructions": None, "model": model, "reasoning_effort": effort}}}}
    return [("raw_thread", "thread/settings/updated", settings),
            ("thread_status", {"type": "active", "activeFlags": []}),
            ("raw_thread", "turn/started", {"turn": turn}), ("packet_delivery",),
            ("interrupted_end", {"items": [], "itemsView": "notLoaded", "error": None}, {"type": "idle"}),
            ("disconnect_on_close",), ("stall", trial_clock.silence)]


async def run_stalled(root, plan, harness, clock, trial_clock, compat, study, **kwargs):
    """``run_paused`` with the adapter's monotonic clock as well: ``clock`` is the wall clock of the pause."""
    trial_clock.harness = harness
    return await run(root, plan, harness, auth=long_authorization(plan), wall_clock=clock, pause_sleep=clock.sleep,
                     compatibility_directories=[compat], study_directory=study, clock=trial_clock,
                     sleep=trial_clock.sleep, **kwargs)


# Revision 4: the observed silent stall is provider_stalled, treated exactly like provider_unavailable


async def test_the_observed_silent_stall_is_provider_stalled_and_pauses_admission_then_resumes(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock, sessions = PauseClock(), TrialClock(), []
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_LOW: lambda f: stall_steps(trial_clock)}),
                      observe=started_at(clock, sessions))
    status = await run_stalled(root, plan, harness, clock, trial_clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation", effort="low")
    assert status["status"] == "complete" and status["holds"] == []
    assert status["live_model_call_starts"] == 12 and status["status_counts"] == {"archived": 12}
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    # Delivery confirmed, then silence until the trial wall closed it: classified, not left unresolved.
    assert result["termination_kind"] == "provider_stalled" and result["infrastructure_failures"] == []
    assert result["boundary"]["termination_kind"] == "per_trial_limit"
    assert result["boundary"]["reason"] == "trial_wall_limit"
    assert result["exposure_confirmed"] is True and result["provider_overload"] is None
    assert result["tool_requests"] == [] and result["observer_outputs"] == []
    assert result["usage"]["total_tokens"] is None and result["usage"]["observed_total_tokens"] is None
    assert result["runtime_closed"] is True and result["queue_reconciled"] is True
    stall = result["provider_stall"]
    assert stall["boundary_reason"] == "trial_wall_limit" and stall["turn_status"] == "interrupted"
    assert stall["silent_seconds"] >= plan["caps"]["trial"]["max_trial_wall_seconds"]
    assert stall["runtime_event_methods"] == ["thread/settings/updated", "thread/status/changed", "turn/started",
                                              "item/started", "item/completed", "thread/status/changed",
                                              "turn/completed", "runtime/disconnected"]
    raw = [event["raw"] for event in result["raw_events"]]
    assert set(raw[0]["params"]) == {"threadId", "threadSettings"}
    assert raw[1]["params"]["status"] == {"type": "active", "activeFlags": []}
    assert raw[5]["params"]["status"] == {"type": "idle"}
    for index, turn_status in ((2, "inProgress"), (6, "interrupted")):
        assert raw[index]["params"]["turn"] == {"id": "turn-gpt-6-luna-low", "status": turn_status,
                                                "items": [], "itemsView": "notLoaded", "error": None}
    assert (stall["tool_requests"], stall["observer_outputs"], stall["usage_notifications"],
            stall["error_notifications"]) == (0, 0, 0, 0)
    assert [event for event in result["events"] if event["kind"] == "provider_stall_observed"]
    # Consumed, ineligible, settled at its reservation, and not an execution failure.
    check = payload["check"]
    assert check["classification"] == "provider_stalled" and check["passed"] is False
    assert check["failure_reasons"] == ["valid_close"] and check["usage_bounded_by_reservation"] is True
    assert payload["orchestrator"]["usage_settlement"] == {
        "status": "bounded_by_reservation", "actual_tokens": 75000, "observed_tokens": 0,
        "reservation_tokens": 75000, "settlement_reason": "provider_stalled"}
    (settled,) = [record for record in lane_records(root, "gpt-6-luna-low", "usage_settled")
                  if record["attempt_id"] == attempt]
    assert settled["settlement_reason"] == "provider_stalled"
    # Admission paused for 10 minutes, recorded at study level and journaled in the attempt's lane.
    pause = payload["orchestrator"]["provider_pause"]
    assert pause["attempt_id"] == attempt and pause["window_count"] == 1 and pause["holds_admission"] is False
    assert pause["resume_at"] - pause["paused_at"] == PROVIDER_PAUSE_SECONDS
    assert lane_records(root, "gpt-6-luna-low", "provider_pause_started") == [pause]
    assert [record["attempt_id"] for record in lane_records(root, "gpt-6-luna-low", "provider_pause_ended")] == [
        attempt]
    assert v11_live.study_provider_pauses(study)[attempt]["pause"] == pause
    after = [time for _, _, time in sessions if time > pause["paused_at"]]
    assert after and min(after) >= pause["resume_at"] and clock.waits
    assert [record["data"]["attempt_id"] for record in records(root, "attempt_started")].count(attempt) == 1
    # The lane report labels the settlement, and the resource proposal ignores the attempt.
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    lane = report["lanes"]["gpt-6-luna-low"]
    (row,) = [row for row in lane["entries"] if row["attempt_id"] == attempt]
    assert row["classification"] == "provider_stalled" and row["settlement_reason"] == "provider_stalled"
    assert row["termination_kind"] == "provider_stalled"
    assert lane["ledger"]["bounded_tokens_by_settlement_reason"] == {
        "observed_usage_after_clean_close": 0, "provider_unavailable": 0, "provider_stalled": 75000}
    assert _ignore_reason(row) == "classification:provider_stalled"
    assert report["study_provider_pauses"] == [pause] and report["unreconciled_starts"] == []
    # Export: consumed and excluded from analysis, and the scored summary leaves it out.
    index, exported = export_row(root, study, tmp_path / "export", attempt)
    assert exported["status"] == "archived" and exported["excluded_from_analysis"] is True
    assert exported["exclusion_reason"] == "provider_stalled" and exported["evidence_error"] is None
    assert index["analysis_exclusions"] == [exported["assignment_id"]] and index["analysis_exclusion_count"] == 1
    sealed = read_sealed(tmp_path / "export" / exported["attempt_path"])
    assert sealed["eligible"] is False and sealed["termination_kind"] == "provider_stalled"
    assert exported["score"]["eligibility"]["eligible"] is False
    assert index["summary"]["trial_count"] == 11
    for grouping in index["summary"]["groupings"]:
        assert sum(cell["trial_count"] for cell in grouping["cells"]) == 11
    assert not any(row["excluded_from_analysis"] for row in index["rows"] if row is not exported)


@pytest.mark.parametrize("case", ["reasoning", "usage_with_total", "usage_without_total", "retry_error",
                                  "system_error_status", "active_flags", "turn_started_error",
                                  "unknown_notification", "hard_stop", "forced_deadline"])
async def test_any_model_event_or_a_stop_leaves_silence_to_the_existing_rules(compat, tmp_path, case):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock = PauseClock(), TrialClock()
    silence = ("stall", trial_clock.silence)
    steps = {
        "reasoning": [("raw", "item/reasoning/summaryTextDelta", {"itemId": "rs-1", "delta": "Checking the grants.",
                                                                  "summaryIndex": 0}), silence],
        "usage_with_total": [("usage", 1500), silence],
        "usage_without_total": [("raw", "thread/tokenUsage/updated", {"tokenUsage": {}}), silence],
        "retry_error": [("raw", "error", {"error": {"message": "Reconnecting.", "codexErrorInfo": "serverOverloaded"},
                                          "willRetry": True}), silence],
        "system_error_status": [("thread_status", {"type": "systemError"}), silence],
        "active_flags": [("thread_status", {"type": "active", "activeFlags": ["waitingOnApproval"]}), silence],
        "turn_started_error": [("raw", "turn/started", {"turn": {
            "id": "turn-gpt-6-luna-xhigh", "status": "inProgress", "items": [],
            "error": {"message": "lost", "codexErrorInfo": "streamDisconnected"}}}), silence],
        "unknown_notification": [("raw", "unknown/notification", {}), silence],
        "hard_stop": [("stall", lambda: (root / v11_live.HARD_STOP_FILE).touch())],
        "forced_deadline": [("stall", lambda: setattr(clock, "now", clock.now + 3 * 10**6))],
    }[case]
    status = await run_stalled(root, plan, Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps})),
                               clock, trial_clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["termination_kind"] != "provider_stalled" and result["provider_stall"] is None
    assert not [event for event in result["events"] if event["kind"] == "provider_stall_observed"]
    assert payload["orchestrator"]["provider_pause"] is None and payload["check"]["classification"] != "provider_stalled"
    assert v11_live.study_provider_pauses(study) == {} and clock.waits == []
    settlement = payload["orchestrator"]["usage_settlement"]
    if case == "usage_with_total":  # observed usage after a clean close: a valid, eligible trial at the wall
        assert status["status"] == "complete" and status["holds"] == []
        assert result["termination_kind"] == "per_trial_limit" and payload["check"]["passed"] is True
        assert settlement == {"status": "bounded_by_reservation", "actual_tokens": 75000, "observed_tokens": 1500,
                              "reservation_tokens": 75000}
        return
    # Usage was never observed (or a notification lacked a total): unresolved, and it holds all admission.
    assert status["status"] == "held" and f"unknown_final_usage:{attempt}" in status["holds"]
    assert settlement["status"] == "unresolved"
    if case in {"hard_stop", "forced_deadline"}:  # a stop truncation, not a silent stall
        assert result["termination_kind"] == "collection_forced_truncation"
        assert payload["check"]["classification"] == "stop_truncation"
        assert ("hard_stop" if case == "hard_stop" else "forced_stop_deadline") in status["holds"]
    else:
        assert result["termination_kind"] == "per_trial_limit" and result["infrastructure_failures"] == []
        assert result["boundary"]["reason"] == "trial_wall_limit"
        assert payload["check"]["classification"] is None


@pytest.mark.parametrize("notification", ["turn/started", "turn/completed", "late_completion"])
@pytest.mark.parametrize("item_type", ["agentMessage", "reasoning"])
async def test_embedded_model_items_mark_the_stall_trace_incomplete(compat, tmp_path, notification, item_type):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock = PauseClock(), TrialClock()
    item = {"id": "embedded-output", "type": item_type, **(
        {"text": "I found a policy concern."} if item_type == "agentMessage"
        else {"summary": [{"type": "summaryText", "text": "Checking the grants."}]})}
    steps = stall_steps(trial_clock, "gpt-6-luna", "xhigh")
    if notification == "turn/started":
        steps[2][2]["turn"]["items"] = [item]
    elif notification == "turn/completed":
        steps[4][1]["items"] = [item]
    else:
        steps.insert(0, ("raw_on_close", "turn/completed", {"turn": {
            "id": "turn-gpt-6-luna-xhigh", "status": "interrupted", "items": [item], "error": None}}))
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}))
    status = await run_stalled(root, plan, harness, clock, trial_clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_stall"] is None
    assert result["infrastructure_failures"] == ["unreconciled items in turn lifecycle notification"]
    assert result["queue_reconciled"] is False and result["world_checkpoint"] is None
    assert any(item in (event["raw"]["params"].get("turn") or {}).get("items", [])
               for event in result["raw_events"])
    assert payload["check"]["classification"] is None and payload["check"]["passed"] is False
    assert f"execution_check_failure:{attempt}" in status["holds"]
    assert status["status"] == "held" and f"unknown_final_usage:{attempt}" in status["holds"]
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    assert payload["orchestrator"]["provider_pause"] is None and v11_live.study_provider_pauses(study) == {}
    assert clock.waits == []


@pytest.mark.parametrize("change", [
    {"error": {"message": "lost", "codexErrorInfo": "streamDisconnected"}},
    {"status": "completed"}, {"id": "another-turn"}, {"threadId": "another-thread"}, {},
], ids=["stream_disconnected", "status", "turn_id", "thread_id", "matching_duplicate"])
async def test_every_late_completion_is_reconciled_with_the_settled_turn(compat, tmp_path, change):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock = PauseClock(), TrialClock()
    turn = {"id": "turn-gpt-6-luna-xhigh", "status": "interrupted", "items": [], "error": None,
            **{key: value for key, value in change.items() if key != "threadId"}}
    if not change:  # absent items and error are as silent as an empty list and null error
        turn.pop("items")
        turn.pop("error")
    late = ("raw_on_close", "turn/completed", {"turn": turn,
                                              **({"threadId": change["threadId"]} if "threadId" in change else {})})
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: [
        late, *stall_steps(trial_clock, "gpt-6-luna", "xhigh")]}))
    status = await run_stalled(root, plan, harness, clock, trial_clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    completions = [event["raw"] for event in result["raw_events"]
                   if event["raw"]["method"] == "turn/completed"]
    assert len(completions) == 2 and completions[-1]["params"]["turn"] == turn
    if not change:
        assert status["status"] == "complete" and status["holds"] == []
        assert result["termination_kind"] == "provider_stalled" and result["infrastructure_failures"] == []
        assert result["provider_stall"]["runtime_event_methods"].count("turn/completed") == 2
        return
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_stall"] is None
    assert result["infrastructure_failures"] == ["terminal notification contradicts settled turn"]
    assert payload["check"]["classification"] is None and payload["check"]["passed"] is False
    assert f"execution_check_failure:{attempt}" in status["holds"]
    assert status["status"] == "held" and f"unknown_final_usage:{attempt}" in status["holds"]
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    assert payload["orchestrator"]["provider_pause"] is None and v11_live.study_provider_pauses(study) == {}
    assert clock.waits == []


async def test_stalls_and_overloads_share_one_limit_and_a_resume_waits_out_the_window(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock = PauseClock(), TrialClock()
    special = {LUNA_L1: lambda f: stall_steps(trial_clock, "gpt-6-luna", "xhigh"), SOL_L1: lambda f: overload_steps(),
               ASTRA_L1: lambda f: stall_steps(trial_clock, "gpt-6-astra", "xhigh")}
    start = clock.now
    status = await run_stalled(root, plan, Harness(tmp_path / "h1", scripted(fixtures, special)), clock, trial_clock,
                               compat, study)
    luna, sol, astra = (attempt_of(rows, model, "L1", "violation") for model in ("gpt-6-luna", "gpt-6-sol",
                                                                                 "gpt-6-astra"))
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    # The third pause in 60 minutes holds, whichever kinds the three are; the limit keeps its revision 3 name.
    assert status["holds"] == [f"provider_unavailable_limit:{astra}"]
    archived = payloads(root)
    assert [archived[attempt]["check"]["classification"] for attempt in (luna, sol, astra)] == [
        "provider_stalled", "provider_unavailable", "provider_stalled"]
    pauses = {attempt: archived[attempt]["orchestrator"]["provider_pause"] for attempt in (luna, sol, astra)}
    assert [pauses[attempt]["window_count"] for attempt in (luna, sol, astra)] == [1, 2, 3]
    assert [pauses[attempt]["holds_admission"] for attempt in (luna, sol, astra)] == [False, False, True]
    assert pauses[sol]["paused_at"] >= pauses[luna]["resume_at"]
    assert pauses[astra]["paused_at"] >= pauses[sol]["resume_at"] and pauses[astra]["paused_at"] - start < 3600
    # A resume inside the window holds from its start; once the window has passed, the run finishes.
    clock.now += 600
    held = await run_stalled(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), clock, trial_clock, compat,
                             study)
    assert held["status"] == "held" and held["holds"] == [f"retained_provider_unavailable_limit:{astra}"]
    assert held["realized_order"] == []
    clock.now = pauses[astra]["paused_at"] + 3600 + 1
    resumed = await run_stalled(root, plan, Harness(tmp_path / "h3", scripted(fixtures)), clock, trial_clock, compat,
                                study)
    assert resumed["status"] == "complete" and resumed["live_model_call_starts"] == 12
    assert {luna, sol, astra}.isdisjoint(resumed["realized_order"])
    starts = [record["data"]["attempt_id"] for record in records(root, "attempt_started")]
    assert len(starts) == len(set(starts)) == 12


@pytest.mark.parametrize("interruption", ["soft_stop", "crash_before_the_pause_is_journaled"])
async def test_a_restart_after_a_stall_respects_its_pause_and_never_reruns_it(compat, tmp_path, monkeypatch,
                                                                             interruption):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, trial_clock = PauseClock(), TrialClock()
    special = {LUNA_L1: lambda f: stall_steps(trial_clock, "gpt-6-luna", "xhigh")}
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    original = p1_live._Journal.append

    def crashing(self, kind, **data):
        if kind == "provider_pause_started":
            raise OSError("simulated crash between the archive and its pause record")
        return original(self, kind, **data)

    with monkeypatch.context() as patched:
        if interruption == "soft_stop":
            clock.on_wait = lambda clock: (root / v11_live.STOP_FILE).touch()
        else:
            patched.setattr(p1_live._Journal, "append", crashing)
        first = await run_stalled(root, plan, Harness(tmp_path / "h1", scripted(fixtures, special)), clock,
                                  trial_clock, compat, study)
    clock.on_wait = None
    pause = payloads(root)[attempt]["orchestrator"]["provider_pause"]
    assert first["status"] == "held" and first["live_model_call_starts"] == 1
    assert v11_live.study_provider_pauses(study)[attempt]["pause"] == pause
    (root / v11_live.STOP_FILE).unlink(missing_ok=True)
    if interruption != "soft_stop":
        # The interrupted index holds the next run; that run journals the pause sealed in the stalled attempt.
        assert records(root, "provider_pause_started") == []
        reconciling = await run_stalled(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), clock, trial_clock,
                                        compat, study)
        assert reconciling["status"] == "held" and f"retained_unreconciled_start:{attempt}" in reconciling["holds"]
        assert [record["data"] for record in records(root, "provider_pause_started")] == [{**pause, "recovered": True}]
    sessions = []
    resumed = await run_stalled(root, plan, Harness(tmp_path / "h3", scripted(fixtures),
                                                    observe=started_at(clock, sessions)),
                                clock, trial_clock, compat, study)
    assert resumed["status"] == "complete" and resumed["live_model_call_starts"] == 12
    assert attempt not in resumed["realized_order"] and min(time for _, _, time in sessions) >= pause["resume_at"]
    assert [record["data"]["attempt_id"] for record in records(root, "attempt_started")].count(attempt) == 1
    assert [record["data"]["attempt_id"] for record in records(root, "provider_pause_ended")] == [attempt]


async def test_a_stalled_compatibility_probe_is_unqualified_and_pauses_its_own_lanes(tmp_path):
    # One lane at a time: the stalled lane's jump past the trial wall must not reach another lane's attempt.
    root, plan = compat_root(tmp_path / "compat", caps=SERIAL)
    sample = compat_fixture(root, plan)
    clock, trial_clock = PauseClock(), TrialClock()

    def script_for(model, effort):
        return stall_steps(trial_clock, model, effort) if (model, effort) == ("gpt-6-sol", "low") \
            else qualifier_steps(sample)

    harness = trial_clock.harness = Harness(tmp_path / "homes", script_for)
    status = await run(root, plan, harness,
                       auth=authorization(plan, root=root, cutoff=10**6, deadline=2 * 10**6), wall_clock=clock,
                       pause_sleep=clock.sleep, clock=trial_clock, sleep=trial_clock.sleep)
    assert status["status"] == "complete" and status["holds"] == [] and status["live_model_call_starts"] == 6
    assert status["qualified_lanes"] == sorted(lane["lane_id"] for lane in plan["lanes"]
                                               if lane["lane_id"] != "gpt-6-sol-low")
    (payload,) = [payload for payload in payloads(root).values() if payload["lane_id"] == "gpt-6-sol-low"]
    assert payload["check"]["classification"] == "provider_stalled" and payload["check"]["passed"] is False
    assert payload["orchestrator"]["provider_pause"]["window_count"] == 1
    assert lane_records(root, "gpt-6-sol-low", "provider_pause_started") == [payload["orchestrator"]["provider_pause"]]


def test_a_stall_settled_at_its_reservation_holds_nothing():
    assert attempt_hold_kinds(False, ["valid_close"], "bounded_by_reservation", "provider_stalled") == []
    assert attempt_hold_kinds(False, ["valid_close"], "unresolved", "provider_stalled") == [
        "execution_check_failure", "unknown_final_usage"]
    row = {"status": "archived", "classification": "provider_stalled", "usage_settlement": "settled",
           "usage_total_tokens": 75000, "elapsed_seconds": 360.0}
    assert _ignore_reason(row) == "classification:provider_stalled"
    policy = v11_live.EXECUTION_POLICY["provider_stall_policy"]
    assert policy.startswith("delivery_confirmed_then_no_model_event_until_the_trial_wall")


# R1: a pause whose classification is durable but missing at study level binds every successor root


def crash_on_a_side_of_the_pause_record(patched, side):
    if side == "before":  # the study record write fails; the journaled settlement already names the refusal
        def refuse(*_args, **_kwargs):
            raise OSError("simulated storage failure before the study pause record")
        patched.setattr(v11_live, "record_study_pause", refuse)
    else:  # the study record is written; the archive record then fails
        summary = v11_phase.attempt_summary

        def crash(payload):
            if payload["orchestrator"]["provider_pause"] is not None:
                raise OSError("simulated crash between the study pause record and the archive record")
            return summary(payload)
        patched.setattr(v11_phase, "attempt_summary", crash)


@pytest.mark.parametrize("resume_first", [False, True], ids=["successor_first", "resume_first"])
@pytest.mark.parametrize("side", ["before", "after"])
async def test_a_failure_on_either_side_of_the_pause_record_still_pauses_a_root_of_another_arm(
        compat, tmp_path, monkeypatch, side, resume_first):
    study, rows, fixtures = calibration_study(tmp_path / "study")
    clock = PauseClock()
    a_root, a_plan = sealed_root(study, compat, phase="calibration", revision="calibration-a",
                                 arms=["calibration_extension_xhigh"])
    script_for, served = first_attempt_overloads(fixtures)
    with monkeypatch.context() as patched:
        crash_on_a_side_of_the_pause_record(patched, side)
        crashed = await run_paused(a_root, a_plan, Harness(tmp_path / "h-a", script_for), clock, compat, study)
    (attempt,) = [record["data"]["attempt_id"] for record in records(a_root, "attempt_started")]
    assert crashed["status"] == "held" and f"attempt_unarchived:{attempt}:OSError" in crashed["holds"]
    assert attempt not in [record["data"]["attempt_id"] for record in records(a_root, "attempt_archived")]
    (settled,) = [record["data"] for record in records(a_root, "usage_settled")]
    assert settled["settlement_reason"] == "provider_unavailable" and settled["actual_tokens"] == 75000
    assert (attempt in v11_live.study_provider_pauses(study)) is (side == "after")
    if resume_first:
        # A resumed A starts nothing and holds on its incomplete start, but first records the missing pause.
        resumed = await run_paused(a_root, a_plan, Harness(tmp_path / "h-a2", script_for), clock, compat, study)
        assert resumed["status"] == "held" and f"retained_unreconciled_start:{attempt}" in resumed["holds"]
        assert resumed["live_model_call_starts"] == 1
        assert len(resumed["recovered_study_pauses"]) == (side == "before")
    recorded = v11_live.study_provider_pauses(study)
    # B selects the other arm; A is its prior root. It waits out A's pause before its first session and counts it.
    b_root, b_plan = sealed_root(study, compat, phase="calibration", revision="calibration-b",
                                 arms=["calibration_extension_low"], prior_roots=[a_root])
    b_script, b_served = first_attempt_overloads(fixtures)
    clock.on_wait = lambda clock: (b_root / v11_live.STOP_FILE).touch() if b_served else None
    sessions = []
    b_status = await run_paused(b_root, b_plan, Harness(tmp_path / "h-b", b_script, observe=started_at(clock, sessions)),
                                clock, compat, study, prior_roots=[a_root])
    a_pause = v11_live.study_provider_pauses(study)[attempt]
    if side == "before":
        # The missing pause was recorded from A's journaled settlement, at reconciliation time, naming A.
        assert a_pause["plan_hash"] == a_plan["seal_hash"] and a_pause["phase"] == "calibration"
        assert a_pause["root_path"] == "roots/calibration-a" and a_pause["pause"]["lane_id"] == "gpt-6-luna-xhigh"
        recovery = a_pause["recovery"]
        assert recovery["settlement_reason"] == "provider_unavailable"
        assert recovery["recorded_by_plan_hash"] == (a_plan if resume_first else b_plan)["seal_hash"]
        assert b_status["recovered_study_pauses"] == ([] if resume_first else [a_pause["pause"]])
    else:
        assert "recovery" not in a_pause and recorded[attempt] == a_pause and b_status["recovered_study_pauses"] == []
    assert sessions and min(time for _, _, time in sessions) >= a_pause["pause"]["resume_at"]
    b_pause = sealed_pause(b_root)
    assert b_pause["window_count"] == 2 and b_status["holds"] == ["soft_stop"]
    assert b_status["live_model_call_starts"] == 1
    # Consumption is preserved: A's start stays consumed and incomplete, and both roots verify against the study.
    assert attempt in b_plan["consumed_attempts"]["consumed_attempt_ids"]
    a_report = v11_live.verify_live_root(a_root, bundle=fake_bundle(), study_directory=study)
    b_report = v11_live.verify_live_root(b_root, bundle=fake_bundle(), study_directory=study, prior_roots=[a_root])
    assert attempt in a_report["unreconciled_starts"] or a_report["status_counts"].get("incomplete_interrupted") == 1
    assert a_report["study_provider_pauses"] == b_report["study_provider_pauses"] == [a_pause["pause"], b_pause]
    # A later run records nothing again.
    assert v11_live.reconcile_study_pauses(study, b_plan, wall_clock=clock) == []


async def test_a_journaled_settlement_without_a_pause_classification_records_no_pause(compat, tmp_path, monkeypatch):
    study, rows, fixtures = calibration_study(tmp_path / "study")
    a_root, a_plan = sealed_root(study, compat, phase="calibration", revision="calibration-a",
                                 arms=["calibration_extension_xhigh"])
    original = p1_live._Journal.append

    def crashing(self, kind, **data):
        if kind == "attempt_archived":
            raise OSError("simulated crash before the archive record")
        return original(self, kind, **data)

    clock = PauseClock()
    with monkeypatch.context() as patched:
        patched.setattr(p1_live._Journal, "append", crashing)
        crashed = await run_paused(a_root, a_plan, Harness(tmp_path / "h-a", scripted(fixtures)), clock, compat, study)
    assert crashed["status"] == "held" and crashed["live_model_call_starts"] == 1
    (settled,) = [record["data"] for record in records(a_root, "usage_settled")]
    assert settled["usage_settlement"] == "settled" and "settlement_reason" not in settled
    assert v11_live.reconcile_study_pauses(study, a_plan, wall_clock=clock) == []
    assert v11_live.study_provider_pauses(study) == {}


# R2: a clean overload first reported at the last drain. Every runtime of a ``LateHarness`` gets the late
# notification, so the first attempt of the run is the one under test.


@pytest.mark.parametrize("turn_error", [True, False], ids=["turn_error", "null_turn_error"])
async def test_an_overload_first_reported_at_the_last_drain_is_still_provider_unavailable(compat, tmp_path,
                                                                                         turn_error):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None}
    steps = [("thread_status", {"type": "systemError"}), ("disconnect_on_close",),
             ("end", "failed", error if turn_error else None)]
    late = ("last_drain", {"error": error, "willRetry": False})
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, LateHarness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}),
                                                      late), clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    if not turn_error:
        # A3 condition 1 requires the full turn error to match the sole notification, including a non-null error.
        assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
        assert status["status"] == "held" and clock.waits == []
        assert payload["orchestrator"]["provider_pause"] is None
        return
    assert result["termination_kind"] == "provider_unavailable" and result["infrastructure_failures"] == []
    assert result["overload_stage"] == "before_tool"
    assert [notice["code"] for notice in result["provider_overload"]["error_notifications"]] == ["serverOverloaded"]
    assert [event for event in result["events"] if event["kind"] == "provider_overload_observed"]
    assert payload["check"]["classification"] == "provider_unavailable"
    assert payload["orchestrator"]["provider_pause"]["window_count"] == 1
    assert status["holds"] == ["soft_stop"]


@pytest.mark.parametrize("late", [None, "streamDisconnected"], ids=["no_notification", "other_code"])
async def test_a_failed_turn_without_a_qualifying_late_notification_still_fails(compat, tmp_path, late):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None}
    steps = [("disconnect_on_close",), ("end", "failed", error)]
    notice = None if late is None else ("last_drain", {"error": {"message": "lost", "codexErrorInfo": late},
                                                       "willRetry": False})
    clock = PauseClock()
    status = await run_paused(root, plan, LateHarness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}),
                                                      notice), clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    result = payloads(root)[attempt]["observer_result"]
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    assert result["infrastructure_failures"] == ([INVALID_TURN] if late is None else [INVALID_TURN, PROVIDER_ERROR])
    assert status["status"] == "held" and f"execution_check_failure:{attempt}" in status["holds"]
    assert v11_live.study_provider_pauses(study) == {} and clock.waits == []


# R3: the study pause verifier checks the recorded root path


@pytest.mark.parametrize("change", ["root_path", "claim_and_record"])
async def test_a_study_pause_naming_another_root_path_is_refused(compat, tmp_path, change):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    await run_paused(root, plan, Harness(tmp_path / "homes", scripted(fixtures, {SOL_L1: lambda f: overload_steps()})),
                     clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    assert v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)["study_provider_pauses"]
    paths = [Path(study) / v11_live.PAUSE_LEDGER / f"{attempt}.json"]
    if change == "claim_and_record":  # the claim agrees with the record, but the registry does not
        paths.append(Path(study) / v11_live.START_LEDGER / f"{attempt}.json")
    for path in paths:
        record = read_sealed(path)
        record.pop("seal_hash")
        atomic_json(path, seal({**record, "root_path": "roots/not-the-producing-root"}))
    with pytest.raises(v11_live.EvidenceError, match="another root path"):
        v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)


# Guest finding (revision 4 probe): a completed turn repeats its final agentMessage with itemsView "summary".


def test_only_an_exact_repeat_of_a_completed_item_is_reconciled_in_a_completed_turn():
    from swarm_auth_bench.peer_reporting_v11.live_runtime import _Controller

    def controller_with(events):
        controller = _Controller.__new__(_Controller)
        controller.raw_events, controller.failures, controller.session = events, [], None
        controller.queue_reconciled, controller.emitted = True, []
        controller.emit = lambda kind, **data: controller.emitted.append((kind, data))
        return controller

    message = {"delivery": None, "id": "agent-item-1", "memoryCitation": None, "phase": "final_answer",
               "questions": None, "text": "Done.", "type": "agentMessage"}
    def trace(embedded, standalone_method="item/completed"):
        return [{"kind": "codex_event", "agent_id": "observer", "raw": {"method": standalone_method,
                 "params": {"threadId": "t", "turnId": "u", "item": dict(message)}}},
                {"kind": "codex_event", "agent_id": "observer", "raw": {"method": "turn/started",
                 "params": {"threadId": "t", "turn": {"id": "u", "items": [], "itemsView": "notLoaded"}}}},
                {"kind": "codex_event", "agent_id": "observer", "raw": {"method": "turn/completed",
                 "params": {"threadId": "t", "turn": {"id": "u", "status": "completed", "itemsView": "summary",
                                                      "items": embedded}}}}]
    cases = (([dict(message)], "item/completed", False), ([], "item/completed", False),
             ([{**message, "text": "Other."}], "item/completed", True),
             ([{**message, "phase": "commentary"}], "item/completed", True),
             ([{"id": "never-sent", "type": "agentMessage", "text": "x"}], "item/completed", True),
             ([{**message, "type": "reasoning"}], "item/completed", True),
             ([dict(message)], "item/started", True))
    for embedded, standalone_method, expected_failure in cases:
        controller = controller_with(trace(embedded, standalone_method))
        controller.reconcile_turn_notifications(None)
        unresolved = "unreconciled items in turn lifecycle notification" in controller.failures
        assert unresolved is expected_failure, embedded
