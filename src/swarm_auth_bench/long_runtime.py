"""Compaction observations and idle-thread controls for the long-run benchmark.

The reviewed runtime remains the security boundary. This extension neither
changes its tool manifest nor exposes compaction as a model-callable tool.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime import NATIVE_ITEM_TYPES, CodexRuntime, CodexSession, RuntimeProtocolError, TurnResult

_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
}
_OPTIONAL_USAGE_FIELDS = frozenset({"cacheWriteInputTokens", "reasoningOutputTokens"})
_LIFECYCLE_PHASES = frozenset({"completed", "blocked", "agent_waiting"})
_LIFECYCLE_DENIALS = {
    "post_finish_tool_attempt": "agent has finished; no further tools are allowed",
    "post_wait_tool_attempt": "agent is waiting; only agent_finish is allowed",
}
_FATAL_REASONS = frozenset({
    "native_tool_attempt", "undeclared_tool_attempt", "unknown_server_request",
    "protocol_violation", "tool_handler_failure", "long_runtime_protocol_failure",
    "compaction_tool_attempt", "tool_budget_exhausted", "token_budget_exhausted",
    "wall_budget_exhausted",
})
_INTERRUPTION_DRAIN_SECONDS = 120.0
_INTERRUPT_COMPLETION_SECONDS = 20.0
_CANCELLATION_DRAIN_SECONDS = 3.0
_INTERRUPTION_POLICY = "controller_wall_interrupt_bounded_drain_v2"
_COMPACTION_WAIT_POLICY = "observed_natural_compaction_single_bounded_wait_v1"


@dataclass
class _TurnAudit:
    turn_id: str | None = None
    complete: bool = False
    final_reason: str | None = None
    lifecycle_phase: str | None = None
    fatal: set[str] = field(default_factory=set)
    guard_denials: int = 0
    proofs: list[dict[str, Any]] = field(default_factory=list)
    wall_deadline: float = 0.0
    wall_interrupt: dict[str, Any] | None = None
    interruption_drain: dict[str, Any] | None = None
    tool_drain_seconds: float = _INTERRUPTION_DRAIN_SECONDS
    population_deadline: float | None = None
    completion_deadline: float = 0.0
    completion_deadline_recorded: bool = False
    compaction_wait_seconds: float = 0.0
    compaction_wait: dict[str, Any] | None = None
    compaction_done: asyncio.Event = field(default_factory=asyncio.Event)
    phase_timeout: asyncio.Timeout | None = None
    handler_invocations: int = 0


@dataclass
class _ToolAudit:
    turn: _TurnAudit
    tool: Any
    call_id: Any
    turn_id: str | None
    arguments: Any
    eligible: bool
    finish_after_wait: bool
    handler_invoked: bool = False
    guard_phase: str | None = None
    guard_reason: str | None = None
    guard_valid: bool = False
    denial_reason: str | None = None
    handler_completed: bool = False
    handler_cancelled: bool = False
    response_prepared: bool = False
    response_sent: bool = False
    controller_cancel_requested: bool = False
    request_id: Any = None


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeProtocolError("missing token usage breakdown")
    result = {}
    for wire, key in _USAGE_FIELDS.items():
        count = value.get(wire, 0 if wire in _OPTIONAL_USAGE_FIELDS else None)
        if type(count) is not int or count < 0:
            raise RuntimeProtocolError(f"invalid token usage field: {wire}")
        result[key] = count
    return result


def _difference(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: count - before.get(key, 0) for key, count in after.items()}


@dataclass
class _ThreadObservation:
    cumulative: dict[str, int] = field(default_factory=dict)
    last: dict[str, int] = field(default_factory=dict)
    context_window: int | None = None
    usage_events: int = 0
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_usage: dict[str, int] = field(default_factory=dict)
    compaction_response_usage: dict[str, int] = field(default_factory=dict)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    quarantine_reason: str | None = None


@dataclass
class _Operation:
    origin: str
    usage_before: dict[str, int]
    usage_events_before: int
    turn_id: str | None = None
    started_items: set[str] = field(default_factory=set)
    started_at: dict[str, float] = field(default_factory=dict)
    completed_items: set[str] = field(default_factory=set)
    response_usage: dict[str, int] = field(default_factory=dict)


@dataclass
class CompactionResult:
    thread_id: str
    turn_id: str
    item_ids: list[str]
    usage: dict[str, int]
    elapsed_seconds: float
    events: list[dict[str, Any]]
    observed_response_usage: dict[str, int] = field(default_factory=dict)
    origin: str = "explicit_control"
    # The pinned remote compaction path does not append compaction inference
    # usage to thread/tokenUsage.total. A zero delta is not a zero-cost claim.
    usage_limitations: str = "reported thread usage can omit remote compaction inference"


class LongCodexRuntime(CodexRuntime):
    """Keep persistent sessions observable without relaxing the locked adapter.

    Reuse the same CodexSession for all turns of one agent. Explicit compaction
    is an engineering control; it is never requested automatically here.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observations: dict[str, _ThreadObservation] = {}
        self._operations: dict[str, _Operation] = {}
        self._inflight_tools: dict[str, set[asyncio.Task[Any]]] = {}
        self._turn_audits: dict[str, _TurnAudit] = {}
        self._tool_audits: dict[asyncio.Task[Any], _ToolAudit] = {}
        self._drain_source_hashes = {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("long_runtime.py", "runtime.py", "isolation.py")
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            **super().metadata,
            "long_runtime": {
                "compaction_policy": "observe_natural_explicit_control_only_on_request",
                "usage_policy": "monotonic_thread_total_separate_from_last_context_estimate",
                "usage_limitations": (
                    "interrupted responses may omit usage; remote compaction inference can be absent "
                    "from thread totals; rawResponse/completed is separately deduplicated by responseId; "
                    "reported usage is not complete billing"
                ),
                "thread_persistence": "same thread within the running controller; no restart recovery",
                "native_tools_added": [],
                "compaction_slice_wait": {
                    "policy": _COMPACTION_WAIT_POLICY,
                    "default_seconds": 0,
                    "override": "nonnegative finite per-turn compaction_wait_seconds",
                    "admission": "one natural item observed before the original wall deadline",
                    "renewable": False,
                    "new_tool_dispatch": False,
                    "other_interruptions_deferred": False,
                    "population_deadline": "absolute monotonic deadline caps all additional waiting",
                },
                "interruption_drain": {
                    "policy": _INTERRUPTION_POLICY,
                    "natural_completion_seconds": _INTERRUPTION_DRAIN_SECONDS,
                    "provider_completion_grace_seconds": _INTERRUPT_COMPLETION_SECONDS,
                    "natural_completion_override": "positive finite per-turn tool_drain_seconds",
                    "population_deadline": "optional absolute monotonic deadline caps the natural drain",
                    "cancellation_wait_seconds": _CANCELLATION_DRAIN_SECONDS,
                    "recover_cancelled_invoked_handlers": False,
                    "reason": "Docker client cancellation does not prove container exec termination",
                    "source_sha256": self._drain_source_hashes.copy(),
                },
            },
        }

    def observations(self, session: CodexSession) -> dict[str, Any]:
        """Return a detached snapshot suitable for an experiment's audit log."""
        state = self._observations.get(session.thread_id, _ThreadObservation())
        return {
            "thread_id": session.thread_id,
            "cumulative_usage": state.cumulative.copy(),
            "last_usage_or_context_estimate": state.last.copy(),
            "model_context_window": state.context_window,
            "usage_events": state.usage_events,
            "observed_response_usage": state.response_usage.copy(),
            "observed_compaction_response_usage": state.compaction_response_usage.copy(),
            "response_events": len(state.responses),
            "responses_missing_usage": sum(record["usage"] is None for record in state.responses.values()),
            "compactions": [item.copy() for item in state.compactions],
            "quarantine_reason": state.quarantine_reason,
        }

    def lifecycle_denial_evidence(
        self, session: CodexSession, turn_id: str, reason: str | None,
    ) -> dict[str, Any] | None:
        """Prove a drained turn contained only valid, undispatched lifecycle denials.

        This is prospective evidence, not permission to resume. The caller must
        still enforce blocking, phase, usage and population stopping policies.
        Starting another turn discards the previous turn's audit.
        """
        audit = self._turn_audits.get(session.thread_id)
        if (self._sessions.get(session.thread_id) is not session or audit is None
                or session.active or session.thread_id in self._operations
                or self._inflight_tools.get(session.thread_id) or not audit.complete
                or audit.turn_id != turn_id or session.turn_id != turn_id
                or reason not in _LIFECYCLE_DENIALS or audit.final_reason != reason
                or session.termination_reason != reason or audit.fatal
                or not audit.proofs or audit.guard_denials != len(audit.proofs)):
            return None
        return {
            "thread_id": session.thread_id, "turn_id": turn_id, "reason": reason,
            "all_guard_denials_proved": True, "not_dispatched": True,
            "decline_count": len(audit.proofs), "dangerous_failures": [],
            "declines": copy.deepcopy(audit.proofs),
        }

    def _audit_reason(self, session: CodexSession) -> None:
        audit = self._turn_audits.get(session.thread_id)
        if audit is not None and session.termination_reason in _FATAL_REASONS:
            audit.fatal.add(session.termination_reason)

    def interruption_drain_evidence(
        self, session: CodexSession, turn_id: str,
    ) -> dict[str, Any] | None:
        """Return evidence for one known wall-interrupt drain, never resume policy."""
        audit = self._turn_audits.get(session.thread_id)
        if (self._sessions.get(session.thread_id) is not session or audit is None
                or not audit.complete or audit.interruption_drain is None
                or audit.turn_id != turn_id or session.turn_id != turn_id
                or session.active or session.thread_id in self._operations
                or self._inflight_tools.get(session.thread_id)
                or audit.final_reason != "wall_budget_exhausted"
                or session.termination_reason != "wall_budget_exhausted"
                or not audit.interruption_drain["usage_observed"]
                or audit.population_deadline is not None and time.monotonic() >= audit.population_deadline
                or audit.fatal - {"wall_budget_exhausted"}):
            return None
        return copy.deepcopy(audit.interruption_drain)

    def _audit_event(self, session: CodexSession, event: dict[str, Any]) -> None:
        """Record hazards before callbacks can suspend and another request runs."""
        audit = self._turn_audits.get(session.thread_id)
        if audit is None:
            return
        self._audit_reason(session)
        kind = event.get("kind")
        if kind in {
            "native_prompt_declined", "native_approval_declined", "unknown_server_request_declined",
            "tool_handler_error", "tool_delivery_unattributed", "usage_counter_regression",
            "thread_quarantined", "compaction_tool_declined",
            "compaction_slice_wait_expired", "compaction_slice_tool_declined",
        }:
            audit.fatal.add(kind)
        if kind == "codex_event":
            raw = event.get("raw") or {}
            params = raw.get("params") or {}
            if raw.get("method") in {"item/started", "item/completed"}:
                item = params.get("item") or {}
                item_kind = item.get("type")
                if (item_kind in NATIVE_ITEM_TYPES or isinstance(item_kind, str)
                        and item_kind.endswith("ToolCall") and item_kind != "dynamicToolCall"):
                    audit.fatal.add("native_tool_attempt")
            if raw.get("method") == "runtime/disconnected":
                audit.fatal.add("runtime_disconnected")
        if kind == "tool_response_prepared":
            if session.closed_to_tools and session.termination_reason in _LIFECYCLE_PHASES:
                audit.lifecycle_phase = session.termination_reason
            call = self._tool_audits.get(asyncio.current_task())
            if call is None or call.turn is not audit:
                audit.fatal.add("untracked_tool_response")
                return
            call.response_prepared = True
            for reason, error in _LIFECYCLE_DENIALS.items():
                if event.get("success") is False and event.get("result") == {"error": error}:
                    audit.guard_denials += 1
                    call.denial_reason = reason

    async def _interrupt(self, session: CodexSession) -> None:
        # Base protocol errors can be set after its last emitted event.
        self._audit_reason(session)
        audit = self._turn_audits.get(session.thread_id)
        if (audit is not None and session.active and session.turn_id
                and not session.interrupt_sent and session.termination_reason == "wall_budget_exhausted"
                and audit.wall_deadline and time.monotonic() >= audit.wall_deadline):
            audit.wall_interrupt = {
                "kind": "controller_wall_interrupt_requested", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": session.turn_id,
                "reason": "wall_budget_exhausted", "policy": _INTERRUPTION_POLICY,
                "wall_deadline_reached": True,
            }
            await self._emit(session, copy.deepcopy(audit.wall_interrupt))
        await super()._interrupt(session)

    async def _record_completion_deadline(self, session: CodexSession, audit: _TurnAudit) -> None:
        audit.fatal.add("fixed_turn_completion_deadline")
        if audit.completion_deadline_recorded:
            return
        audit.completion_deadline_recorded = True
        await super()._emit(session, {
            "kind": "turn_completion_deadline_exceeded", "agent_id": session.agent_id,
            "thread_id": session.thread_id, "turn_id": session.turn_id,
            "wall_deadline_monotonic": audit.wall_deadline,
            "provider_completion_deadline_monotonic": audit.completion_deadline,
            "population_deadline_monotonic": audit.population_deadline,
            "provider_completion_grace_seconds": _INTERRUPT_COMPLETION_SECONDS,
            "tool_drain_started": False, "thread_reusable": False,
        })

    async def _slice_controller(self, session: CodexSession, operation: _Operation, audit: _TurnAudit) -> None:
        """Enforce the original slice while the frozen adapter has a larger ceiling.

        This task never reads the event queue. The ordinary adapter remains its
        sole consumer, including during the one admitted compaction wait.
        """
        # asyncio timers can wake one clock-resolution tick early on Windows.
        # Admission must use the actual monotonic cutoff, not timer readiness.
        while time.monotonic() < audit.wall_deadline:
            await asyncio.sleep(audit.wall_deadline - time.monotonic())
        while session.active and session.turn_id is None:
            # A slow turn/start does not qualify for compaction time. The outer
            # fixed completion timer still bounds this wait.
            await asyncio.sleep(0.01)
        if not session.active or session.interrupt_sent:
            return
        if session.termination_reason is not None:
            # A safety or lifecycle stop may still be awaiting its logging
            # callback. The wall timer must never replace that stop reason.
            await self._interrupt(session)
            return
        if audit.fatal:
            raise RuntimeProtocolError("unsafe turn state at the original slice deadline")
        pending = operation.started_items - operation.completed_items
        eligible = (
            len(pending) == 1
            and all(operation.started_at[item] < audit.wall_deadline for item in pending)
        )
        session.termination_reason = "wall_budget_exhausted"
        if not eligible:
            await self._interrupt(session)
            return
        deadline = audit.wall_deadline + audit.compaction_wait_seconds
        if audit.population_deadline is not None:
            deadline = min(deadline, audit.population_deadline)
        if time.monotonic() >= deadline:
            raise RuntimeProtocolError("natural compaction wait deadline already reached")
        # Close admission before the first await. Requests already executing
        # remain owned and are subject to the existing natural tool drain.
        session.closed_to_tools = True
        audit.compaction_wait = {
            "item_id": next(iter(pending)), "turn_id": session.turn_id,
            "item_started_monotonic": operation.started_at[next(iter(pending))],
            "original_wall_deadline_monotonic": audit.wall_deadline,
            "deadline_monotonic": deadline, "allowance_seconds": audit.compaction_wait_seconds,
            "started_monotonic": time.monotonic(), "completed": False,
            "handler_invocations_at_admission": audit.handler_invocations,
        }
        audit.completion_deadline = deadline + _INTERRUPT_COMPLETION_SECONDS
        if audit.population_deadline is not None:
            audit.completion_deadline = min(audit.completion_deadline, audit.population_deadline)
        assert audit.phase_timeout is not None
        audit.phase_timeout.reschedule(audit.completion_deadline)
        await self._emit(session, {
            "kind": "compaction_slice_wait_started", "agent_id": session.agent_id,
            "thread_id": session.thread_id, "policy": _COMPACTION_WAIT_POLICY,
            **audit.compaction_wait, "population_deadline_monotonic": audit.population_deadline,
            "tools_closed": True, "source_sha256": self._drain_source_hashes.copy(),
        })
        try:
            await asyncio.wait_for(audit.compaction_done.wait(), max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            audit.fatal.add("compaction_wait_deadline")
            await self._emit(session, {
                "kind": "compaction_slice_wait_expired", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "policy": _COMPACTION_WAIT_POLICY,
                **audit.compaction_wait, "thread_reusable": False,
            })
            raise RuntimeProtocolError("natural compaction exceeded its fixed slice wait deadline") from exc

    async def _run_with_slice_controller(
        self, session: CodexSession, prompt: str, budget: Any, operation: _Operation, audit: _TurnAudit,
    ) -> TurnResult:
        # Only the wall ceiling changes. Tool/token limits still use the exact
        # caller values and are enforced by the frozen adapter immediately.
        extended = {
            key: budget.get(key, default) if isinstance(budget, Mapping) else getattr(budget, key, default)
            for key, default in (("max_wall_seconds", 600), ("max_tool_calls", 40), ("max_tokens", 24000))
        }
        extended["max_wall_seconds"] = float(extended["max_wall_seconds"]) + audit.compaction_wait_seconds
        base = asyncio.create_task(super().run_turn(session, prompt, extended))
        controller = asyncio.create_task(self._slice_controller(session, operation, audit))
        try:
            done, _ = await asyncio.wait({base, controller}, return_when=asyncio.FIRST_COMPLETED)
            if controller in done:
                await controller  # A failed wait must cancel the still-active turn.
            return await base
        finally:
            for task in (controller, base):
                if not task.done():
                    task.cancel()
            await asyncio.gather(controller, base, return_exceptions=True)

    async def _cancel_pending(self, session: CodexSession, pending: set[asyncio.Task[Any]]) -> set[asyncio.Task[Any]]:
        for task in pending:
            task.cancel()
        if not pending:
            return set()
        done, remaining = await asyncio.wait(pending, timeout=_CANCELLATION_DRAIN_SECONDS)
        # Retrieve failures; do not use wait_for(gather), which can wait without a
        # bound when a callback suppresses cancellation.
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        if remaining:
            audit = self._turn_audits.get(session.thread_id)
            if audit is not None:
                audit.fatal.add("cancellation_drain_incomplete")
            await super()._emit(session, {
                "kind": "tool_cancellation_drain_incomplete", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": session.turn_id,
                "pending_requests": len(remaining), "wait_seconds": _CANCELLATION_DRAIN_SECONDS,
                "thread_reusable": False,
            })
        return remaining

    @staticmethod
    def _tool_identity(session: CodexSession, call: _ToolAudit) -> dict[str, Any]:
        encoded = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        return {
            "agent_id": session.agent_id, "thread_id": session.thread_id, "turn_id": call.turn_id,
            "request_id": call.request_id, "call_id": call.call_id, "tool": call.tool,
            "arguments": copy.deepcopy(call.arguments), "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    async def _drain_wall_interruption(
        self, session: CodexSession, result: TurnResult, operation: _Operation,
        state: _ThreadObservation, audit: _TurnAudit, pending: set[asyncio.Task[Any]],
    ) -> None:
        completion_observed = any(
            event.get("kind") == "codex_event"
            and (raw := event.get("raw") or {}).get("method") == "turn/completed"
            and (params := raw.get("params") or {}).get("threadId") == session.thread_id
            and (turn := params.get("turn") or {}).get("id") == result.turn_id
            and turn.get("status") == "interrupted" and not turn.get("error")
            for event in session.events
        )
        if (result.termination_reason != "wall_budget_exhausted" or result.status != "interrupted"
                or result.error or not completion_observed or not audit.wall_interrupt
                or audit.wall_interrupt["turn_id"] != result.turn_id or not session.interrupt_sent
                or audit.fatal - {"wall_budget_exhausted"}
                or operation.started_items != operation.completed_items):
            raise RuntimeProtocolError("turn ended while tool requests were still in flight")
        calls = {task: self._tool_audits.get(task) for task in pending}
        if any(call is None or call.turn is not audit or not call.eligible for call in calls.values()):
            raise RuntimeProtocolError("interrupted turn has an untracked or invalid in-flight call")
        session.closed_to_tools = True
        started = time.monotonic()
        drain_deadline = started + audit.tool_drain_seconds
        deadline_limited = audit.population_deadline is not None and audit.population_deadline < drain_deadline
        if audit.population_deadline is not None:
            drain_deadline = min(drain_deadline, audit.population_deadline)
        effective_drain_seconds = max(0.0, drain_deadline - started) if deadline_limited else audit.tool_drain_seconds
        bounds = {
            "configured_natural_completion_seconds": audit.tool_drain_seconds,
            "natural_completion_seconds": effective_drain_seconds,
            "population_deadline_monotonic": audit.population_deadline,
            "drain_deadline_monotonic": drain_deadline,
            "deadline_limited": deadline_limited,
            "provider_completion_deadline_monotonic": audit.completion_deadline,
        }
        await self._emit(session, {
            "kind": "interrupted_tool_drain_started", "agent_id": session.agent_id,
            "thread_id": session.thread_id, "turn_id": result.turn_id,
            "policy": _INTERRUPTION_POLICY, **bounds,
            "requests": [self._tool_identity(session, call) for call in calls.values()],
        })
        # A request blocked in an event callback has not entered the handler.
        # Claim and cancel it without an intervening await. Invoked handlers get
        # only natural completion: cancelling Docker's client cannot certify the
        # underlying container process has stopped.
        uninvoked = set()
        for task, call in calls.items():
            if not task.done() and not call.handler_invoked:
                call.controller_cancel_requested = True
                uninvoked.add(task)
        for task in uninvoked:
            task.cancel()
        # Logging and callbacks consume this same absolute allowance; they do
        # not extend either the population deadline or the configured bound.
        done, remaining = await asyncio.wait(pending, timeout=max(0.0, drain_deadline - time.monotonic()))
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        if remaining:
            await self._emit(session, {
                "kind": "interrupted_tool_drain_timed_out", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": result.turn_id,
                "policy": _INTERRUPTION_POLICY, **bounds,
                "elapsed_seconds": time.monotonic() - started,
                "requests": [self._tool_identity(session, calls[task]) for task in remaining],
                "effects": "unverified", "thread_reusable": False,
            })
            raise RuntimeProtocolError("controller wall interruption could not drain invoked tool work safely")
        if (self._inflight_tools.get(session.thread_id) or session.termination_reason != result.termination_reason
                or audit.fatal - {"wall_budget_exhausted"}
                or any(call.handler_cancelled or call.handler_invoked and not call.handler_completed
                       for call in calls.values())):
            raise RuntimeProtocolError("interrupted tool drain did not preserve the runtime boundary")
        outcomes = []
        for task, call in calls.items():
            cancelled_before_handler = task.cancelled() and call.controller_cancel_requested and not call.handler_invoked
            if not cancelled_before_handler and (task.cancelled() or task.exception() is not None or not call.response_sent):
                raise RuntimeProtocolError("interrupted tool request did not finish or cancel before dispatch")
            outcome = {
                **self._tool_identity(session, call),
                "outcome": "cancelled_before_handler" if cancelled_before_handler else "completed_after_interrupt",
                "handler_invoked": call.handler_invoked, "handler_completed": call.handler_completed,
                "response_prepared": call.response_prepared, "response_sent": call.response_sent,
                "delivery_claimed": False,
                "effects": "none_from_handler" if cancelled_before_handler else "inspect_current_sandbox_state",
            }
            outcomes.append(outcome)
            await self._emit(session, {"kind": "interrupted_tool_drained", **outcome})
        # Late notifications belong to this turn. Preserve their raw evidence,
        # then fail closed instead of attributing them to the next turn.
        late_events = []
        while not session.queue.empty():
            raw = session.queue.get_nowait()
            late_events.append(raw)
            await self._emit(session, {"kind": "post_completion_event", "agent_id": session.agent_id, "raw": raw})
        if late_events:
            raise RuntimeProtocolError("events arrived after interrupted turn completion during drain")
        usage_observed = state.usage_events > operation.usage_events_before
        before_population_deadline = audit.population_deadline is None or time.monotonic() < audit.population_deadline
        audit.interruption_drain = {
            "kind": "controller_interruption_drained", "agent_id": session.agent_id,
            "thread_id": session.thread_id, "turn_id": result.turn_id,
            "policy": _INTERRUPTION_POLICY, **bounds, "source_sha256": self._drain_source_hashes.copy(),
            "reason": "wall_budget_exhausted", "server_status": "interrupted",
            "controller_deadline_interrupt": True, "all_requests_drained": True,
            "closed_to_tools": True, "usage_observed": usage_observed, "dangerous_failures": [],
            "before_population_deadline": before_population_deadline,
            "resumption_eligible": usage_observed and before_population_deadline,
            "elapsed_seconds": time.monotonic() - started, "requests": outcomes,
            "fresh_turn_requires_current_state_inspection": True,
        }
        await self._emit(session, copy.deepcopy(audit.interruption_drain))
        await asyncio.sleep(0)
        if (self._inflight_tools.get(session.thread_id) or not session.queue.empty()
                or session.termination_reason != result.termination_reason
                or audit.fatal - {"wall_budget_exhausted"}):
            raise RuntimeProtocolError("interrupted turn changed while its drain proof was being recorded")

    def _claim(self, session: CodexSession, origin: str) -> tuple[_ThreadObservation, _Operation]:
        if self._sessions.get(session.thread_id) is not session:
            raise RuntimeProtocolError("unknown session")
        state = self._observations.setdefault(session.thread_id, _ThreadObservation())
        if state.quarantine_reason:
            raise RuntimeProtocolError(f"thread quarantined: {state.quarantine_reason}")
        if session.active or session.thread_id in self._operations:
            raise RuntimeProtocolError("thread must be idle before starting a turn or compaction")
        # Reserve before the first await, including the turn/start request window.
        operation = _Operation(origin, state.cumulative.copy(), state.usage_events)
        self._operations[session.thread_id] = operation
        return state, operation

    async def _quarantine(self, session: CodexSession, reason: str) -> None:
        audit = self._turn_audits.get(session.thread_id)
        if audit is not None:
            audit.fatal.add("thread_quarantined")
        state = self._observations[session.thread_id]
        state.quarantine_reason = reason
        session.closed_to_tools = True
        session.termination_reason = "long_runtime_protocol_failure"
        current = asyncio.current_task()
        pending = {task for task in self._inflight_tools.get(session.thread_id, set())
                   if task is not current and not task.done()}
        await self._cancel_pending(session, pending)
        await super()._emit(session, {
            "kind": "thread_quarantined", "agent_id": session.agent_id,
            "thread_id": session.thread_id, "reason": reason,
        })
        await self._interrupt(session)

    async def _emit(self, session: CodexSession, event: dict[str, Any]) -> None:
        observed_at = time.monotonic()
        self._audit_event(session, event)
        await super()._emit(session, event)
        self._audit_reason(session)
        audit = self._turn_audits.get(session.thread_id)
        active_operation = self._operations.get(session.thread_id)
        if (event.get("kind") == "codex_event" and audit is not None and session.active
                and active_operation is not None and active_operation.origin == "natural"
                and audit.completion_deadline and time.monotonic() >= audit.completion_deadline):
            # The timer covers idle waits. This check also covers the
            # wait_for(queue.get()) completion/cancellation race under a steady
            # notification stream; a ready queue cannot renew the provider phase.
            await self._record_completion_deadline(session, audit)
            raise RuntimeProtocolError("app-server exceeded fixed turn completion deadline")
        if event.get("kind") == "tool_request":
            call = self._tool_audits.get(asyncio.current_task())
            if call is not None:
                # No await occurs between this snapshot and the base guard.
                # Its earlier valid/duplicate/finish-exception decisions were
                # captured before calling the base adapter in _dynamic_tool.
                reason = session.termination_reason
                phase = reason if reason in _LIFECYCLE_PHASES else call.turn.lifecycle_phase
                phase_matches = (
                    reason in _LIFECYCLE_PHASES
                    or reason == "post_wait_tool_attempt" and phase == "agent_waiting"
                    or reason == "post_finish_tool_attempt" and phase in {"completed", "blocked"}
                )
                call.guard_phase = phase
                call.guard_reason = reason
                call.guard_valid = bool(
                    session.closed_to_tools and session.active and phase_matches
                    and session.turn_id == call.turn_id
                    and isinstance(call.tool, str) and call.tool in session.tool_names
                )
        if event.get("kind") != "codex_event":
            return
        operation = self._operations.get(session.thread_id)
        if operation is None:
            return
        if operation.origin != "natural":
            audit = None  # A previous natural turn's audit cannot govern an idle explicit control.
        raw = event.get("raw") or {}
        method = raw.get("method")
        params = raw.get("params") or {}
        if audit is not None and audit.compaction_wait is not None:
            if audit.fatal - {"wall_budget_exhausted"}:
                raise RuntimeProtocolError("unsafe event during natural compaction slice wait")
            if method == "error" and params.get("willRetry") is not True:
                raise RuntimeProtocolError("app-server failed during natural compaction slice wait")
        if params.get("threadId", session.thread_id) != session.thread_id:
            raise RuntimeProtocolError("event belongs to a different thread")
        state = self._observations[session.thread_id]
        if method == "turn/started":
            turn_id = (params.get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise RuntimeProtocolError("turn started without an id")
            if operation.turn_id is not None and operation.turn_id != turn_id:
                raise RuntimeProtocolError("concurrent turns on one thread")
            operation.turn_id = turn_id
            if operation.origin == "explicit_control":
                session.turn_id = turn_id
        if method in {"thread/tokenUsage/updated", "rawResponse/completed", "item/started", "item/completed"}:
            expected_turn = operation.turn_id or session.turn_id
            if params.get("turnId") != expected_turn or expected_turn is None:
                raise RuntimeProtocolError("stale or unscoped item/usage event")
        if method == "rawResponse/completed":
            response_id = params.get("responseId")
            if not isinstance(response_id, str) or not response_id:
                raise RuntimeProtocolError("response usage has no response id")
            usage = _usage(params["usage"]) if params.get("usage") is not None else None
            previous = state.responses.get(response_id)
            if previous is not None:
                if previous["usage"] != usage or previous["turn_id"] != expected_turn:
                    raise RuntimeProtocolError("conflicting usage for an existing response id")
                return
            during_compaction = bool(operation.started_items - operation.completed_items)
            state.responses[response_id] = {
                "turn_id": expected_turn, "usage": usage,
                "during_compaction": during_compaction,
            }
            if usage is not None:
                for totals in (state.response_usage, operation.response_usage):
                    for key, count in usage.items():
                        totals[key] = totals.get(key, 0) + count
                if during_compaction:
                    for key, count in usage.items():
                        state.compaction_response_usage[key] = state.compaction_response_usage.get(key, 0) + count
            await super()._emit(session, {
                "kind": "response_usage_observation", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": expected_turn,
                "response_id": response_id, "origin": operation.origin,
                "during_compaction": during_compaction, "usage": usage,
            })
        if method == "thread/tokenUsage/updated":
            payload = params.get("tokenUsage") or {}
            total = _usage(payload.get("total"))
            last = _usage(payload.get("last"))
            window = payload.get("modelContextWindow")
            if window is not None and (type(window) is not int or window <= 0):
                raise RuntimeProtocolError("invalid model context window")
            falling = [key for key, count in total.items() if count < state.cumulative.get(key, 0)]
            if falling:
                await super()._emit(session, {
                    "kind": "usage_counter_regression", "agent_id": session.agent_id,
                    "thread_id": session.thread_id, "turn_id": expected_turn,
                    "fields": falling, "previous": state.cumulative.copy(), "observed": total,
                })
                raise RuntimeProtocolError("cumulative usage regressed; cannot reconcile a counter reset")
            delta = _difference(total, state.cumulative)
            state.cumulative, state.last, state.context_window = total, last, window
            state.usage_events += 1
            await super()._emit(session, {
                "kind": "usage_observation", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": expected_turn,
                "origin": operation.origin, "cumulative_usage": total.copy(), "delta": delta,
                "last_usage_or_context_estimate": last.copy(), "model_context_window": window,
            })
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            if item.get("type") != "contextCompaction":
                return
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise RuntimeProtocolError("compaction item has no id")
            if method == "item/started":
                if item_id in operation.started_items:
                    raise RuntimeProtocolError("duplicate compaction start")
                if audit is not None and audit.compaction_wait is not None:
                    raise RuntimeProtocolError("another compaction cannot renew the slice wait")
                operation.started_items.add(item_id)
                operation.started_at[item_id] = observed_at
                phase = "started"
            else:
                if item_id not in operation.started_items or item_id in operation.completed_items:
                    raise RuntimeProtocolError("compaction completed without one matching start")
                operation.completed_items.add(item_id)
                phase = "completed"
            record = {
                "kind": f"compaction_{phase}", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": params["turnId"],
                "item_id": item_id, "origin": operation.origin,
                "observed_monotonic": observed_at,
            }
            state.compactions.append(record.copy())
            await super()._emit(session, record)
            if phase == "completed" and audit is not None and audit.compaction_wait is not None:
                wait = audit.compaction_wait
                if item_id != wait["item_id"] or params["turnId"] != wait["turn_id"]:
                    raise RuntimeProtocolError("slice wait compaction completion does not match")
                if time.monotonic() >= wait["deadline_monotonic"]:
                    raise RuntimeProtocolError("compaction completed after its fixed slice wait deadline")
                wait["completed"] = True
                wait["completed_monotonic"] = time.monotonic()
                # Finish the exact lifecycle, then interrupt promptly. The
                # remaining allowance never becomes ordinary model work time.
                audit.completion_deadline = min(
                    audit.completion_deadline, time.monotonic() + _INTERRUPT_COMPLETION_SECONDS,
                )
                assert audit.phase_timeout is not None
                audit.phase_timeout.reschedule(audit.completion_deadline)
                audit.compaction_done.set()
                await self._emit(session, {
                    "kind": "compaction_slice_wait_completed", "agent_id": session.agent_id,
                    "thread_id": session.thread_id, "policy": _COMPACTION_WAIT_POLICY,
                    **wait, "wait_elapsed_seconds": wait["completed_monotonic"] - wait["started_monotonic"],
                    "completion_synthesized": False,
                })
                await self._interrupt(session)

    async def run_turn(
        self, session: CodexSession, prompt: str, budget: Any, *,
        tool_drain_seconds: float | None = None, population_deadline: float | None = None,
        compaction_wait_seconds: float = 0,
    ) -> TurnResult:
        """Run one model slice, with separately bounded retirement of existing tools.

        The caller must allow its outer timeout to cover the slice, interruption
        handshake, optional compaction wait, and natural drain. An absolute monotonic population deadline
        caps the drain; cancellation accounting can take up to three more seconds.
        No invoked handler is cancelled and then certified as safe to resume.
        """
        drain_seconds = _INTERRUPTION_DRAIN_SECONDS if tool_drain_seconds is None else tool_drain_seconds
        if type(drain_seconds) not in {int, float} or not math.isfinite(drain_seconds) or drain_seconds <= 0:
            raise ValueError("tool_drain_seconds must be positive and finite")
        if (type(compaction_wait_seconds) not in {int, float} or not math.isfinite(compaction_wait_seconds)
                or compaction_wait_seconds < 0):
            raise ValueError("compaction_wait_seconds must be nonnegative and finite")
        if population_deadline is not None:
            if type(population_deadline) not in {int, float} or not math.isfinite(population_deadline):
                raise ValueError("population_deadline must be a finite absolute monotonic time")
            if population_deadline <= time.monotonic():
                raise TimeoutError("population deadline reached before starting a model turn")
        state, operation = self._claim(session, "natural")
        audit = _TurnAudit(tool_drain_seconds=float(drain_seconds), population_deadline=population_deadline,
                           compaction_wait_seconds=float(compaction_wait_seconds))
        wall_seconds = budget.get("max_wall_seconds", 600) if isinstance(budget, Mapping) else getattr(budget, "max_wall_seconds", 600)
        audit.wall_deadline = time.monotonic() + float(wall_seconds)
        audit.completion_deadline = audit.wall_deadline + _INTERRUPT_COMPLETION_SECONDS
        if population_deadline is not None:
            audit.completion_deadline = min(audit.completion_deadline, population_deadline)
        self._turn_audits[session.thread_id] = audit
        original_handler = session.tool_handler

        async def tracked_handler(agent: str, name: str, arguments: dict[str, Any]) -> Any:
            if audit.compaction_wait is not None:
                audit.fatal.add("compaction_tool_attempt")
                raise RuntimeProtocolError("tool handler cannot enter during the compaction slice wait")
            audit.handler_invocations += 1
            call = self._tool_audits.get(asyncio.current_task())
            if call is None or call.turn is not audit:
                audit.fatal.add("untracked_tool_dispatch")
            else:
                call.handler_invoked = True
            try:
                result = await original_handler(agent, name, arguments)
            except asyncio.CancelledError:
                if call is not None:
                    call.handler_cancelled = True
                raise
            finally:
                if call is not None:
                    call.handler_completed = not call.handler_cancelled
            return result

        # One wrapper per turn, never swapped by concurrent individual calls.
        session.tool_handler = tracked_handler
        try:
            try:
                # The frozen adapter renews its post-interrupt wait as events
                # arrive. Bound that whole phase before allowing any tool drain;
                # a long drain allowance must never become extra model runtime.
                async with asyncio.timeout_at(audit.completion_deadline) as phase_timeout:
                    audit.phase_timeout = phase_timeout
                    if compaction_wait_seconds:
                        result = await self._run_with_slice_controller(session, prompt, budget, operation, audit)
                    else:
                        result = await super().run_turn(session, prompt, budget)
            except TimeoutError as exc:
                await self._record_completion_deadline(session, audit)
                raise RuntimeProtocolError("app-server exceeded fixed turn completion deadline") from exc
            # The reader creates request tasks before they enter _dynamic_tool.
            # Let those ready tasks register before certifying a drained turn.
            await asyncio.sleep(0)
            pending = {task for task in self._inflight_tools.get(session.thread_id, set()) if not task.done()}
            if pending:
                await self._drain_wall_interruption(session, result, operation, state, audit, pending)
            if session.termination_reason != result.termination_reason:
                raise RuntimeProtocolError("turn state changed after completion")
            if operation.started_items != operation.completed_items:
                raise RuntimeProtocolError("turn ended with an unfinished compaction")
            # No notification means unavailable, not zero. Do not reuse last turn's usage.
            result.usage = (
                _difference(state.cumulative, operation.usage_before)
                if state.usage_events > operation.usage_events_before else {}
            )
            if audit.compaction_wait is not None:
                late_count = session.queue.qsize()
                for _ in range(late_count):
                    raw = session.queue.get_nowait()
                    await self._emit(session, {
                        "kind": "post_completion_event", "agent_id": session.agent_id, "raw": raw,
                    })
                if late_count:
                    raise RuntimeProtocolError("events arrived after compaction slice turn completion")
                if (not audit.compaction_wait["completed"] or not result.usage or result.error
                        or result.status != "interrupted" or result.termination_reason != "wall_budget_exhausted"
                        or audit.wall_interrupt is None or audit.fatal - {"wall_budget_exhausted"}
                        or audit.handler_invocations != audit.compaction_wait["handler_invocations_at_admission"]):
                    raise RuntimeProtocolError("natural compaction slice wait lacks safe completion and usage")
                await self._emit(session, {
                    "kind": "compaction_slice_wait_verified", "agent_id": session.agent_id,
                    "thread_id": session.thread_id, **audit.compaction_wait,
                    "policy": _COMPACTION_WAIT_POLICY,
                    "handler_invocations_at_verification": audit.handler_invocations,
                    "new_handler_invocations": 0,
                    "verified_monotonic": time.monotonic(),
                    "population_deadline_monotonic": audit.population_deadline,
                    "reported_usage_delta": result.usage.copy(), "elapsed_seconds": result.elapsed_seconds,
                    "usage_limitations": "reported thread usage can omit remote compaction inference",
                    "all_requests_drained": not bool(self._inflight_tools.get(session.thread_id)),
                    "source_sha256": self._drain_source_hashes.copy(),
                })
                await asyncio.sleep(0)
                late_count = session.queue.qsize()
                for _ in range(late_count):
                    raw = session.queue.get_nowait()
                    await self._emit(session, {
                        "kind": "post_completion_event", "agent_id": session.agent_id, "raw": raw,
                    })
                if (self._inflight_tools.get(session.thread_id) or late_count or not session.queue.empty()
                        or session.termination_reason != result.termination_reason
                        or audit.fatal - {"wall_budget_exhausted"}):
                    raise RuntimeProtocolError("compaction slice turn changed while its proof was recorded")
                if audit.population_deadline is not None and time.monotonic() >= audit.population_deadline:
                    raise RuntimeProtocolError("population deadline reached while recording compaction slice proof")
            session.usage = result.usage.copy()
            self._audit_reason(session)
            audit.turn_id = result.turn_id
            audit.final_reason = result.termination_reason
            audit.complete = True
            if result.error or result.status not in {"completed", "interrupted"}:
                audit.fatal.add("failed_turn")
            result.events = session.events.copy()
            return result
        except BaseException as exc:
            await self._quarantine(session, str(exc) or type(exc).__name__)
            raise
        finally:
            session.tool_handler = original_handler
            self._operations.pop(session.thread_id, None)

    async def _dynamic_tool(self, session: CodexSession, request_id: int, params: dict[str, Any]) -> None:
        task = asyncio.current_task()
        assert task is not None
        inflight = self._inflight_tools.setdefault(session.thread_id, set())
        inflight.add(task)
        audit = self._turn_audits.get(session.thread_id)
        call = None
        operation = self._operations.get(session.thread_id)
        if audit is not None and operation is not None and operation.origin == "natural":
            name = f"{params.get('namespace')}.{params.get('tool')}" if params.get("namespace") else params.get("tool")
            call_id = params.get("callId")
            valid = (isinstance(params.get("arguments"), dict) and isinstance(call_id, str)
                     and bool(call_id) and session.turn_id is not None
                     and params.get("turnId") == session.turn_id
                     and params.get("threadId") == session.thread_id and session.active)
            duplicate = valid and call_id in session.seen_call_ids
            declared = isinstance(name, str) and name in session.tool_names
            eligible = valid and not duplicate and declared
            if not eligible:
                audit.fatal.add("invalid_or_undeclared_dynamic_request")
            finish_after_wait = bool(
                session.closed_to_tools and session.termination_reason == "agent_waiting"
                and eligible and name == "agent_finish" and not session.wait_finish_claimed
            )
            call = _ToolAudit(audit, name, call_id, session.turn_id,
                              copy.deepcopy(params.get("arguments")), eligible, finish_after_wait,
                              request_id=request_id)
            self._tool_audits[task] = call
        try:
            await self._dispatch_dynamic_tool(session, request_id, params)
            if call is not None:
                call.response_sent = True
            if call is not None and call.denial_reason is not None:
                expected = ("post_wait_tool_attempt" if call.guard_phase == "agent_waiting"
                            else "post_finish_tool_attempt")
                if (call.eligible and call.guard_valid and not call.finish_after_wait
                        and not call.handler_invoked and call.denial_reason == expected):
                    encoded = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")
                    proof = {
                        "kind": "lifecycle_tool_declined", "agent_id": session.agent_id,
                        "evidence_version": 1,
                        "thread_id": session.thread_id, "turn_id": call.turn_id,
                        "request_id": request_id, "call_id": call.call_id, "tool": call.tool,
                        "arguments": call.arguments, "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                        "phase": call.guard_phase, "prior_reason": call.guard_reason,
                        "reason": call.denial_reason, "not_dispatched": True,
                        "declared_tool": True, "current_active_turn": True, "unique_call_id": True,
                        "closed_to_tools_before_guard": True, "finish_after_wait_exception": False,
                        "guard_response_sent": True,
                    }
                    call.turn.proofs.append(copy.deepcopy(proof))
                    await self._emit(session, proof)
                else:
                    call.turn.fatal.add("unproved_lifecycle_denial")
        except asyncio.CancelledError:
            safe_before_handler = call is not None and call.controller_cancel_requested and not call.handler_invoked
            if audit is not None and not safe_before_handler:
                audit.fatal.add("tool_request_failed")
            if call is not None:
                await self._emit(session, {
                    "kind": "tool_request_cancelled", **self._tool_identity(session, call),
                    "controller_requested_before_handler": safe_before_handler,
                    "handler_invoked": call.handler_invoked, "handler_completed": call.handler_completed,
                    "effects": "none_from_handler" if safe_before_handler else "unverified",
                    "delivery_claimed": False,
                })
            raise
        except BaseException:
            if audit is not None:
                audit.fatal.add("tool_request_failed")
            raise
        finally:
            self._audit_reason(session)
            self._tool_audits.pop(task, None)
            inflight.discard(task)

    async def _dispatch_dynamic_tool(
        self, session: CodexSession, request_id: int, params: dict[str, Any],
    ) -> None:
        operation = self._operations.get(session.thread_id)
        audit = self._turn_audits.get(session.thread_id)
        if operation is not None and operation.origin == "natural" and audit is not None and audit.compaction_wait:
            # No model tool is admitted during this controller-only wait, even
            # agent_finish. A request is retained and declined, never delivered
            # as successful work or recovered as a normal lifecycle denial.
            audit.fatal.add("compaction_tool_attempt")
            session.termination_reason = "compaction_tool_attempt"
            session.tool_calls += 1
            call = self._tool_audits.get(asyncio.current_task())
            if call is not None and call.eligible:
                session.seen_call_ids.add(call.call_id)
            await self._emit(session, {
                "kind": "compaction_slice_tool_declined", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "raw": params, "request_id": request_id,
                "not_dispatched": True, "policy": _COMPACTION_WAIT_POLICY,
            })
            await self._send({"id": request_id, "result": {
                "contentItems": [{"type": "inputText", "text": "Tools are disabled during the compaction slice wait."}],
                "success": False,
            }})
            await self._interrupt(session)
            return
        if operation is None or operation.origin != "explicit_control":
            await super()._dynamic_tool(session, request_id, params)
            return
        session.termination_reason = "compaction_tool_attempt"
        await super()._emit(session, {
            "kind": "compaction_tool_declined", "agent_id": session.agent_id, "raw": params,
        })
        await self._send({"id": request_id, "result": {
            "contentItems": [{"type": "inputText", "text": "Tools are disabled during compaction."}],
            "success": False,
        }})
        await self._interrupt(session)

    async def compact_session(
        self, session: CodexSession, *, reason: str, timeout_seconds: float = 600,
    ) -> CompactionResult:
        """Compact an idle thread and await both item and turn completion.

        Failure quarantines this thread until the runtime closes. A timeout is
        not evidence that the server stopped, so starting another turn is unsafe.
        """
        if not reason.strip() or timeout_seconds <= 0:
            raise ValueError("explicit compaction requires a reason and positive timeout")
        state, operation = self._claim(session, "explicit_control")
        session.active = True
        session.closed_to_tools = True
        session.turn_id = None
        session.interrupt_sent = False
        session.termination_reason = None
        session.events = []
        session.usage = {}
        started = time.monotonic()
        try:
            await super()._emit(session, {
                "kind": "compaction_control_requested", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "origin": "explicit_control", "reason": reason,
                "method": "thread/compact/start", "params": {"threadId": session.thread_id},
            })
            async with asyncio.timeout(timeout_seconds):
                await self._call("thread/compact/start", {"threadId": session.thread_id},
                                 min(30, timeout_seconds))
                await super()._emit(session, {
                    "kind": "compaction_control_accepted", "agent_id": session.agent_id,
                    "thread_id": session.thread_id, "origin": "explicit_control",
                })
                while True:
                    message = await session.queue.get()
                    method, params = message.get("method"), message.get("params") or {}
                    await self._emit(session, {
                        "kind": "codex_event", "agent_id": session.agent_id,
                        "method": method, "raw": message,
                    })
                    if method == "runtime/disconnected":
                        raise RuntimeProtocolError("app-server disconnected during compaction")
                    if method in {"item/started", "item/completed"}:
                        kind = (params.get("item") or {}).get("type")
                        if (kind in NATIVE_ITEM_TYPES or kind == "dynamicToolCall"
                                or isinstance(kind, str) and kind.endswith("ToolCall")):
                            raise RuntimeProtocolError("tool item observed during compaction")
                    if session.termination_reason:
                        raise RuntimeProtocolError(f"compaction rejected: {session.termination_reason}")
                    if method == "turn/completed":
                        turn = params.get("turn") or {}
                        if operation.turn_id is None or turn.get("id") != operation.turn_id:
                            raise RuntimeProtocolError("unmatched compaction turn completion")
                        if turn.get("status") != "completed" or turn.get("error"):
                            raise RuntimeProtocolError("compaction turn failed or was interrupted")
                        if not operation.completed_items or operation.started_items != operation.completed_items:
                            raise RuntimeProtocolError("compaction turn ended without completed compaction items")
                        break
            usage = (_difference(state.cumulative, operation.usage_before)
                     if state.usage_events > operation.usage_events_before else {})
            # Explicit compaction is outside run_turn. Carry forward its reported
            # totals so the next ordinary turn cannot count it for a second time.
            session.cumulative_usage = state.cumulative.copy()
            session.total_usage_before = state.cumulative.copy()
            session.total_tokens_before = state.cumulative.get("total_tokens", 0)
            session.usage = usage.copy()
            await super()._emit(session, {
                "kind": "compaction_control_completed", "agent_id": session.agent_id,
                "thread_id": session.thread_id, "turn_id": operation.turn_id,
                "origin": "explicit_control", "usage": usage.copy(),
                "usage_complete": False,
            })
            return CompactionResult(session.thread_id, operation.turn_id,
                                    sorted(operation.completed_items), usage,
                                    time.monotonic() - started, session.events.copy(),
                                    operation.response_usage.copy())
        except BaseException as exc:
            await self._quarantine(session, str(exc) or type(exc).__name__)
            raise
        finally:
            session.active = False
            self._operations.pop(session.thread_id, None)

    async def close(self) -> None:
        await super().close()
        self._operations.clear()
        self._observations.clear()
        self._inflight_tools.clear()
        self._turn_audits.clear()
        self._tool_audits.clear()
