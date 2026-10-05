"""Lanes, frozen caps, execution authorization, and the global admission policy.

A lane is one model at one reasoning effort. Each lane runs its assignments one
at a time in its own sealed phase directory, with its own journal and budget
ledger. A global slot limits the attempts that run at once across all lanes to
``global_max_concurrency`` (at most six).

The admission policy is ported from an earlier unpublished multi-lane study. Any
failed execution check holds all new admission. So do an unsettled usage
record, the admission cutoff, the forced-stop deadline, a parent stop file, a
halted lane, a stopped lane ledger, and retained incomplete or failed evidence.
Unknown final usage alone does not hold when the world closed and the runtime
shut down cleanly: the attempt settles at the larger of its observed usage and
its reservation (``bounded_by_reservation``, spec section 10). Active attempts
finish within their own caps. At the forced-stop deadline they are truncated
and drained. A hold is never lifted inside a run, and an archived failed
attempt holds every later run of the same plan.

This module performs no model call and imports no v1.1 content module.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from ..events import content_hash
from ..peer_reporting.config import MODELS, validate_caps
from ..peer_reporting.storage import check_seal
from . import PROTOCOL_ID

EFFORTS = ("xhigh", "low")
WORLD_MODES = ("normal", "report_store_unavailable")
PROMPT_CONDITIONS = ("neutral", "guided", "discouraged")
PHASES = ("compatibility", "calibration", "smoke", "collection")
MAX_GLOBAL_CONCURRENCY = 6
TOOL_REQUEST_CAP = 32
CAPS_KIND = "peer_reporting_v11_caps"
CAPS_STATUSES = ("candidate", "frozen")
TRIAL_CAP_FIELDS = ("max_trial_wall_seconds", "drain_grace_seconds", "max_tool_requests_per_trial",
                    "trial_observed_token_stop_target", "reserved_tokens_per_trial")
AUTHORIZATION_KIND = "peer_reporting_v11_execution_authorization"
AUTHORIZATION_VERSION = "peer-reporting-v11-authorization-v1"
AUTHORIZATION_FIELDS = frozenset({
    "kind", "schema_version", "protocol_id", "phase", "live_plan_hash", "caps_hash", "maximum_live_calls",
    "admission_cutoff_utc", "forced_stop_deadline_utc", "authorization", "recorded_utc", "seal_hash",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
BOUNDED_USAGE = "bounded_by_reservation"
SETTLED_USAGE = ("settled", BOUNDED_USAGE)


def validate_effort(value: Any) -> str:
    if type(value) is not str or value not in EFFORTS:
        raise ValueError(f"reasoning effort must be one of {EFFORTS}")
    return value


def validate_world_mode(value: Any) -> str:
    if type(value) is not str or value not in WORLD_MODES:
        raise ValueError(f"world mode must be one of {WORLD_MODES}")
    return value


def validate_phase(value: Any) -> str:
    if type(value) is not str or value not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    return value


def validate_identifier(value: Any, description: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{description} must be a bounded identifier")
    return value


def lane_id(model: str, effort: str) -> str:
    if model not in MODELS:
        raise ValueError("lane model must be an approved model ID")
    return f"{model}-{validate_effort(effort)}"


def lane_order() -> list[tuple[str, str]]:
    """Every model at xhigh, then every model at low, in protocol model order."""
    return [(model, effort) for effort in EFFORTS for model in MODELS]


# Frozen caps


def _positive(value: Any, description: str, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and type(value) is not int):
        raise ValueError(f"{description} must be a positive {'integer' if integer else 'number'}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{description} must be finite and positive")


def validate_caps_record(record: Any, *, require_frozen: bool = True) -> dict:
    """Check a v1.1 caps record: per-trial caps, per-phase lane wall clocks, global concurrency.

    Per-lane P1-format caps are derived from it by ``lane_caps``. The tool request
    cap is the protocol's 32. Live phases require ``caps_status == "frozen"``.
    """
    fields = {"kind", "protocol_id", "revision", "caps_status", "trial", "lane_wall_seconds",
              "global_max_concurrency"}
    if type(record) is not dict or set(record) != fields:
        raise ValueError(f"caps record must contain exactly {sorted(fields)}")
    if record["kind"] != CAPS_KIND or record["protocol_id"] != PROTOCOL_ID:
        raise ValueError("not a v1.1 caps record")
    validate_identifier(record["revision"], "caps revision")
    if record["caps_status"] not in CAPS_STATUSES:
        raise ValueError(f"caps_status must be one of {CAPS_STATUSES}")
    if require_frozen and record["caps_status"] != "frozen":
        raise ValueError("live phases require frozen caps; no model session was created")
    trial = record["trial"]
    if type(trial) is not dict or set(trial) != set(TRIAL_CAP_FIELDS):
        raise ValueError(f"trial caps must contain exactly {list(TRIAL_CAP_FIELDS)}")
    if trial["max_tool_requests_per_trial"] != TOOL_REQUEST_CAP or type(trial["max_tool_requests_per_trial"]) is not int:
        raise ValueError(f"the v1.1 tool request cap is {TOOL_REQUEST_CAP}")
    walls = record["lane_wall_seconds"]
    if type(walls) is not dict or set(walls) != set(PHASES):
        raise ValueError(f"lane_wall_seconds must name exactly {list(PHASES)}")
    for phase, value in walls.items():
        _positive(value, f"lane_wall_seconds.{phase}")
    concurrency = record["global_max_concurrency"]
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_GLOBAL_CONCURRENCY:
        raise ValueError(f"global_max_concurrency must be an integer from 1 to {MAX_GLOBAL_CONCURRENCY}")
    for phase in PHASES:
        lane_caps(record, phase, 1, _validated=True)
    return deepcopy(record)


def lane_caps(record: dict, phase: str, planned_trials: int, *, _validated: bool = False) -> dict:
    """P1-format caps for one lane: serial, with one reservation per planned trial as its token target."""
    if not _validated:
        validate_caps_record(record, require_frozen=False)
    validate_phase(phase)
    if type(planned_trials) is not int or planned_trials < 1:
        raise ValueError("a lane needs at least one planned trial")
    caps = {**record["trial"], "collection_wall_seconds": record["lane_wall_seconds"][phase],
            "collection_observed_token_stop_target": record["trial"]["reserved_tokens_per_trial"] * planned_trials,
            "max_concurrency": 1}
    validate_caps(caps)
    return caps


def trial_policy(record: dict) -> dict:
    """The per-trial limits announced in the instructions; identical for every lane and phase."""
    return dict(record["trial"])


# Execution authorization


def parse_utc(value: Any, description: str) -> float:
    if type(value) is not str:
        raise ValueError(f"{description} must be an ISO-8601 time with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{description} must be an ISO-8601 time with a timezone") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{description} requires a timezone")
    return parsed.timestamp()


def validate_authorization(record: Any, plan: dict) -> dict:
    """Require an explicit, sealed user authorization of exactly this sealed live plan.

    Returns the record with parsed ``admission_cutoff`` and ``forced_stop_deadline``
    timestamps. Any mismatch refuses the phase before a runtime is created.
    """
    check_seal(record)
    if set(record) != AUTHORIZATION_FIELDS:
        raise ValueError(f"authorization must contain exactly {sorted(AUTHORIZATION_FIELDS)}")
    if (record["kind"] != AUTHORIZATION_KIND or record["schema_version"] != AUTHORIZATION_VERSION
            or record["protocol_id"] != PROTOCOL_ID):
        raise ValueError("not a v1.1 execution authorization")
    if (record["phase"] != plan.get("phase") or record["live_plan_hash"] != plan.get("seal_hash")
            or record["caps_hash"] != plan.get("caps_hash")
            or record["maximum_live_calls"] != plan.get("maximum_live_calls")):
        raise ValueError("authorization names another phase, plan, caps record, or call count")
    approval = record["authorization"]
    if (type(approval) is not dict or set(approval) != {"status", "text"} or approval["status"] != "approved"
            or type(approval["text"]) is not str or not approval["text"].strip() or len(approval["text"]) > 20000):
        raise ValueError("authorization requires approved status and the user's explicit text")
    parse_utc(record["recorded_utc"], "recorded_utc")
    cutoff = parse_utc(record["admission_cutoff_utc"], "admission_cutoff_utc")
    deadline = parse_utc(record["forced_stop_deadline_utc"], "forced_stop_deadline_utc")
    trial = (plan.get("caps") or {}).get("trial") or {}
    window = [trial.get(key) for key in ("max_trial_wall_seconds", "drain_grace_seconds")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in window):
        raise ValueError("the sealed plan lacks the trial wall and drain caps")
    # A trial admitted at the cutoff must be able to close and drain before the deadline.
    if cutoff + sum(window) > deadline:
        raise ValueError("the admission cutoff must precede the forced-stop deadline by at least the trial wall "
                         "plus the drain time")
    return {**deepcopy(record), "admission_cutoff": cutoff, "forced_stop_deadline": deadline}


# Global admission policy


def attempt_hold_kinds(check_passed: Any, failure_reasons: Any, usage_settlement: Any) -> list[str]:
    """Why an archived attempt holds all new admission; empty when it does not.

    A failed execution check holds, except a check whose only failure is unknown
    usage after a clean close that settled at the reservation bound. Any
    settlement other than ``settled`` or ``bounded_by_reservation`` holds.
    """
    kinds = []
    bounded_only = usage_settlement == BOUNDED_USAGE and set(failure_reasons or []) <= {"usage_known"}
    if check_passed is not True and not bounded_only:
        kinds.append("execution_check_failure")
    if usage_settlement not in SETTLED_USAGE:
        kinds.append("unknown_final_usage")
    return kinds


class AdmissionPolicy:
    """Holds are one-way. Any reason recorded here refuses every later admission."""

    def __init__(self, *, admission_cutoff: float, forced_stop_deadline: float,
                 wall_clock: Callable[[], float] = time.time, stop_file: Path | None = None,
                 stop_files: Iterable[Path] = (), on_change: Callable[[], None] | None = None) -> None:
        if not admission_cutoff < forced_stop_deadline:
            raise ValueError("the admission cutoff must precede the forced-stop deadline")
        self.admission_cutoff, self.forced_stop_deadline = admission_cutoff, forced_stop_deadline
        self.wall_clock, self.on_change = wall_clock, on_change
        self.stop_files = [Path(path) for path in ([stop_file] if stop_file is not None else []) + list(stop_files)]
        self.holds: list[str] = []

    def hold(self, reason: str) -> None:
        if reason not in self.holds:
            self.holds.append(reason)
            if self.on_change is not None:
                self.on_change()

    def force_stop_due(self) -> bool:
        return any(path.exists() for path in self.stop_files) or self.wall_clock() >= self.forced_stop_deadline

    def admission_check(self) -> dict | None:
        """Return a hold record, or None when a new attempt may be admitted now."""
        if self.force_stop_due():
            self.hold("parent_stop_or_forced_deadline")
        elif self.wall_clock() >= self.admission_cutoff:
            self.hold("admission_cutoff")
        return {"reason": "global_admission_hold", "holds": list(self.holds)} if self.holds else None

    def accept_archived(self, payload: dict) -> None:
        """Stop all new admission after a failed execution check or an unsettled usage record."""
        check = payload.get("check") or {}
        settlement = ((payload.get("orchestrator") or {}).get("usage_settlement") or {}).get("status")
        for kind in attempt_hold_kinds(check.get("passed"), check.get("failure_reasons"), settlement):
            self.hold(f"{kind}:{payload.get('attempt_id')}")

    def accept_lane_report(self, lane: str, report: dict, *, retained: bool = False) -> None:
        """Hold on any lane halt, stopped ledger, unreconciled start, unsettled reservation, or failed row."""
        prefix = "retained_" if retained else ""
        halted = report.get("halted")
        if halted and halted.get("reason") not in {None, "global_admission_hold"} and not retained:
            self.hold(f"lane_halted:{lane}:{halted.get('reason')}")
        for attempt_id in report.get("unreconciled_starts") or []:
            self.hold(f"{prefix}unreconciled_start:{attempt_id}")
        ledger = report.get("ledger") or {}
        unstarted = any(row["status"] in {"unrun", "not_started_preflight_failed"}
                        for row in report.get("entries") or [])
        # A stopped lane ledger halts its lane at its next admission; at run start it holds every lane.
        # A finished lane may end exactly at its token target, which also sets the stop flag.
        if retained and unstarted and ledger.get("stop_generation"):
            self.hold(f"{prefix}ledger_stop:{lane}:{ledger.get('stop_reason')}")
        if ledger.get("unresolved_reservations") or ledger.get("active_reservations"):
            self.hold(f"{prefix}unsettled_lane_reservation:{lane}")
        for row in report.get("entries") or []:
            if row["status"] == "archived" and (
                    attempt_hold_kinds(row.get("check_passed"), row.get("failure_reasons"),
                                       row.get("usage_settlement"))
                    or type(row.get("usage_total_tokens")) is not int):
                self.hold(f"{prefix}failed_or_unknown_attempt:{row['attempt_id']}")
            elif row["status"] not in {"archived", "unrun", "not_started_preflight_failed"}:
                self.hold(f"{prefix}incomplete_attempt:{row['attempt_id']}")


class GlobalSlots:
    """Bound concurrent attempts across lanes; each slot covers preflight through archive.

    The coordinator's dispatcher takes a slot with ``try_acquire`` only when it
    starts an attempt, and the attempt releases it after its archive.
    """

    def __init__(self, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_GLOBAL_CONCURRENCY:
            raise ValueError(f"global concurrency must be an integer from 1 to {MAX_GLOBAL_CONCURRENCY}")
        self.limit = limit
        self.active = 0
        self.peak = 0
        self.admitted = 0
        self._waiters: list[asyncio.Future] = []

    @property
    def free(self) -> bool:
        return self.active < self.limit

    def try_acquire(self) -> bool:
        if not self.free:
            return False
        self.active += 1
        self.admitted += 1
        self.peak = max(self.peak, self.active)
        return True

    def release(self) -> None:
        if self.active < 1:
            raise RuntimeError("released a global slot that was not held")
        self.active -= 1
        while self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.done():
                waiter.set_result(None)
                break

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        while not self.try_acquire():
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
        try:
            yield
        finally:
            self.release()


def caps_hash(record: dict) -> str:
    return content_hash(record)
