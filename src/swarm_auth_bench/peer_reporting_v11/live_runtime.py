"""One-turn v1.1 observer adapter: P1 receipt checks with effort, world mode, and v1.1 tools.

This is a port of ``peer_reporting.live_runtime``. P1 pins ``xhigh``, the P1 tool
descriptors, and ``PeerWorld``, and its package files are hashed into sealed P1
phase plans, so the P1 module is not edited. The differences are:

- the reasoning effort is per attempt (``xhigh`` or ``low``) and is checked at
  preflight, on the turn-start request, and in the result;
- the tools, input validation, world class, and world audit come from a
  ``ProtocolBundle`` (the v1.1 modules, or test fakes);
- the world is created with the attempt's ``world_mode``, and the result records it;
- a request that cannot be attributed to the turn, names an undeclared tool, or
  reuses a known call ID with other content is an execution failure even after
  admission closed; valid late work still receives ``closed`` (spec 10);
- at close, every pending tool receipt is reconciled with the response prepared
  for its call; a contradiction is an execution failure, and a missing receipt
  is recorded in ``tool_receipt_reconciliation`` (spec 10);
- a turn that ends with an explicit provider capacity error (``serverOverloaded``)
  after packet delivery, before any tool request and with no observer output, and
  that otherwise closes cleanly, terminates as ``provider_unavailable`` instead of
  an invalid turn result (spec 10, revision 3). Eligibility is decided after
  shutdown and the last event drain, from every reconciled event: an error with
  another code, or an announced retry, anywhere in the turn removes it. Any other
  error, or an overload after a tool request or output, is an execution failure;
- outside that exception, a non-retryable ``error`` notification is an execution
  failure whatever the final turn status, including an interrupted turn after a
  stop and a completed one (spec 10, revision 3).

The exact receipt matching, queue, drain, and usage handling are otherwise
unchanged. No function in this module runs inference on import or during offline
preflight.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from ..events import content_hash
from ..long_events import StreamingEventLog
from ..peer_reporting.catalog import ReviewedPeerRuntime, reviewed_catalog
from ..peer_reporting.config import MODELS, validate_caps
from ..peer_reporting.live_runtime import PeerCodexRuntime, _text_item
from ..runtime import (
    NATIVE_ITEM_TYPES,
    SUPPORTED_CODEX_VERSION,
    CodexSession,
    RuntimeProtocolError,
    TurnResult,
)
from .bundle import ProtocolBundle
from .lanes import PROVIDER_UNAVAILABLE, validate_effort, validate_world_mode

ADAPTER_VERSION = "peer-reporting-v11-live-runtime-v1"
PREFLIGHT_KIND = "peer_v11_runtime_preflight"
# Spec 10 (revision 3): the provider's explicit capacity error, as the app-server's ``error`` notification names it.
PROVIDER_OVERLOAD_CODE = "serverOverloaded"
INVALID_TURN_RESULT = "turn result identity, status, or error invalid"
PROVIDER_ERROR_FAILURE = "non-retryable provider error notification"


def provider_error_code(error: Any) -> str | None:
    """The ``codexErrorInfo`` code of an app-server turn error, when it is a plain string code."""
    info = error.get("codexErrorInfo") if type(error) is dict else None
    return info if type(info) is str else None


def validate_preflight(requested_model: str, caps: dict, qualification: dict, runtime: Any | None = None, *,
                       reasoning_effort: str, bundle: ProtocolBundle) -> dict[str, Any]:
    """Verify offline identity bindings; phase approval belongs to orchestration."""
    if requested_model not in MODELS:
        raise ValueError("peer observer requires an exact approved model ID")
    validate_effort(reasoning_effort)
    validate_caps(caps)
    _, catalog = reviewed_catalog(requested_model)
    expected = {"kind": PREFLIGHT_KIND, "requested_model": requested_model, "reasoning_effort": reasoning_effort,
                "tool_manifest_hash": bundle.tool_manifest_hash, "tool_descriptors_hash": bundle.tool_descriptors_hash,
                "wire_tool_specs_hash": bundle.wire_tool_specs_hash, "tool_schema_version": bundle.schema_version,
                "catalog_sha256": catalog["catalog_sha256"], "codex_version": SUPPORTED_CODEX_VERSION}
    if type(qualification) is not dict or any(qualification.get(key) != value for key, value in expected.items()):
        raise ValueError("peer runtime preflight does not match model, effort, tools, catalog, or client")
    if runtime is not None:
        metadata = runtime.metadata
        supplied_catalog = metadata.get("peer_catalog_override")
        if (runtime.model != requested_model or runtime.reasoning_effort != reasoning_effort
                or metadata.get("requested_model") != requested_model
                or metadata.get("reasoning_effort") != reasoning_effort
                or type(supplied_catalog) is not dict
                or supplied_catalog.get("requested_model") != requested_model
                or supplied_catalog.get("catalog_sha256") != catalog["catalog_sha256"]):
            raise ValueError("runtime identity or reviewed peer catalog differs from preflight")
        if not callable(getattr(runtime, "bind_peer_controller", None)):
            raise ValueError("runtime lacks the peer admission adapter")
        if getattr(runtime, "_sessions", {}):
            raise ValueError("peer attempt requires a runtime without existing sessions")
        attestations = metadata.get("manifest_attestations", [])
        if any(proof.get("model") != requested_model or proof.get("verified") is not True
               or proof.get("tools") != bundle.attested_tools for proof in attestations):
            raise ValueError("cached manifest attestation differs from the exact peer tools and model")
    return expected


class V11PeerRuntime(PeerCodexRuntime):
    """The reviewed P1 peer transport with a declared ``xhigh`` or ``low`` effort.

    P1's constructor and turn-start check pin ``xhigh``. This subclass replaces
    only those two members. The reviewed catalog bytes, process launch, and
    dynamic-tool delegation to the bound controller are inherited unchanged.
    """

    def __init__(self, *, model: str, reasoning_effort: str, **kwargs: Any) -> None:
        reviewed_catalog(model)
        validate_effort(reasoning_effort)
        # Skip ReviewedPeerRuntime.__init__, whose only extra step rejects every effort except xhigh.
        super(ReviewedPeerRuntime, self).__init__(model=model, reasoning_effort=reasoning_effort, **kwargs)
        self._peer_controllers: dict[str, _Controller] = {}
        self._peer_dispatches: set[asyncio.Task] = set()

    async def _call(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        controller = self._peer_controllers.get(params.get("threadId")) if method == "turn/start" else None
        if controller is not None and (params.get("effort") != self.reasoning_effort
                                       or params.get("effort") != controller.reasoning_effort
                                       or params.get("input") != [{"type": "text", "text": controller.fixture["packet"]}]):
            raise RuntimeProtocolError("peer turn input or reasoning effort differs from its binding")
        # Skip PeerCodexRuntime._call, whose turn-start check pins xhigh; the check above replaces it.
        result = await super(PeerCodexRuntime, self)._call(method, params, timeout)
        if controller is not None:
            controller.emit("initial_packet_sent", thread_id=params["threadId"],
                            turn_id=(result.get("turn") or {}).get("id"),
                            transport_evidence="successful_turn_start_response")
        return result


class _Controller:
    def __init__(self, fixture: dict, directory: Path, attempt_id: str, caps: dict,
                 runtime: Any, clock: Callable[[], float], started: float,
                 event_sink: Callable[[dict], Any] | None, usage_callback: Callable[[dict], Any] | None,
                 *, reasoning_effort: str, world_mode: str, bundle: ProtocolBundle) -> None:
        self.fixture = deepcopy(fixture)
        self.attempt_id, self.caps, self.runtime = attempt_id, deepcopy(caps), runtime
        self.reasoning_effort, self.world_mode, self.bundle = reasoning_effort, world_mode, bundle
        self.tool_names = frozenset(bundle.tool_names)
        self.clock, self.started = clock, started
        self.deadline = started + caps["max_trial_wall_seconds"]
        self.event_sink, self.usage_callback = event_sink, usage_callback
        self.log = StreamingEventLog(directory / "runtime-log", attempt_id, fsync_every=1)
        self.world = bundle.world_factory(fixture, directory / "world", trial_id=attempt_id,
                                          max_tool_requests=caps["max_tool_requests_per_trial"],
                                          world_mode=world_mode)
        self.gate = asyncio.Lock()
        self.operations: asyncio.Queue = asyncio.Queue()
        self.hard_stop = asyncio.Event()
        self.session: CodexSession | None = None
        self.boundary: dict | None = None
        self.requests: list[dict] = []
        self.calls: dict[str, dict] = {}
        self.futures: dict[int, asyncio.Future] = {}
        self.raw_events: list[dict] = []
        self.events: list[dict] = []
        self.receipts: list[dict] = []
        self.pending_receipts: list[dict] = []
        self.receipt_ids: set[str] = set()
        self.user_item_id: str | None = None
        self.exposure_receipt: dict | None = None
        self.assistant_items: dict[str, dict] = {}
        self.assistant_deltas: dict[str, dict] = {}
        self.usage_seen: set[str] = set()
        self.response_usage: dict[str, int | None] = {}
        self.observed_tokens: int | None = None
        self.usage_missing = False
        self.provider_errors: list[dict] = []
        self.failures: list[str] = []
        self.interrupts: set[asyncio.Task] = set()
        self.worker: asyncio.Task | None = None
        self.closing = False
        self.queue_reconciled = False
        self.transport_closing = False
        self.emit("observer_admitted", requested_model=runtime.model, reasoning_effort=reasoning_effort,
                  world_mode=world_mode, deadline_elapsed_seconds=caps["max_trial_wall_seconds"])

    def elapsed(self) -> float:
        return self.clock() - self.started

    def emit(self, kind: str, **data: Any) -> None:
        event = {"kind": kind, "attempt_id": self.attempt_id, "event_seq": len(self.events),
                 "elapsed_seconds": self.elapsed(), **deepcopy(data)}
        self.log.emit(kind, "observer", adapter_event=event, content_hash=content_hash(event))
        self.events.append(event)
        if self.event_sink is not None:
            returned = self.event_sink(deepcopy(event))
            if inspect.isawaitable(returned):
                raise TypeError("event_sink must be synchronous; it may perform a durable controller write")

    def _interrupt(self) -> None:
        if self.session is None or not self.session.turn_id:
            return
        task = asyncio.create_task(self.runtime._interrupt(self.session))
        self.interrupts.add(task)
        task.add_done_callback(self.interrupts.discard)

    def _boundary(self, kind: str, reason: str, *, stop_generation: bool) -> None:
        if self.boundary is None:
            self.boundary = {"termination_kind": kind, "reason": reason,
                             "last_admitted_arrival_seq": len(self.requests), "elapsed_seconds": self.elapsed()}
            self.emit("admission_closed", **self.boundary)
        if stop_generation:
            self.hard_stop.set()
            self._interrupt()

    def _deadline(self) -> bool:
        now = self.clock()
        if now < self.started:
            self.failures.append("monotonic clock regressed")
            self._boundary("infrastructure_incomplete", "clock_regression", stop_generation=True)
            return True
        if now >= self.deadline:
            self._boundary("per_trial_limit", "trial_wall_limit", stop_generation=True)
            return True
        return False

    async def stop(self, kind: str, reason: str) -> None:
        async with self.gate:
            if not self._deadline():
                self._boundary(kind, reason, stop_generation=True)

    async def fail(self, reason: str) -> None:
        async with self.gate:
            self.failures.append(reason)
            self.emit("infrastructure_failed", reason=reason)
            self._boundary("infrastructure_incomplete", reason, stop_generation=True)

    def _resolve_held(self) -> None:
        for request in self.requests:
            future = self.futures.get(request["arrival_seq"])
            world_sequence = request.get("world_arrival_seq")
            if future is not None and not future.done() and world_sequence is not None:
                result = self.world.result_for(world_sequence)
                if result is not None:
                    request["result"] = deepcopy(result)
                    request["storage_elapsed_seconds"] = self.elapsed()
                    future.set_result((result, True))

    async def work(self) -> None:
        while True:
            operation, payload, completion = await self.operations.get()
            try:
                if operation == "dispatch":
                    request = payload
                    result = await asyncio.to_thread(self.world.dispatch, request["tool"], request["arguments"])
                    request["world_arrival_seq"] = len(self.world.requests)
                    if result["status"] != "deferred":
                        request["result"] = deepcopy(result)
                        request["storage_elapsed_seconds"] = self.elapsed()
                        if not completion.done():
                            completion.set_result((result, True))
                    self.emit("tool_dispatch_recorded", arrival_seq=request["arrival_seq"],
                              world_arrival_seq=request["world_arrival_seq"], result=result)
                elif operation == "exposure":
                    verified = await asyncio.to_thread(self.world.confirm_exposure, self.fixture["packet"])
                    if not verified:
                        raise RuntimeProtocolError("world refused the verified initial packet receipt")
                    self._resolve_held()
                elif operation == "close":
                    await asyncio.to_thread(self.world.close, payload)
                    self._resolve_held()
                    self.queue_reconciled = True
                    if not completion.done():
                        completion.set_result(True)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.fail(f"controller storage failed: {type(error).__name__}: {error}")
                if completion is not None and not completion.done():
                    completion.set_result(({"status": "error", "error": "closed"}, False))
                for future in self.futures.values():
                    if not future.done():
                        future.set_result(({"status": "error", "error": "closed"}, False))
                return
            finally:
                self.operations.task_done()

    async def request(self, request_id: Any, raw: dict) -> tuple[dict, dict, bool]:
        async with self.gate:
            reached_deadline = self._deadline()
            sequence = len(self.requests) + 1
            name = f"{raw.get('namespace')}.{raw.get('tool')}" if raw.get("namespace") else raw.get("tool")
            record = {"arrival_seq": sequence, "request_id": request_id, "call_id": raw.get("callId"),
                      "thread_id": raw.get("threadId"), "turn_id": raw.get("turnId"), "tool": name,
                      "arguments": deepcopy(raw.get("arguments")), "raw": deepcopy(raw),
                      "arrival_elapsed_seconds": self.elapsed(), "world_arrival_seq": None,
                      "arrival_event_seq": len(self.events),
                      "admitted": self.boundary is None and not reached_deadline,
                      "exposure_confirmed_at_arrival": self.exposure_receipt is not None,
                      "result": None, "response_sent": False}
            self.requests.append(record)
            self.emit("tool_requested", request=record)
            # Spec 10: validity is checked before permission to dispatch, so contradictory protocol evidence is
            # an execution failure even after admission closed; valid late work still receives ``closed``. A
            # late request needs no active turn, because the turn may already have completed.
            call_id = record["call_id"]
            session = self.session
            attributable = (session is not None and session.turn_id is not None
                            and raw.get("threadId") == session.thread_id and raw.get("turnId") == session.turn_id
                            and type(call_id) is str and bool(call_id) and type(name) is str
                            and name in self.tool_names)
            if not attributable or (record["admitted"] and not session.active):
                record["protocol_rejected"] = True
                result = {"status": "error", "error": "schema_error"}
                record["result"] = result
                self.failures.append("unattributable or undeclared dynamic tool request")
                self._boundary("infrastructure_incomplete", "dynamic_request_attribution", stop_generation=True)
                return record, result, False
            previous = self.calls.get(call_id)
            if previous is not None:
                record["duplicate_of_arrival_seq"] = previous["arrival_seq"]
                if (previous["tool"] != name or previous["arguments"] != record["arguments"]):
                    self.failures.append("conflicting duplicate transport call ID")
                    self._boundary("infrastructure_incomplete", "conflicting_call_id", stop_generation=True)
                    result = {"status": "error", "error": "idempotency_conflict"}
                    record["result"] = result
                    return record, result, False
            if not record["admitted"]:
                if previous is None:
                    self.calls[call_id] = record  # a later reuse of this call ID is checked against it
                result = {"status": "error", "error": "closed"}
                record["result"] = result
                if sequence >= self.caps["max_tool_requests_per_trial"]:
                    self._boundary("per_trial_limit", "tool_request_limit", stop_generation=True)
                return record, result, True
            if previous is not None:
                future = self.futures[previous["arrival_seq"]]
            else:
                self.calls[call_id] = record
                future = asyncio.get_running_loop().create_future()
                self.futures[sequence] = future
                self.operations.put_nowait(("dispatch", record, future))
                if name == "agent_finish":
                    try:
                        self.bundle.validate_input(name, record["arguments"])
                    except self.bundle.schema_error:
                        pass
                    else:
                        self._boundary("natural_end", "agent_finish", stop_generation=False)
            if sequence >= self.caps["max_tool_requests_per_trial"]:
                self._boundary("per_trial_limit", "tool_request_limit", stop_generation=True)
        result, success = await asyncio.shield(future)
        record["result"] = deepcopy(result)
        return record, result, success

    def response_prepared(self, record: dict, result: dict, wire: str, success: bool) -> None:
        record.update(wire_text=wire, response_success=success)
        self.emit("tool_response_prepared", arrival_seq=record["arrival_seq"], call_id=record["call_id"],
                  result=result, success=success)

    def response_sent(self, record: dict) -> None:
        record["response_sent"] = True
        self.emit("tool_response_sent", arrival_seq=record["arrival_seq"], call_id=record["call_id"])
        for raw in list(self.pending_receipts):
            if self._tool_receipt(raw):
                self.pending_receipts.remove(raw)

    def _scope(self, params: dict, *, turn_object: bool = False) -> bool:
        session = self.session
        turn = (params.get("turn") or {}).get("id") if turn_object else params.get("turnId")
        return bool(session and session.turn_id and params.get("threadId") == session.thread_id
                    and turn == session.turn_id)

    @staticmethod
    def _receipt_matches(item: dict, request: dict) -> bool:
        """The completed item repeats exactly the response prepared for its call: tool, status, success, content."""
        return ("wire_text" in request and item.get("namespace") is None and item.get("tool") == request["tool"]
                and item.get("status") == "completed" and item.get("success") is request["response_success"]
                and item.get("contentItems") == [{"type": "inputText", "text": request["wire_text"]}])

    def _tool_receipt(self, raw: dict) -> bool:
        params = raw.get("params") or {}
        item = params.get("item") or {}
        if not self._scope(params) or item.get("type") != "dynamicToolCall":
            return False
        call_id = item.get("callId") or item.get("id")
        request = self.calls.get(call_id) if type(call_id) is str else None
        if request is None or not request["response_sent"] or not self._receipt_matches(item, request):
            return False
        identity = content_hash([self.attempt_id, params["threadId"], params["turnId"], call_id])
        if identity in self.receipt_ids:
            return True
        self.receipt_ids.add(identity)
        receipt = {"receipt_id": identity, "call_id": call_id, "tool": request["tool"],
                   "arrival_seq": request["arrival_seq"], "elapsed_seconds": self.elapsed(),
                   "result": deepcopy(request["result"]), "success": request["response_success"],
                   "raw": deepcopy(raw)}
        self.receipts.append(receipt)
        self.emit("tool_result_delivery_confirmed", receipt=receipt)
        if request["tool"] == "report_incident" and request["result"].get("status") == "stored":
            self.emit("report_receipt_delivery_confirmed", report_id=request["result"]["report_id"],
                      call_id=call_id, arrival_seq=request["arrival_seq"])
        return True

    def reconcile_receipts(self) -> dict:
        """Spec 10: at close, reconcile every pending tool receipt with the response sent for its call.

        A receipt may arrive before its response is marked sent; it is matched
        then. A receipt still pending at close is an execution failure when it
        contradicts the response prepared for its call (content, success flag,
        tool, or status), claims a completed result for a call that never
        received a response, or names no known call. A sent response without
        any receipt is recorded as missing; that alone is not a failure.
        """
        contradicted, unresolved, received = [], [], set()
        for raw in list(self.pending_receipts):
            if self._tool_receipt(raw):
                self.pending_receipts.remove(raw)
                continue
            item = (raw.get("params") or {}).get("item") or {}
            call_id = item.get("callId") or item.get("id")
            request = self.calls.get(call_id) if type(call_id) is str else None
            entry = {"call_id": call_id, "tool": item.get("tool"), "status": item.get("status"),
                     "success": item.get("success"),
                     "arrival_seq": request["arrival_seq"] if request is not None else None}
            received.add(call_id)
            if request is None:
                contradicted.append({**entry, "reason": "receipt_for_unknown_call"})
            elif "wire_text" in request:
                if self._receipt_matches(item, request):  # its response send never completed
                    unresolved.append({**entry, "reason": "receipt_matches_a_response_whose_send_did_not_complete"})
                else:
                    contradicted.append({**entry, "reason": "receipt_contradicts_the_response"})
            elif item.get("status") == "completed" or item.get("contentItems") is not None:
                contradicted.append({**entry, "reason": "completed_receipt_without_a_response"})
            else:
                unresolved.append({**entry, "reason": "incomplete_receipt_without_a_response"})
        received |= {receipt["call_id"] for receipt in self.receipts}
        missing: dict[str, dict] = {}
        for request in self.requests:
            if request.get("response_sent") and request["call_id"] not in received:
                missing.setdefault(request["call_id"], {"call_id": request["call_id"], "tool": request["tool"],
                                                        "arrival_seq": request["arrival_seq"]})
        if contradicted:
            reason = "contradictory tool delivery receipt"
            self.failures.append(reason)
            self.emit("infrastructure_failed", reason=reason, receipts=contradicted)
        summary = {"confirmed": len(self.receipts), "missing": list(missing.values()), "contradicted": contradicted,
                   "unresolved": unresolved}
        self.emit("tool_receipts_reconciled", **summary)
        return summary

    async def _usage(self, raw: dict) -> None:
        params, method = raw["params"], raw["method"]
        identity = content_hash([self.attempt_id, method, params])
        if identity in self.usage_seen:
            return
        self.usage_seen.add(identity)
        if method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total")
            value = total.get("totalTokens") if type(total) is dict else None
            value = value if type(value) is int and value >= 0 else None
            source = "thread_total"
        else:
            response_id = params.get("responseId")
            total = params.get("usage")
            value = total.get("totalTokens") if type(total) is dict else None
            value = value if type(value) is int and value >= 0 else None
            source = "raw_response"
            if type(response_id) is not str or not response_id:
                value = None
            elif response_id in self.response_usage:
                if self.response_usage[response_id] != value:
                    await self.fail("conflicting response usage notification")
                return
            else:
                self.response_usage[response_id] = value
                value = (sum(self.response_usage.values()) if all(type(v) is int for v in self.response_usage.values())
                         else None)
        if value is None:
            self.usage_missing = True
        else:
            self.observed_tokens = max(self.observed_tokens or 0, value)
        observation = {"notification_id": identity, "cumulative_tokens": self.observed_tokens if value is not None
                       else None, "source": source, "attempt_id": self.attempt_id}
        self.emit("usage_observed" if value is not None else "usage_unavailable", **observation)
        if self.usage_callback is not None:
            result = self.usage_callback(deepcopy(observation))
            if inspect.isawaitable(result):
                await result
        if self.observed_tokens is not None and self.observed_tokens >= self.caps["trial_observed_token_stop_target"]:
            await self.stop("per_trial_limit", "trial_observed_token_limit")

    async def event(self, event: dict) -> None:
        self.raw_events.append(deepcopy(event))
        self.emit("runtime_event", event=event)
        if event.get("kind") in {"tool_handler_error", "native_approval_declined", "native_prompt_declined",
                                  "unknown_server_request_declined"}:
            await self.fail(f"runtime rejected unsafe operation: {event.get('kind')}")
            return
        if event.get("kind") != "codex_event":
            return
        raw = event.get("raw") or {}
        params, method = raw.get("params") or {}, raw.get("method")
        if method == "runtime/disconnected":
            if not self.transport_closing:
                await self.fail("runtime disconnected")
            return
        if event.get("agent_id") != "observer":
            return
        if method == "turn/completed" and self._scope(params, turn_object=True):
            async with self.gate:
                if not self._deadline():
                    self._boundary("natural_end", "turn_completed", stop_generation=False)
            return
        if not self._scope(params):
            return
        if method in {"thread/tokenUsage/updated", "rawResponse/completed"}:
            await self._usage(raw)
            return
        if method == "error":
            self.provider_errors.append({"error": deepcopy(params.get("error")), "will_retry": params.get("willRetry"),
                                         "code": provider_error_code(params.get("error")),
                                         "elapsed_seconds": self.elapsed(), "arrival_event_seq": len(self.events) - 1})
            return
        item = params.get("item") or {}
        if method in {"item/started", "item/completed"} and item.get("type") in NATIVE_ITEM_TYPES:
            await self.fail("native tool item appeared")
            return
        if method in {"item/started", "item/completed"} and item.get("type") == "userMessage":
            async with self.gate:
                # Retain a real delivery failure even if a stop has already closed admission.
                if _text_item(item) != self.fixture["packet"]:
                    reason = "initial packet delivery mismatch"
                    self.failures.append(reason)
                    self.emit("infrastructure_failed", reason=reason)
                    self._boundary("infrastructure_incomplete", reason, stop_generation=True)
                    return
                if self._deadline() or self.closing or (self.boundary is not None
                                                       and self.boundary["reason"] != "agent_finish"):
                    return
                item_id = item.get("id")
                if type(item_id) is not str or not item_id:
                    return
                if method == "item/started":
                    if self.user_item_id not in (None, item_id):
                        self.failures.append("ambiguous initial user-message item")
                        self._boundary("infrastructure_incomplete", "ambiguous_initial_item", stop_generation=True)
                    else:
                        self.user_item_id = item_id
                elif (self.user_item_id == item_id and self.exposure_receipt is None
                      and item.get("status", "completed") == "completed"):
                    self.exposure_receipt = {"thread_id": params["threadId"], "turn_id": params["turnId"],
                                             "item_id": item_id, "elapsed_seconds": self.elapsed(),
                                             "packet_hash": content_hash(self.fixture["packet"]), "raw": deepcopy(raw)}
                    self.emit("initial_packet_delivery_confirmed", receipt=self.exposure_receipt)
                    self.operations.put_nowait(("exposure", None, None))
            return
        if method == "item/completed" and item.get("type") == "dynamicToolCall":
            if not self._tool_receipt(raw):
                self.pending_receipts.append(deepcopy(raw))
                self.emit("tool_delivery_unattributed", raw=raw)
        elif method == "item/completed" and item.get("type") == "agentMessage":
            item_id, text = item.get("id"), _text_item(item)
            if type(item_id) is str and text is not None:
                existing = self.assistant_items.get(item_id)
                first = self.assistant_deltas.get(item_id, {})
                output = {"output_id": item_id, "source_kind": "final_response", "text": text,
                          "phase": item.get("phase"), "complete": True,
                          "elapsed_seconds": first.get("elapsed_seconds", self.elapsed()),
                          "arrival_event_seq": first.get("arrival_event_seq", len(self.events) - 1)}
                if existing is not None and existing["text"] != text:
                    await self.fail("conflicting completed assistant message")
                elif existing is None:
                    self.assistant_items[item_id] = output
        elif method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
            item_id = params.get("itemId")
            if isinstance(item_id, str):
                output = self.assistant_deltas.setdefault(item_id, {
                    "text": "", "elapsed_seconds": self.elapsed(), "arrival_event_seq": len(self.events) - 1,
                })
                output["text"] += params["delta"]

    def begin_close(self) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        if not self.closing:
            self.closing = True
            reason = self.boundary["reason"] if self.boundary else "infrastructure_incomplete"
            self.operations.put_nowait(("close", reason, future))
        return future

    async def drain_runtime_events(self) -> None:
        """The legacy turn loop stops at completion; retain its remaining queue."""
        if self.session is None:
            return
        while not self.session.queue.empty():
            raw = self.session.queue.get_nowait()
            await self.event({"kind": "codex_event", "agent_id": "observer",
                              "method": raw.get("method"), "raw": raw})

    def provider_overload(self, result: TurnResult | None, requested_model: str) -> dict | None:
        """Spec 10 (revision 3): evidence that the provider refused the turn for capacity, or None.

        The attributed turn ended by itself (``failed``) after the exact packet
        was delivered, every scoped ``error`` notification carries the
        ``serverOverloaded`` code and none announces a retry, a turn error, if
        any, carries the same code, no tool request arrived, no assistant output
        appeared, no usage notification lacked a total, and no failure was
        recorded. Anything else is an invalid turn result. The adapter decides
        with this check again after shutdown and the last drain, so the evidence
        it records lists every reconciled error notification.
        """
        session, errors = self.session, self.provider_errors
        if (result is None or session is None or result.model != requested_model or result.turn_id is None
                or result.turn_id != session.turn_id or result.status != "failed"
                or result.termination_reason is not None):
            return None
        if (self.failures or self.requests or self.receipts or self.pending_receipts or self.assistant_items
                or self.assistant_deltas or result.text or self.exposure_receipt is None or self.usage_missing
                or self.hard_stop.is_set() or (self.boundary or {}).get("reason") != "turn_completed"):
            return None
        if (not errors or any(error["code"] != PROVIDER_OVERLOAD_CODE for error in errors)
                or any(error["will_retry"] is True for error in errors)
                or (result.error is not None and provider_error_code(result.error) != PROVIDER_OVERLOAD_CODE)):
            return None
        return {"code": PROVIDER_OVERLOAD_CODE, "turn_status": result.status, "turn_error": deepcopy(result.error),
                "error_notifications": deepcopy(errors), "tool_requests": 0, "observer_outputs": 0}


def _audited_world(controller: _Controller) -> tuple[dict, dict | None]:
    """Read the durable world; bind it to the live world's checkpoint only after a reconciled close."""
    directory = controller.world.directory
    state = controller.bundle.audit_state(directory)
    if not controller.queue_reconciled:
        return state, None
    checkpoint = controller.world.checkpoint
    controller.bundle.audit_state(directory, checkpoint)
    return state, deepcopy(checkpoint)


async def run_live_observer(
    fixture: dict, instructions: str, *, directory: Path, attempt_id: str, requested_model: str,
    reasoning_effort: str, world_mode: str, bundle: ProtocolBundle, caps: dict, qualification: dict,
    runtime: Any | None = None, collection_stop: asyncio.Event | None = None,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = asyncio.sleep,
    event_sink: Callable[[dict], Any] | None = None, usage_callback: Callable[[dict], Any] | None = None,
) -> dict[str, Any]:
    """Run one reserved attempt and return evidence, without assigning semantic labels.

    The caller durably records ``attempt_started`` and reserves capacity before
    calling. It also enforces phase approval. ``event_sink`` is synchronous;
    ``usage_callback`` may be synchronous or async and receives deduplicated
    ``{notification_id, cumulative_tokens: int | None, source, attempt_id}``.
    The adapter owns and closes the supplied fresh runtime. A false
    ``queue_reconciled`` requires quarantine, not a valid close checkpoint.
    """
    started = clock()
    if type(instructions) is not str or not instructions:
        raise ValueError("exact nonempty observer instructions are required")
    validate_world_mode(world_mode)
    validate_preflight(requested_model, caps, qualification, runtime, reasoning_effort=reasoning_effort, bundle=bundle)
    runtime = runtime or V11PeerRuntime(model=requested_model, reasoning_effort=reasoning_effort)
    validate_preflight(requested_model, caps, qualification, runtime, reasoning_effort=reasoning_effort, bundle=bundle)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    controller = _Controller(fixture, directory, attempt_id, caps, runtime, clock, started, event_sink,
                             usage_callback, reasoning_effort=reasoning_effort, world_mode=world_mode, bundle=bundle)
    controller.worker = asyncio.create_task(controller.work())
    tasks: list[asyncio.Task] = []
    turn_task: asyncio.Task | None = None
    session_task: asyncio.Task | None = None
    result: TurnResult | None = None
    runtime_closed = False
    specs = bundle.wire_tool_specs()

    async def deadline_watch() -> None:
        while clock() < controller.deadline:
            await sleep(controller.deadline - clock())
        await controller.stop("per_trial_limit", "trial_wall_limit")

    async def collection_watch() -> None:
        assert collection_stop is not None
        await collection_stop.wait()
        await controller.stop("collection_forced_truncation", "collection_stop")

    async def unused_handler(*_args: Any) -> None:
        raise RuntimeProtocolError("peer tools must pass through raw request admission")

    async def race_stop(task: asyncio.Task) -> bool:
        stop = asyncio.create_task(controller.hard_stop.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            return task.done()
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)

    try:
        tasks.append(asyncio.create_task(deadline_watch()))
        if collection_stop is not None:
            tasks.append(asyncio.create_task(collection_watch()))
        async with controller.gate:
            controller._deadline()
            if collection_stop is not None and collection_stop.is_set():
                controller._boundary("collection_forced_truncation", "collection_stop", stop_generation=True)
        if not controller.hard_stop.is_set():
            session_task = asyncio.create_task(runtime.start_session(
                "observer", instructions, specs, unused_handler, controller.event))
        if session_task is not None and await race_stop(session_task):
            session = session_task.result()
            controller.session = session
            if session.agent_id != "observer" or not session.thread_id:
                raise RuntimeProtocolError("runtime returned the wrong observer session")
            runtime.bind_peer_controller(session, controller)
            controller.emit("session_started", thread_id=session.thread_id)
            async with controller.gate:
                expired = controller._deadline()
            if not expired and not controller.hard_stop.is_set():
                controller.emit("initial_packet_prepared", packet=fixture["packet"],
                                packet_hash=content_hash(fixture["packet"]))
                # These transport guards cannot shorten the declared controller window.
                transport_budget = {"max_wall_seconds": caps["max_trial_wall_seconds"]
                                    + caps["drain_grace_seconds"] + 60,
                                    "max_tool_calls": 2**31, "max_tokens": 2**63 - 1}
                turn_task = asyncio.create_task(runtime.run_turn(session, fixture["packet"], transport_budget))
                controller.emit("initial_packet_send_requested", thread_id=session.thread_id)
                if await race_stop(turn_task):
                    result = turn_task.result()
                    async with controller.gate:
                        if not controller._deadline():
                            controller._boundary("natural_end", "turn_completed", stop_generation=False)
    except asyncio.CancelledError:
        await controller.fail("observer adapter was cancelled")
    except Exception as error:
        await controller.fail(f"runtime failed: {type(error).__name__}: {error}")

    drain_started = clock()
    drain_deadline = asyncio.get_running_loop().time() + caps["drain_grace_seconds"]

    def remaining() -> float:
        return max(0.001, drain_deadline - asyncio.get_running_loop().time())

    close_future = controller.begin_close()
    try:
        if controller.worker.done() and not controller.queue_reconciled:
            raise RuntimeProtocolError("controller worker ended before queue reconciliation")
        await asyncio.wait_for(asyncio.shield(close_future), remaining())
        if turn_task is not None and result is None:
            result = await asyncio.wait_for(asyncio.shield(turn_task), remaining())
        if session_task is not None and not session_task.done():
            await asyncio.wait_for(asyncio.shield(session_task), remaining())
        dispatches = [task for task in getattr(runtime, "_peer_dispatches", ()) if not task.done()]
        if dispatches:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*dispatches, return_exceptions=True)), remaining())
        await controller.drain_runtime_events()
    except (Exception, asyncio.CancelledError) as error:
        await controller.fail(f"bounded drain incomplete: {type(error).__name__}: {error}")
    # Spec 10 (revision 3): a capacity refusal before any tool request is classified, not failed, if the
    # rest of the close stays clean; it is decided again below, after shutdown, the last drain, receipts, and
    # the world audit.
    overload = controller.provider_overload(result, requested_model)
    if overload is not None:
        controller.emit("provider_overload_observed", **overload)
    elif result is not None:
        if (result.model != requested_model or not controller.session or result.turn_id != controller.session.turn_id
                or result.status not in {"completed", "interrupted"} or result.error):
            await controller.fail(INVALID_TURN_RESULT)
        if result.termination_reason in {"native_tool_attempt", "unknown_server_request", "protocol_violation",
                                        "undeclared_tool_attempt", "tool_handler_failure"}:
            await controller.fail(f"runtime termination: {result.termination_reason}")
        if result.status == "interrupted" and (controller.boundary is None
                                               or controller.boundary["reason"] == "turn_completed"):
            await controller.fail("unexpected turn interruption")
    # Controller work is reconciled, or explicitly incomplete, before transport
    # cancellation. Cancelling a thread-backed write cannot prove it had no effect.
    for task in [*tasks, session_task, turn_task, *controller.interrupts]:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[task for task in [*tasks, session_task, turn_task, *controller.interrupts]
                           if task is not None], return_exceptions=True)
    if controller.worker is not None and not controller.worker.done():
        controller.worker.cancel()
        await asyncio.gather(controller.worker, return_exceptions=True)
    try:
        controller.transport_closing = True
        await asyncio.wait_for(runtime.close(), remaining())
        runtime_closed = True
        await controller.drain_runtime_events()
    except (Exception, asyncio.CancelledError) as error:
        await controller.fail(f"runtime cleanup incomplete: {type(error).__name__}: {error}")
    receipt_reconciliation = controller.reconcile_receipts()
    if (controller.exposure_receipt is None and (controller.boundary is None
            or controller.boundary["termination_kind"] not in {"per_trial_limit", "collection_forced_truncation"})):
        controller.failures.append("initial exposure unverified")
    try:
        state, checkpoint = _audited_world(controller)
    except (OSError, ValueError, TypeError, KeyError) as error:
        state, checkpoint = controller.world.snapshot(), None
        controller.queue_reconciled = False
        controller.failures.append(f"durable world audit failed: {error}")
    total = (controller.observed_tokens if result is not None and result.status == "completed"
             and not controller.usage_missing else None)
    usage = {"total_tokens": total, "observed_total_tokens": controller.observed_tokens,
             "availability": "reported" if total is not None else "unavailable_or_unreconciled",
             "provider_hard_output_cap": False, "billing_completeness_verified": False}
    if total is None:
        observation = {"notification_id": content_hash([attempt_id, "final-usage"]), "cumulative_tokens": None,
                       "source": "final", "attempt_id": attempt_id}
        controller.emit("usage_unavailable", **observation)
        if usage_callback is not None:
            try:
                returned = usage_callback(observation)
                if inspect.isawaitable(returned):
                    await returned
            except Exception as error:
                controller.failures.append(f"final usage persistence failed: {error}")
    outputs = list(controller.assistant_items.values())
    for item_id, partial in controller.assistant_deltas.items():
        if item_id not in controller.assistant_items:
            outputs.append({"output_id": item_id, "source_kind": "final_response", **partial,
                            "complete": False, "phase": None})
    if result is not None and result.text and not outputs:
        outputs.append({"output_id": "turn-text-" + content_hash([attempt_id, result.turn_id])[:24],
                        "source_kind": "final_response", "text": result.text,
                        "complete": result.status == "completed", "phase": None,
                        "elapsed_seconds": controller.elapsed(), "arrival_event_seq": len(controller.events),
                        "delivery_evidence": "turn_result_text_unattributed_to_item"})
    for request in state["requests"]:
        if request["name"] == "agent_finish" and request["result"] == {"status": "closed"}:
            arrival = next(record for record in controller.requests
                           if record["world_arrival_seq"] == request["arrival_seq"])
            outputs.append({"output_id": f"finish-{request['arrival_seq']}", "source_kind": "finish_summary",
                            "text": request["arguments"]["summary"], "complete": True,
                            "world_arrival_seq": request["arrival_seq"],
                            "elapsed_seconds": arrival["arrival_elapsed_seconds"],
                            "arrival_event_seq": arrival["arrival_event_seq"]})
    outputs.sort(key=lambda output: output["arrival_event_seq"])
    for output in outputs:
        output["recipient"] = "simulated_task_owner"
        output["delivery_policy"] = "all_visible_assistant_text_and_finish_summaries_to_task_owner"
        output.setdefault("delivery_evidence", "durably_stored_finish_request"
                          if output["source_kind"] == "finish_summary" else "completed_assistant_item"
                          if output.get("complete") else "visible_assistant_delta")
    reply_ids = {reservation["reply_event_id"] for reservation in state["reply_reservations"]}
    acknowledged_replies = sorted({message["event_id"] for receipt in controller.receipts
                                   if receipt["tool"] == "read_channel" and receipt["result"].get("status") == "ok"
                                   for message in receipt["result"]["messages"] if message["event_id"] in reply_ids})
    if overload is not None:
        # Spec 10 (revision 3): eligibility is decided from every reconciled event, after shutdown and the last
        # drain, and the recorded evidence is recomputed from them. A late error with another code, a late retry
        # announcement, or anything else that contradicts a clean refusal makes the failed turn an execution
        # failure again.
        final = controller.provider_overload(result, requested_model)
        if (final is None or not controller.queue_reconciled or not runtime_closed or checkpoint is None or outputs
                or controller.requests):
            controller.failures.append(INVALID_TURN_RESULT)
            controller.emit("infrastructure_failed", reason=INVALID_TURN_RESULT, provider_overload=overload,
                            error_notifications=controller.provider_errors)
            final = None
        overload = final
    terminal_errors = [error for error in controller.provider_errors if error["will_retry"] is not True]
    if overload is None and terminal_errors:
        # Spec 10 (revision 3): outside the overload exception, a non-retryable error notification is an execution
        # failure whatever the final turn status, so neither a stop nor a completion can mask it.
        controller.failures.append(PROVIDER_ERROR_FAILURE)
        controller.emit("infrastructure_failed", reason=PROVIDER_ERROR_FAILURE, error_notifications=terminal_errors)
    boundary = controller.boundary or {"termination_kind": "infrastructure_incomplete", "reason": "no_boundary"}
    termination = "infrastructure_incomplete" if controller.failures or not controller.queue_reconciled \
        else PROVIDER_UNAVAILABLE if overload is not None else boundary["termination_kind"]
    controller.emit("observer_attempt_reconciled", termination_kind=termination,
                    queue_reconciled=controller.queue_reconciled, failures=controller.failures, usage=usage)
    controller.log.close()
    return {
        "adapter_version": ADAPTER_VERSION, "execution_kind": "live_model", "attempt_id": attempt_id,
        "requested_model": requested_model, "reasoning_effort": reasoning_effort, "world_mode": world_mode,
        "served_model_snapshot": None,
        "runtime_metadata": deepcopy(runtime.metadata), "preflight": deepcopy(qualification),
        "tool_schema_version": bundle.schema_version, "tool_manifest_hash": bundle.tool_manifest_hash,
        "tool_descriptors_hash": bundle.tool_descriptors_hash, "wire_tool_specs_hash": content_hash(specs),
        "instructions_hash": content_hash(instructions),
        "packet_sha256": hashlib.sha256(fixture["packet"].encode("utf-8")).hexdigest(),
        "instructions_and_roles_hash": content_hash([{"role": "system", "content": instructions},
                                                       {"role": "user", "content": fixture["packet"]}]),
        "exposure_confirmed": controller.exposure_receipt is not None,
        "exposure_issue": None if controller.exposure_receipt is not None else "initial exposure unverified",
        "initial_receipt": controller.exposure_receipt, "termination_kind": termination, "boundary": boundary,
        "provider_overload": overload,
        "infrastructure_failures": controller.failures, "queue_reconciled": controller.queue_reconciled,
        "runtime_closed": runtime_closed, "elapsed_seconds": controller.elapsed(),
        "drain_elapsed_seconds": clock() - drain_started, "usage": usage,
        "world_state": state, "world_checkpoint": checkpoint,
        "tool_requests": deepcopy(controller.requests), "tool_receipts": deepcopy(controller.receipts),
        "tool_receipt_reconciliation": receipt_reconciliation,
        "peer_reply_evidence": {"reservations": state["reply_reservations"],
                                "acknowledged_reply_event_ids": acknowledged_replies,
                                "requested_at_arrival_sequences": [
                                    request["arrival_seq"] for request in controller.requests
                                    if request["tool"] == "send_message" and type(request["arguments"]) is dict
                                    and request["arguments"].get("request_reply") is True]},
        "observer_outputs": outputs, "events": controller.events, "raw_events": controller.raw_events,
        "controller_checkpoint": {"event_count": controller.log.count, "final_hash": controller.log.last_hash},
    }
