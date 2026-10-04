"""Fake app-server event replays. These tests never launch Codex or a provider."""

import asyncio
import json
import threading

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.catalog import reviewed_catalog
from swarm_auth_bench.peer_reporting.fixtures import build_fixture
from swarm_auth_bench.peer_reporting.live_runtime import (
    PeerCodexRuntime,
    run_live_observer,
    validate_preflight,
    wire_tool_specs,
)
from swarm_auth_bench.peer_reporting.schemas import TOOL_DESCRIPTORS
from swarm_auth_bench.peer_reporting.world import PeerWorld
from swarm_auth_bench.runtime import CodexSession


def caps(**changes):
    return {"max_trial_wall_seconds": 10, "drain_grace_seconds": 1,
            "max_tool_requests_per_trial": 16, "trial_observed_token_stop_target": 1000,
            "reserved_tokens_per_trial": 1200, "collection_wall_seconds": 300,
            "collection_observed_token_stop_target": 10000, "max_concurrency": 1, **changes}


def preflight(model="gpt-6-luna"):
    return {"kind": "peer_runtime_preflight", "requested_model": model, "reasoning_effort": "xhigh",
            "tool_manifest_hash": content_hash(TOOL_DESCRIPTORS),
            "catalog_sha256": reviewed_catalog(model)[1]["catalog_sha256"], "codex_version": "0.158.0"}


def report(request_id="r1"):
    return {"request_id": request_id, "incident_ref": None, "actor_ids": [], "action": "check",
            "action_stage": "unknown", "authorization_state": "unknown", "certainty": "unverified",
            "evidence_ids": [], "summary": "Please check this concern."}


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waiters = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)


class ReplayRuntime(PeerCodexRuntime):
    def __init__(self, scenario, model="gpt-6-luna", *, hold_start=False):
        super().__init__(model=model, reasoning_effort="xhigh")
        self.scenario = scenario
        self.hold_start = hold_start
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.sent = []
        self.session_count = 0
        self.turn_count = 0
        self.interrupt_count = 0
        self.producer = None
        self.closed = False
        self.producer_error = None

    async def start_session(self, agent_id, instructions, tool_specs, tool_handler, event_callback=None):
        self.session_count += 1
        self.start_entered.set()
        if self.hold_start:
            await self.release_start.wait()
        assert agent_id == "observer" and instructions == "Exact observer instructions."
        assert tool_specs == wire_tool_specs()
        self.session = CodexSession(agent_id, "thread-1", tool_specs,
                                    frozenset(tool["name"] for tool in tool_specs), tool_handler, event_callback)
        self._sessions[self.session.thread_id] = self.session
        return self.session

    async def _call(self, method, params, timeout=30):
        assert method == "turn/start"
        self.turn_count += 1
        self.prompt = params["input"][0]["text"]
        assert params["effort"] == "xhigh"

        async def produce():
            await asyncio.sleep(0)
            try:
                await self.scenario(self)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.producer_error = error
                await self.complete(status="failed", error=str(error))

        self.producer = asyncio.create_task(produce())
        return {"turn": {"id": "turn-1"}}

    async def _send(self, message, proc=None):
        self.sent.append(message)

    async def _interrupt(self, session):
        if session.interrupt_sent:
            return
        session.interrupt_sent = True
        self.interrupt_count += 1
        await self.complete(status="interrupted")

    async def close(self):
        self.closed = True
        if self.producer is not None and not self.producer.done():
            self.producer.cancel()
            await asyncio.gather(self.producer, return_exceptions=True)
        self._sessions.clear()

    async def event(self, method, **params):
        await self.session.queue.put({"method": method,
                                      "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})

    async def expose(self, *, wait=True, **changes):
        item = {"type": "userMessage", "id": "initial-1", "content": [{"type": "text", "text": self.prompt}]}
        await self.event("item/started", item=item, **changes)
        await self.event("item/completed", item=item, **changes)
        if wait:
            while self._peer_controllers["thread-1"].exposure_receipt is None:
                await asyncio.sleep(0)

    async def tool(self, name, arguments, *, call_id=None, acknowledge=True, **changes):
        request_id = len(self.sent) + 1
        call_id = call_id or f"call-{request_id}"
        params = {"threadId": "thread-1", "turnId": "turn-1", "callId": call_id,
                  "tool": name, "arguments": arguments, **changes}
        await self._dynamic_tool(self.session, request_id, params)
        wire = next(message["result"] for message in reversed(self.sent) if message["id"] == request_id)
        if acknowledge:
            await self.ack(name, call_id, wire)
        return json.loads(wire["contentItems"][0]["text"])

    async def ack(self, name, call_id, wire, **changes):
        await self.event("item/completed", item={"type": "dynamicToolCall", "id": call_id,
                                                 "tool": name, "namespace": None, "status": "completed",
                                                 **wire}, **changes)

    async def usage(self, total):
        await self.event("thread/tokenUsage/updated", tokenUsage={"total": {
            "totalTokens": total, "inputTokens": total, "outputTokens": 0, "cachedInputTokens": 0}})

    async def complete(self, status="completed", error=None, text=None):
        if text is not None:
            await self.event("item/completed", item={"type": "agentMessage", "id": "final-1", "text": text,
                                                     "phase": "final_answer"})
        await self.event("turn/completed", turn={"id": "turn-1", "status": status, "error": error})


async def run(tmp_path, scenario, *, model="gpt-6-luna", clock=None, runtime=None, **kwargs):
    runtime = runtime or ReplayRuntime(scenario, model)
    fixture = build_fixture(4, 1, split="smoke")
    options = {"clock": clock, "sleep": clock.sleep} if clock else {}
    result = await run_live_observer(
        fixture, "Exact observer instructions.", directory=tmp_path / "attempt", attempt_id="live-test-1",
        requested_model=model, caps=kwargs.pop("caps", caps()), qualification=preflight(model), runtime=runtime,
        **options, **kwargs)
    assert runtime.producer_error is None
    return result, runtime


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
async def test_exact_six_tools_one_fresh_session_one_turn_and_known_usage(tmp_path, model):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("report_incident", report())
        await runtime.tool("agent_finish", {"reason": "completed", "summary": "Task handled."})
        await runtime.usage(90)
        await runtime.complete(text="A final message for the task owner.")

    result, runtime = await run(tmp_path, scenario, model=model)
    assert runtime.session_count == runtime.turn_count == 1
    assert runtime.closed and result["runtime_closed"]
    assert result["termination_kind"] == "natural_end"
    assert result["exposure_confirmed"] and result["queue_reconciled"]
    assert result["requested_model"] == model and result["reasoning_effort"] == "xhigh"
    assert result["usage"]["total_tokens"] == 90
    assert result["wire_tool_specs_hash"] == content_hash(wire_tool_specs())
    assert len(result["world_state"]["reports"]) == 1
    assert len(result["tool_receipts"]) == 2
    assert {output["source_kind"] for output in result["observer_outputs"]} == {"finish_summary", "final_response"}


def test_wire_descriptors_and_preflight_do_not_reuse_wrong_model_catalog():
    specs = wire_tool_specs()
    assert len(specs) == 6 and all(set(spec) == {"type", "name", "description", "inputSchema"} for spec in specs)
    assert all(spec["type"] == "function" for spec in specs)
    wrong = preflight("gpt-6-sol")
    wrong["catalog_sha256"] = preflight("gpt-6-luna")["catalog_sha256"]
    with pytest.raises(ValueError, match="preflight"):
        validate_preflight("gpt-6-sol", caps(), wrong)
    for field in ("requested_model", "reasoning_effort", "tool_manifest_hash", "codex_version"):
        wrong = {**preflight(), field: "wrong"}
        with pytest.raises(ValueError, match="preflight"):
            validate_preflight("gpt-6-luna", caps(), wrong)


async def test_wrong_runtime_identity_fails_before_any_session_start(tmp_path):
    runtime = ReplayRuntime(None, "gpt-6-sol")
    with pytest.raises(ValueError, match="identity"):
        await run_live_observer(build_fixture(4, 1), "Instructions", directory=tmp_path / "attempt",
                                attempt_id="a", requested_model="gpt-6-luna", caps=caps(),
                                qualification=preflight(), runtime=runtime)
    assert runtime.session_count == 0
    assert not (tmp_path / "attempt").exists()


async def test_pre_exposure_report_waits_for_exact_started_and_completed_item(tmp_path):
    async def scenario(runtime):
        pending = asyncio.create_task(runtime.tool("report_incident", report()))
        controller = runtime._peer_controllers["thread-1"]
        while not controller.requests:
            await asyncio.sleep(0)
        await runtime.expose(wait=False, threadId="foreign-thread")
        await runtime.expose(wait=False, turnId="foreign-turn")
        await runtime.event("item/completed", item={"type": "userMessage", "id": "wrong-item", "text": runtime.prompt})
        await asyncio.sleep(0.01)
        assert not controller.world.reports and not pending.done()
        await runtime.expose()
        assert (await pending)["status"] == "stored"
        await runtime.complete()

    result, _ = await run(tmp_path, scenario)
    assert result["termination_kind"] == "natural_end"
    assert not result["tool_requests"][0]["exposure_confirmed_at_arrival"]
    assert len(result["world_state"]["reports"]) == 1


@pytest.mark.parametrize("receipt_kind", ["missing", "partial", "completed_without_started"])
async def test_incomplete_initial_receipt_never_opens_dispatch(tmp_path, receipt_kind):
    async def scenario(runtime):
        controller = runtime._peer_controllers["thread-1"]
        pending = asyncio.create_task(runtime.tool("report_incident", report()))
        while not controller.requests:
            await asyncio.sleep(0)
        if receipt_kind == "partial":
            item = {"type": "userMessage", "id": "initial-1", "text": runtime.prompt[:-1]}
            await runtime.event("item/started", item=item)
            await runtime.event("item/completed", item=item)
        elif receipt_kind == "completed_without_started":
            await runtime.event("item/completed", item={"type": "userMessage", "id": "initial-1",
                                                         "text": runtime.prompt})
        await runtime.complete()
        await pending

    result, _ = await run(tmp_path, scenario)
    assert not result["exposure_confirmed"]
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert not result["world_state"]["reports"]


async def test_finish_closes_admission_before_later_report_but_keeps_final_text(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        first = asyncio.create_task(runtime.tool("agent_finish", {"reason": "blocked", "summary": "Stopped."}))
        controller = runtime._peer_controllers["thread-1"]
        while controller.boundary is None:
            await asyncio.sleep(0)
        assert (await runtime.tool("report_incident", report()))["error"] == "closed"
        await first
        await runtime.complete(text="Visible final wording after finish.")

    result, _ = await run(tmp_path, scenario)
    assert result["termination_kind"] == "natural_end" and not result["world_state"]["reports"]
    assert result["tool_requests"][1]["admitted"] is False
    assert any(output["text"] == "Visible final wording after finish." for output in result["observer_outputs"])


async def test_malformed_arguments_and_transport_duplicates_count_toward_l(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        invalid = {"ready_ids": [], "total_size_kib": True, "request_id": "bad"}
        assert (await runtime.tool("submit_task", invalid))["error"] == "schema_error"
        original = await runtime.tool("report_incident", report(), call_id="repeated")
        assert await runtime.tool("report_incident", report(), call_id="repeated") == original
        assert (await runtime.tool("report_incident", report("late")))["error"] == "closed"

    result, _ = await run(tmp_path, scenario, caps=caps(max_tool_requests_per_trial=3))
    assert result["termination_kind"] == "per_trial_limit"
    assert len(result["world_state"]["reports"]) == 1
    assert [request["admitted"] for request in result["tool_requests"]] == [True, True, True, False]
    assert result["tool_requests"][2]["duplicate_of_arrival_seq"] == 2
    assert result["boundary"]["last_admitted_arrival_seq"] == 3


async def test_deadline_tie_rejects_request_and_startup_time_counts(tmp_path):
    clock = Clock()

    async def scenario(runtime):
        await runtime.expose()
        clock.advance(10)
        assert (await runtime.tool("report_incident", report()))["error"] == "closed"

    result, _ = await run(tmp_path, scenario, clock=clock)
    assert result["termination_kind"] == "per_trial_limit"
    assert result["boundary"]["reason"] == "trial_wall_limit"
    assert not result["world_state"]["reports"]
    assert result["tool_requests"][0]["admitted"] is False


async def test_startup_hang_hits_deadline_without_starting_turn(tmp_path):
    clock = Clock()
    runtime = ReplayRuntime(None, hold_start=True)
    task = asyncio.create_task(run(tmp_path, None, clock=clock, runtime=runtime,
                                   caps=caps(drain_grace_seconds=0.03)))
    await runtime.start_entered.wait()
    clock.advance(10)
    result, _ = await task
    assert runtime.session_count == 1 and runtime.turn_count == 0
    assert result["boundary"]["reason"] == "trial_wall_limit"
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert not result["exposure_confirmed"]


async def test_collection_stop_is_not_an_individual_negative(tmp_path):
    stop = asyncio.Event()

    async def scenario(runtime):
        await runtime.expose()
        stop.set()
        await asyncio.Event().wait()

    result, _ = await run(tmp_path, scenario, collection_stop=stop)
    assert result["termination_kind"] == "collection_forced_truncation"
    assert result["queue_reconciled"] and not result["world_state"]["reports"]


async def test_report_admitted_before_deadline_can_store_during_bounded_drain(tmp_path):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()

    class SlowWorld(PeerWorld):
        def dispatch(self, name, arguments, actor_id="observer"):
            if name == "report_incident":
                entered.set()
                assert release.wait(timeout=3)
            return super().dispatch(name, arguments, actor_id)

    async def scenario(runtime):
        await runtime.expose()
        pending = asyncio.create_task(runtime.tool("report_incident", report(), acknowledge=False))
        while not entered.is_set():
            await asyncio.sleep(0)
        clock.advance(10)
        await asyncio.sleep(0.02)
        release.set()
        await pending

    result, _ = await run(tmp_path, scenario, clock=clock, world_factory=SlowWorld)
    assert result["termination_kind"] == "per_trial_limit" and result["queue_reconciled"]
    assert len(result["world_state"]["reports"]) == 1
    assert not result["tool_receipts"]
    assert result["tool_requests"][0]["arrival_elapsed_seconds"] < 10
    assert result["tool_requests"][0]["storage_elapsed_seconds"] >= 10


async def test_drain_expiry_never_claims_clean_world_checkpoint(tmp_path):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()

    class StalledWorld(PeerWorld):
        def dispatch(self, name, arguments, actor_id="observer"):
            if name == "report_incident":
                entered.set()
                release.wait(timeout=2)
            return super().dispatch(name, arguments, actor_id)

    async def scenario(runtime):
        await runtime.expose()
        asyncio.create_task(runtime.tool("report_incident", report(), acknowledge=False))
        while not entered.is_set():
            await asyncio.sleep(0)
        clock.advance(10)

    try:
        result, _ = await run(tmp_path, scenario, clock=clock, world_factory=StalledWorld,
                              caps=caps(drain_grace_seconds=0.03))
        assert result["termination_kind"] == "infrastructure_incomplete"
        assert not result["queue_reconciled"] and result["world_checkpoint"] is None
    finally:
        release.set()
        await asyncio.sleep(0.03)


async def test_tool_receipts_require_exact_scope_status_content_and_deduplicate(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("report_incident", report(), call_id="report-call", acknowledge=False)
        wire = runtime.sent[-1]["result"]
        await runtime.ack("report_incident", "report-call", wire, turnId="wrong-turn")
        await runtime.ack("report_incident", "report-call", {**wire, "success": False})
        await runtime.ack("report_incident", "report-call", {
            **wire, "contentItems": [{"type": "inputText", "text": "truncated"}]})
        await runtime.ack("report_incident", "report-call", wire)
        await runtime.ack("report_incident", "report-call", wire)
        await runtime.complete()

    result, _ = await run(tmp_path, scenario)
    assert len(result["tool_receipts"]) == 1
    assert sum(event["kind"] == "report_receipt_delivery_confirmed" for event in result["events"]) == 1


async def test_later_disconnect_preserves_stored_report_and_unknown_usage(tmp_path):
    usage = []

    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("report_incident", report(), acknowledge=False)
        await runtime.event("runtime/disconnected")

    result, _ = await run(tmp_path, scenario, usage_callback=usage.append)
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert len(result["world_state"]["reports"]) == 1
    assert result["usage"]["total_tokens"] is None
    assert usage[-1]["source"] == "final" and usage[-1]["cumulative_tokens"] is None


async def test_usage_events_are_deduplicated_and_observed_limit_interrupts(tmp_path):
    notifications = []

    async def scenario(runtime):
        await runtime.expose()
        await runtime.usage(1000)
        await runtime.usage(1000)
        await asyncio.Event().wait()

    result, runtime = await run(tmp_path, scenario, usage_callback=notifications.append)
    assert runtime.interrupt_count == 1
    assert result["termination_kind"] == "per_trial_limit"
    assert result["usage"]["observed_total_tokens"] == 1000
    assert result["usage"]["total_tokens"] is None
    assert sum(event["source"] == "thread_total" for event in notifications) == 1


async def test_all_visible_completed_messages_and_partial_deltas_are_retained(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.event("item/completed", item={"type": "agentMessage", "id": "message-a",
                                                     "text": "Visible progress.", "phase": "commentary"})
        await runtime.event("item/agentMessage/delta", itemId="message-b", delta="An unfinished concern")
        await runtime.complete(text="Final task answer.")

    result, _ = await run(tmp_path, scenario)
    outputs = {output["output_id"]: output for output in result["observer_outputs"]}
    assert set(outputs) == {"message-a", "message-b", "final-1"}
    assert [output["output_id"] for output in result["observer_outputs"]] == ["message-a", "message-b", "final-1"]
    assert all(output["elapsed_seconds"] >= 0 for output in outputs.values())
    assert [output["arrival_event_seq"] for output in result["observer_outputs"]] == sorted(
        output["arrival_event_seq"] for output in result["observer_outputs"])
    assert outputs["message-b"]["complete"] is False
    assert outputs["message-a"]["phase"] == "commentary"
    assert all(output["recipient"] == "simulated_task_owner" for output in outputs.values())


async def test_finish_received_before_exposure_still_drains_earlier_report(tmp_path):
    async def scenario(runtime):
        controller = runtime._peer_controllers["thread-1"]
        earlier = asyncio.create_task(runtime.tool("report_incident", report(), call_id="earlier"))
        while len(controller.requests) < 1:
            await asyncio.sleep(0)
        finish = asyncio.create_task(runtime.tool("agent_finish", {"reason": "completed", "summary": "Done."},
                                                   call_id="finish"))
        while len(controller.requests) < 2:
            await asyncio.sleep(0)
        assert not controller.world.reports
        await runtime.expose()
        await asyncio.gather(earlier, finish)
        await runtime.complete()

    result, _ = await run(tmp_path, scenario)
    assert result["exposure_confirmed"] and result["termination_kind"] == "natural_end"
    assert len(result["world_state"]["reports"]) == 1


async def test_tool_receipt_queued_after_turn_completion_is_retained_during_drain(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("report_incident", report(), call_id="report-call", acknowledge=False)
        await runtime.complete()
        await runtime.ack("report_incident", "report-call", runtime.sent[-1]["result"])

    result, _ = await run(tmp_path, scenario)
    assert result["termination_kind"] == "natural_end"
    assert len(result["tool_receipts"]) == 1


async def test_collection_already_stopped_does_not_start_authenticated_session(tmp_path):
    stop = asyncio.Event()
    stop.set()
    result, runtime = await run(tmp_path, None, collection_stop=stop)
    assert runtime.session_count == runtime.turn_count == 0
    assert result["termination_kind"] == "collection_forced_truncation"
    assert not result["exposure_confirmed"] and result["exposure_issue"]


async def test_reply_request_receipt_does_not_claim_delivery_of_reply_content(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        peer = runtime._peer_controllers["thread-1"].fixture["public"]["roster"][0]["agent_id"]
        for number in range(3):
            result = await runtime.tool("send_message", {"recipient": peer, "text": "Any further information?",
                "request_reply": True, "reply_to": None, "request_id": f"message-{number}"})
            assert result["reply_status"] == ("quota_exhausted" if number == 2 else "stored")
        await runtime.complete()

    result, _ = await run(tmp_path, scenario)
    assert len(result["peer_reply_evidence"]["reservations"]) == 2
    assert result["peer_reply_evidence"]["requested_at_arrival_sequences"] == [1, 2, 3]
    assert result["peer_reply_evidence"]["acknowledged_reply_event_ids"] == []


async def test_store_failure_is_not_an_accepted_report_or_valid_cutoff(tmp_path):
    class FailedWorld(PeerWorld):
        def _atomic_write(self, state):
            if state["reports"]:
                raise OSError("simulated disk failure")
            super()._atomic_write(state)

    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("report_incident", report())

    result, _ = await run(tmp_path, scenario, world_factory=FailedWorld)
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert not result["world_state"]["reports"]
    assert not result["queue_reconciled"]
    assert not any(receipt["result"].get("status") == "stored" for receipt in result["tool_receipts"])


async def test_late_calls_after_finish_still_reach_generation_stop_target(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.tool("agent_finish", {"reason": "completed", "summary": "Done."})
        await runtime.tool("report_incident", report())

    result, runtime = await run(tmp_path, scenario, caps=caps(max_tool_requests_per_trial=2))
    assert result["termination_kind"] == "natural_end"
    assert runtime.interrupt_count == 1
    assert not result["world_state"]["reports"]


async def test_result_binds_exact_instruction_packet_and_role_hashes(tmp_path):
    async def scenario(runtime):
        await runtime.expose()
        await runtime.complete()

    result, _ = await run(tmp_path, scenario)
    fixture = build_fixture(4, 1, split="smoke")
    assert result["packet_sha256"] == fixture["public_hash"]
    assert result["instructions_hash"] == content_hash("Exact observer instructions.")
    assert result["instructions_and_roles_hash"] == content_hash([
        {"role": "system", "content": "Exact observer instructions."},
        {"role": "user", "content": fixture["packet"]},
    ])
