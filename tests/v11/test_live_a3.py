"""Revision 5 amendment A3: post-tool overloads pause only after full reconciliation. No model calls."""

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11.lanes import PROVIDER_PAUSE_SECONDS

from .live_fakes import (
    OVERLOAD_MESSAGE,
    FakeTransport,
    Harness,
    PauseClock,
    fake_bundle,
    overload_steps,
    pilot_overload_steps,
    qualified_root,
)
from .test_live_astra_r3 import EXTRA_ERRORS, export_row
from .test_live_overload import (
    ASTRA_L1,
    INVALID_TURN,
    LUNA_L1,
    SOL_L1,
    lane_records,
    run_paused,
    sealed_root,
    started_at,
)
from .test_live_r2 import READ, attempt_of, payloads, records, scripted, smoke_study


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("a3-compat"))[0]


async def assert_execution_failure_and_export(root, plan, status, clock, compat, study, fixtures, tmp_path,
                                            attempt):
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    assert "overload_stage" not in result and result["runtime_closed"]
    assert payload["check"]["classification"] is None and payload["check"]["passed"] is False
    assert "no_infrastructure_failure" in payload["check"]["failure_reasons"]
    assert payload["orchestrator"]["provider_pause"] is None
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    assert status["status"] == "held" and status["live_model_call_starts"] == 1 and clock.waits == []
    assert f"execution_check_failure:{attempt}" in status["holds"]
    assert records(root, "provider_pause_started") == [] and v11_live.study_provider_pauses(study) == {}
    assert len(result["tool_requests"]) == len(result["tool_receipts"]) == 1
    assert result["tool_requests"][0]["response_sent"] is True
    assert result["raw_events"] == [event["event"] for event in result["events"] if event["kind"] == "runtime_event"]
    resumed = Harness(tmp_path / "resume-homes", scripted(fixtures))
    later = await run_paused(root, plan, resumed, clock, compat, study)
    assert later["status"] == "held" and f"retained_failed_or_unknown_attempt:{attempt}" in later["holds"]
    assert resumed.created == [] and later["live_model_call_starts"] == 1
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    verified = next(row for row in report["lanes"]["gpt-6-luna-xhigh"]["entries"] if row["attempt_id"] == attempt)
    assert verified["classification"] is None
    index, row = export_row(root, study, tmp_path / "export", attempt)
    normalized = read_sealed(tmp_path / "export" / row["attempt_path"])
    assert row["status"] == "archived" and row["evidence_error"] is None and row["score_error"] is None
    assert row["excluded_from_analysis"] is False and index["analysis_exclusions"] == []
    assert normalized["termination_kind"] == "infrastructure_incomplete"
    assert row["score"]["eligibility"]["eligible"] is False
    assert row["score"]["eligibility"]["resolves_assignment"] is False
    assert all(endpoint["value"] is None for endpoint in row["score"]["endpoints"].values())
    assert [output["text"] for output in normalized["observer_outputs"]] == [
        output["text"] for output in result["observer_outputs"]]
    return result, normalized


@pytest.mark.parametrize("boundary", ["in_turn", "last_drain"])
@pytest.mark.parametrize("scope", ["missing_turn", "wrong_turn", "wrong_thread"])
@pytest.mark.parametrize("code", ["streamDisconnected", "serverOverloaded"])
async def test_unattributable_retained_errors_prevent_a3(compat, tmp_path, boundary, scope, code):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = {"message": "broken stream" if code == "streamDisconnected" else OVERLOAD_MESSAGE,
             "codexErrorInfo": code, "additionalDetails": None}
    params = {"error": error, "willRetry": False}
    if scope == "wrong_turn":
        params["turnId"] = "wrong-turn"
    elif scope == "wrong_thread":
        params["threadId"] = "wrong-thread"
    method = "raw_thread" if scope == "missing_turn" else "raw"
    if boundary == "last_drain":
        method += "_on_close"
    steps = [READ, (method, "error", params), *overload_steps()]
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}))
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    result, _ = await assert_execution_failure_and_export(root, plan, status, clock, compat, study, fixtures,
                                                        tmp_path, attempt)
    notices = [event["raw"]["params"] for event in result["raw_events"] if event.get("method") == "error"]
    assert len(notices) == 2
    extra = notices[0 if boundary == "in_turn" else 1]
    assert extra["error"] == error and extra["willRetry"] is False
    if scope == "missing_turn":
        assert "turnId" not in extra
    else:
        key = "turnId" if scope == "wrong_turn" else "threadId"
        assert extra[key] == params[key]
    failures = [event for event in result["events"] if event["kind"] == "infrastructure_failed"
                and event.get("reason") == "non-retryable provider error notification"]
    assert len(failures) == 1 and len(failures[0]["error_notifications"]) == 2
    assert sum(notice["attributed"] for notice in failures[0]["error_notifications"]) == 1


@pytest.mark.parametrize("scope", ["wrong_turn", "wrong_thread", "matching_scope"])
async def test_unconsumed_standalone_items_cannot_reconcile_summary_repeats(compat, tmp_path, scope):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None}
    item = {"id": "unconsumed", "type": "agentMessage", "text": "Evidence never consumed"}
    params = {"item": item}
    if scope == "wrong_turn":
        params["turnId"] = "wrong-turn"
    elif scope == "wrong_thread":
        params["threadId"] = "wrong-thread"
    summary = {"id": "turn-gpt-6-luna-xhigh", "status": "failed", "error": error, "itemsView": "summary",
               "items": [item]}
    steps = [READ, ("raw_on_close", "item/completed", params),
             ("raw_on_close", "turn/completed", {"turn": summary}), *overload_steps()]
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}))
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    result, normalized = await assert_execution_failure_and_export(root, plan, status, clock, compat, study,
                                                                 fixtures, tmp_path, attempt)
    assert result["queue_reconciled"] is False
    assert "unreconciled items in turn lifecycle notification" in result["infrastructure_failures"]
    standalone = [event["raw"]["params"] for event in result["raw_events"]
                  if event.get("method") == "item/completed" and event["raw"]["params"].get("item") == item]
    embedded = [event["raw"]["params"]["turn"] for event in result["raw_events"]
                if event.get("method") == "turn/completed" and event["raw"]["params"].get("turn") == summary]
    assert len(standalone) == len(embedded) == 1
    reconciliation = next(event for event in result["events"] if event["kind"] == "turn_notifications_reconciled")
    assert reconciliation["unresolved"][0]["params"]["turn"]["items"] == [item]
    expected = [item["text"]] if scope == "matching_scope" else []
    assert [output["text"] for output in result["observer_outputs"]] == expected
    assert [output["text"] for output in normalized["observer_outputs"]] == expected


def test_newly_sealed_plan_describes_a3_predicates(compat, tmp_path):
    study, _, _ = smoke_study(tmp_path / "study")
    _, plan = sealed_root(study, compat)
    policy = plan["execution_policy"]["provider_overload_policy"]
    for predicate in (
        "A3_exactly_one_retained_error_notification_attributed_to_observer_thread_and_turn",
        "serverOverloaded_willRetry_false_matching_failed_turn_error",
        "no_other_error_code_or_announced_retry",
        "every_admitted_request_has_one_sent_response_unique_call_ids_declared_attributable_tools",
        "receipts_and_turn_notifications_reconciled_cleanup_confirmed",
        "decided_after_shutdown_and_the_last_drain",
        "prior_tools_and_output_allowed_overload_stage_before_tool_or_after_tool",
    ):
        assert predicate in policy
    assert "before_any_tool_request_and_output" not in policy


async def test_exact_pilot_replay_pauses_resumes_and_exports_an_ineligible_unscored_attempt(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock, sessions = PauseClock(), []
    harness = Harness(tmp_path / "homes", scripted(fixtures, {SOL_L1: lambda f: pilot_overload_steps()}),
                      observe=started_at(clock, sessions))
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert status["status"] == "complete" and status["holds"] == []
    assert status["live_model_call_starts"] == 12
    assert result["termination_kind"] == payload["check"]["classification"] == "provider_unavailable"
    assert result["overload_stage"] == result["provider_overload"]["overload_stage"] == "after_tool"
    assert result["infrastructure_failures"] == [] and result["runtime_closed"] and result["queue_reconciled"]
    assert result["observer_outputs"] == [] and len(result["tool_requests"]) == len(result["tool_receipts"]) == 1
    assert result["tool_requests"][0]["response_sent"] is True
    assert result["usage"]["total_tokens"] is None and result["usage"]["observed_total_tokens"] == 15838
    methods = [event["raw"]["method"] for event in result["raw_events"] if event["kind"] == "codex_event"]
    assert methods == ["turn/started", "item/started", "item/completed", "item/started", "item/completed",
                       "item/started", "item/completed", "thread/tokenUsage/updated", "thread/tokenUsage/updated",
                       "thread/status/changed", "error", "turn/completed", "runtime/disconnected"]
    kinds = [event["kind"] for event in result["events"]]
    assert kinds.index("tool_requested") < kinds.index("tool_dispatch_recorded") < kinds.index("tool_response_sent")
    assert kinds.index("tool_response_sent") < kinds.index("tool_result_delivery_confirmed")
    assert kinds.index("provider_overload_observed") > kinds.index("turn_notifications_reconciled")
    assert kinds.index("provider_overload_observed") > kinds.index("tool_receipts_reconciled")
    assert payload["check"]["passed"] is False
    assert payload["orchestrator"]["usage_settlement"] == {
        "status": "bounded_by_reservation", "actual_tokens": 75000, "observed_tokens": 15838,
        "reservation_tokens": 75000, "settlement_reason": "provider_unavailable"}
    pause = payload["orchestrator"]["provider_pause"]
    assert pause["resume_at"] - pause["paused_at"] == PROVIDER_PAUSE_SECONDS
    assert pause["window_count"] == 1 and pause["holds_admission"] is False
    assert lane_records(root, "gpt-6-sol-xhigh", "provider_pause_started") == [pause]
    (ended,) = lane_records(root, "gpt-6-sol-xhigh", "provider_pause_ended")
    assert ended["ended_at"] >= pause["resume_at"]
    sol_sessions = [when for model, effort, when in sessions if model == "gpt-6-sol" and effort == "xhigh"]
    assert len(sol_sessions) == 3 and all(when >= pause["resume_at"] for when in sol_sessions[1:])
    assert not any(pause["paused_at"] < when < pause["resume_at"] for _, _, when in sessions)
    assert status["realized_order"].count(attempt) == 1
    assert v11_live.study_provider_pauses(study)[attempt]["pause"] == pause
    (archive,) = lane_records(root, "gpt-6-sol-xhigh", "attempt_archived")[:1]
    assert archive["summary"]["overload_stage"] == "after_tool"
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    row = next(row for row in report["lanes"]["gpt-6-sol-xhigh"]["entries"] if row["attempt_id"] == attempt)
    assert row["overload_stage"] == "after_tool" and row["settlement_reason"] == "provider_unavailable"
    index, exported = export_row(root, study, tmp_path / "export", attempt)
    normalized = read_sealed(tmp_path / "export" / exported["attempt_path"])
    assert exported["excluded_from_analysis"] and exported["exclusion_reason"] == "provider_unavailable"
    assert normalized["eligible"] is False and normalized["excluded_from_analysis"] is True
    assert normalized["overload_stage"] == exported["overload_stage"] == "after_tool"
    assert exported["score"]["eligibility"]["eligible"] is False
    assert exported["score"]["eligibility"]["resolves_assignment"] is False
    assert all(endpoint["value"] is None for endpoint in exported["score"]["endpoints"].values())
    assert index["summary"]["trial_count"] == 11 and index["analysis_exclusion_count"] == 1


@pytest.mark.parametrize("prefix,stage", [
    ([], "before_tool"),
    ([("message", "Checking the records.")], "after_tool"),
    ([("raw", "item/agentMessage/delta", {"itemId": "partial-1", "delta": "Checking"})], "after_tool"),
    ([READ], "after_tool"),
    ([(*READ, {"receipt": False})], "after_tool"),
    ([("raw", "thread/tokenUsage/updated", {"tokenUsage": {}}), READ], "after_tool"),
    ([("tool", "agent_finish", {"reason": "completed", "summary": "Done."})], "after_tool"),
])
async def test_prior_output_and_missing_usage_or_receipts_do_not_disqualify_overloads(compat, tmp_path, prefix,
                                                                                     stage):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    harness = Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: [*prefix, *overload_steps()]}))
    status = await run_paused(root, plan, harness, clock, compat, study)
    payload = payloads(root)[attempt_of(rows, "gpt-6-luna", "L1", "violation")]
    assert status["holds"] == ["soft_stop"]
    assert payload["observer_result"]["overload_stage"] == stage
    assert payload["check"]["classification"] == "provider_unavailable"
    assert payload["orchestrator"]["usage_settlement"]["status"] == "bounded_by_reservation"
    assert payload["orchestrator"]["usage_settlement"]["actual_tokens"] == 75000


class MissingResponseTransport(FakeTransport):
    async def _peer_dynamic_tool(self, session, request_id, params):
        # Admit and dispatch the real controller request, but never prepare or send a response.
        await self._peer_controllers[session.thread_id].request(request_id, params)


class CleanupFailureTransport(FakeTransport):
    async def close(self):
        await super().close()
        raise RuntimeError("fake cleanup unconfirmed")


class ChangedTransportHarness(Harness):
    def __init__(self, root, script_for, transport):
        super().__init__(root, script_for)
        self.transport = transport

    def factory(self, model, effort):
        home = self.root / f"home-{len(self.created)}"
        home.mkdir(parents=True)
        runtime = self.transport(model, effort, self.script_for(model, effort), home)
        self.created.append(runtime)
        return runtime


@pytest.mark.parametrize("case", [
    "second_code", "other_code_in_started", "retry_before_error", "retry_at_last_drain", "retry_in_lifecycle",
    "duplicate_error", "missing_retry_flag",
    "unanswered_tool", "contradicted_receipt", "unreconciled_notifications", "cleanup_unconfirmed",
    "mismatched_turn_error", "mismatched_late_completion", "reused_call_id", "undeclared_tool", "unattributable_tool",
])
async def test_any_failed_a3_condition_keeps_the_after_tool_overload_an_execution_failure(compat, tmp_path, case):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    steps, transport = [READ, *overload_steps()], FakeTransport
    error = {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None}
    if case == "second_code":
        steps.insert(1, ("raw_on_close", "error", EXTRA_ERRORS["stream_disconnected"]))
    elif case == "other_code_in_started":
        steps.insert(1, ("raw_on_close", "turn/started", {"turn": {
            "id": "turn-gpt-6-luna-xhigh", "status": "inProgress", "items": [],
            "error": EXTRA_ERRORS["stream_disconnected"]["error"]}}))
    elif case == "retry_in_lifecycle":
        steps.insert(1, ("raw_on_close", "thread/status/changed", {
            "status": {"type": "systemError"}, "willRetry": True}))
    elif case.startswith("retry_"):
        method = "raw" if case == "retry_before_error" else "raw_on_close"
        steps.insert(1, (method, "error", EXTRA_ERRORS["retry_announced"]))
    elif case == "duplicate_error":
        steps.insert(1, ("raw_on_close", "error", {"error": error, "willRetry": False}))
    elif case == "missing_retry_flag":
        steps[2] = ("raw", "error", {"error": error})
    elif case == "unanswered_tool":
        steps[0] = (*READ, {"await_response": False})
        transport = MissingResponseTransport
    elif case == "contradicted_receipt":
        steps[0] = (*READ, {"receipt": {"success": False}})
    elif case == "unreconciled_notifications":
        steps.insert(1, ("raw_on_close", "turn/completed", {"turn": {
            "id": "turn-gpt-6-luna-xhigh", "status": "failed", "error": error,
            "items": [{"id": "unconsumed", "type": "agentMessage", "text": "Unconsumed output"}]}}))
    elif case == "cleanup_unconfirmed":
        transport = CleanupFailureTransport
    elif case == "mismatched_turn_error":
        steps[-1] = ("end", "failed", {**error, "message": "different overload error"})
    elif case == "mismatched_late_completion":
        steps.insert(1, ("raw_on_close", "turn/completed", {"turn": {
            "id": "turn-gpt-6-luna-xhigh", "status": "failed", "error": {**error, "additionalDetails": "different"}}}))
    elif case == "reused_call_id":
        steps.insert(1, (*READ, {"call_id": "call-1"}))
    elif case == "undeclared_tool":
        steps[0] = ("tool", "undeclared_tool", {})
    elif case == "unattributable_tool":
        steps[0] = (*READ, {"params": {"turnId": "wrong-turn"}})
    harness = ChangedTransportHarness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps}), transport)
    clock = PauseClock()
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    assert "overload_stage" not in result
    assert payload["orchestrator"]["provider_pause"] is None
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    assert status["status"] == "held" and status["live_model_call_starts"] == 1 and clock.waits == []
    assert f"execution_check_failure:{attempt}" in status["holds"]
    assert records(root, "provider_pause_started") == [] and v11_live.study_provider_pauses(study) == {}
    if case == "unanswered_tool":
        assert len(result["tool_requests"]) == 1 and result["tool_requests"][0]["response_sent"] is False
    if case not in {"undeclared_tool", "unattributable_tool"}:
        assert INVALID_TURN in result["infrastructure_failures"]


async def test_three_mixed_before_and_after_tool_overloads_still_hold_at_study_level(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    special = {LUNA_L1: lambda f: overload_steps(), SOL_L1: lambda f: pilot_overload_steps(),
               ASTRA_L1: lambda f: [READ, *overload_steps()]}
    clock = PauseClock()
    status = await run_paused(root, plan, Harness(tmp_path / "homes", scripted(fixtures, special)), clock, compat, study)
    attempts = [attempt_of(rows, model, "L1", "violation") for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")]
    archived = payloads(root)
    pauses = [archived[attempt]["orchestrator"]["provider_pause"] for attempt in attempts]
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    assert status["holds"] == [f"provider_unavailable_limit:{attempts[-1]}"]
    assert [archived[attempt]["observer_result"]["overload_stage"] for attempt in attempts] == [
        "before_tool", "after_tool", "after_tool"]
    assert [pause["window_count"] for pause in pauses] == [1, 2, 3]
    assert pauses[-1]["paused_at"] - pauses[0]["paused_at"] < 3600
    assert all(v11_live.study_provider_pauses(study)[attempt]["pause"] == pause
               for attempt, pause in zip(attempts, pauses))


@pytest.mark.parametrize("changed", [False, True], ids=["exact_summary_repeat", "changed_summary_item"])
async def test_after_tool_overload_keeps_the_existing_summary_repeat_reconciliation(compat, tmp_path, changed):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None}
    message = {"id": "agent-item-1", "type": "agentMessage", "text": "Changed" if changed else "Checking"}
    steps = [READ, ("message", "Checking"), ("raw_on_close", "turn/completed", {"turn": {
        "id": "turn-gpt-6-luna-xhigh", "status": "failed", "error": error, "itemsView": "summary",
        "items": [message]}}), *overload_steps()]
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, Harness(tmp_path / "homes", scripted(fixtures, {LUNA_L1: lambda f: steps})),
                              clock, compat, study)
    result = payloads(root)[attempt_of(rows, "gpt-6-luna", "L1", "violation")]["observer_result"]
    assert result["termination_kind"] == ("infrastructure_incomplete" if changed else "provider_unavailable")
    assert status["status"] == "held"
    if changed:
        assert "unreconciled items in turn lifecycle notification" in result["infrastructure_failures"]
        assert clock.waits == []
    else:
        assert status["holds"] == ["soft_stop"] and result["overload_stage"] == "after_tool"
