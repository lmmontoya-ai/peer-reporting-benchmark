"""Lanes, frozen caps, execution authorization, and the global admission policy.

A lane is one model at one reasoning effort. Each lane runs its assignments one
at a time in its own sealed phase directory, with its own journal and budget
ledger. A global slot limits the attempts that run at once across all lanes to
``global_max_concurrency`` (at most six).

The admission policy is ported from an earlier unpublished multi-lane study. Any
failed execution check holds all new admission. So do an unsettled usage
record, the admission cutoff, a soft or hard stop file, the forced-stop
deadline, a halted lane, a stopped lane ledger, and retained incomplete or
failed evidence. Unknown final usage alone does not hold when the world closed,
the runtime shut down cleanly, and usage was observed during the attempt: the
attempt settles at the larger of its observed usage and its reservation
(``bounded_by_reservation``, spec section 10). Active attempts finish within
their own caps after a soft stop (``root/STOP``, ``--stop-file``). A hard stop
(``root/HARD_STOP``, ``--hard-stop-file``) or the forced-stop deadline truncates
and drains them; such a truncation is consumed and behaviorally ineligible
(``stop_truncation``) but is not an execution failure. A hold is never lifted
inside a run, and an archived failed attempt holds every later run of the same
plan unless an approved amendment accepts it.

A provider capacity refusal before any tool request (``provider_unavailable``,
spec 10, revision 3) is consumed and ineligible but is not an execution failure:
it pauses all new admission for 10 minutes (``ProviderPause``), and a third such
attempt within any 60 minutes holds all new admission. In a study, pauses and
their counts are recorded at study level, so they bind every root of the study.

This module performs no model call and imports no v1.1 content module.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
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
# Spec 10: a compatibility root has no study registry, so its authorization names the root's resolved path.
COMPATIBILITY_AUTHORIZATION_FIELDS = AUTHORIZATION_FIELDS | {"root_path"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
BOUNDED_USAGE = "bounded_by_reservation"
SETTLED_USAGE = ("settled", BOUNDED_USAGE)
# Spec 10 stops. A soft stop refuses new admission; a hard stop or the deadline also truncates.
SOFT_STOP = "soft_stop"
HARD_STOP = "hard_stop"
FORCED_STOP_DEADLINE = "forced_stop_deadline"
STOP_TRUNCATION_REASON = "hard_stop_or_forced_deadline"  # the attempt's collection stop reason
STOP_TRUNCATION = "stop_truncation"  # classification: consumed, ineligible, not an execution failure
# Spec 10 (revision 3): a provider capacity refusal before any tool request. Consumed, ineligible, settled at its
# reservation, not an execution failure; it pauses new admission, and the third within the window holds.
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_PAUSE = "provider_pause"  # refusal reason while a pause is active; the entry stays unstarted
PROVIDER_PAUSE_SECONDS = 600
PROVIDER_WINDOW_SECONDS = 3600
PROVIDER_UNAVAILABLE_LIMIT = 3
PAUSE_RECORD_FIELDS = frozenset({"attempt_id", "lane_id", "paused_at", "resume_at", "paused_at_utc", "resume_at_utc",
                                 "pause_seconds", "window_seconds", "window_count", "limit", "holds_admission"})


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
    """P1-format caps for one lane: serial, with a token target one reservation above its planned reservations.

    Spec 10 (W09 N-c): a bounded settlement charges at least a full reservation,
    so a lane whose trials hit limits and overshoot once must still admit its
    last planned row. The lane wall counts only time while a run is open.
    """
    if not _validated:
        validate_caps_record(record, require_frozen=False)
    validate_phase(phase)
    if type(planned_trials) is not int or planned_trials < 1:
        raise ValueError("a lane needs at least one planned trial")
    reservation = record["trial"]["reserved_tokens_per_trial"]
    caps = {**record["trial"], "collection_wall_seconds": record["lane_wall_seconds"][phase],
            "collection_observed_token_stop_target": reservation * (planned_trials + 1), "max_concurrency": 1}
    validate_caps(caps)
    validate_token_headroom(caps, planned_trials)
    return caps


def validate_token_headroom(caps: dict, planned_trials: int) -> None:
    """A lane's token target must exceed its planned reservations by at least one reservation."""
    if caps["collection_observed_token_stop_target"] < caps["reserved_tokens_per_trial"] * (planned_trials + 1):
        raise ValueError("a lane's token target must exceed its planned reservations by at least one reservation")


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


def validate_authorization(record: Any, plan: dict, *, root: Path | None = None) -> dict:
    """Require an explicit, sealed user authorization of exactly this sealed live plan.

    A compatibility authorization also names the root's resolved path
    (``root_path``); with ``root``, that path must be the root's, so a copied or
    moved compatibility root needs its own authorization. Returns the record
    with parsed ``admission_cutoff`` and ``forced_stop_deadline`` timestamps.
    Any mismatch refuses the phase before a runtime is created.
    """
    check_seal(record)
    compatibility = plan.get("phase") == "compatibility"
    fields = COMPATIBILITY_AUTHORIZATION_FIELDS if compatibility else AUTHORIZATION_FIELDS
    if set(record) != fields:
        raise ValueError(f"authorization must contain exactly {sorted(fields)}")
    if (record["kind"] != AUTHORIZATION_KIND or record["schema_version"] != AUTHORIZATION_VERSION
            or record["protocol_id"] != PROTOCOL_ID):
        raise ValueError("not a v1.1 execution authorization")
    if (record["phase"] != plan.get("phase") or record["live_plan_hash"] != plan.get("seal_hash")
            or record["caps_hash"] != plan.get("caps_hash")
            or record["maximum_live_calls"] != plan.get("maximum_live_calls")):
        raise ValueError("authorization names another phase, plan, caps record, or call count")
    if compatibility:
        if type(record["root_path"]) is not str or not Path(record["root_path"]).is_absolute():
            raise ValueError("a compatibility authorization names the root's resolved absolute path")
        if root is not None and record["root_path"] != str(Path(root).resolve()):
            raise ValueError("the authorization names another root path; a copied or moved compatibility root "
                             "needs its own authorization")
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


def attempt_hold_kinds(check_passed: Any, failure_reasons: Any, usage_settlement: Any,
                       classification: Any = None) -> list[str]:
    """Why an archived attempt holds all new admission; empty when it does not.

    A failed execution check holds, except a check whose only failure is unknown
    usage after a clean close that settled at the reservation bound, a
    ``stop_truncation`` (a hard stop or the deadline truncated an otherwise clean
    attempt), and a ``provider_unavailable`` attempt settled at its reservation
    (its pause and window limit are ``ProviderPause``'s). Any settlement other than
    ``settled`` or ``bounded_by_reservation`` holds, including for a stop truncation.
    """
    kinds = []
    bounded_only = usage_settlement == BOUNDED_USAGE and set(failure_reasons or []) <= {"usage_known"}
    provider_unavailable = classification == PROVIDER_UNAVAILABLE and usage_settlement == BOUNDED_USAGE
    if (check_passed is not True and not bounded_only and classification != STOP_TRUNCATION
            and not provider_unavailable):
        kinds.append("execution_check_failure")
    if usage_settlement not in SETTLED_USAGE:
        kinds.append("unknown_final_usage")
    return kinds


class AdmissionPolicy:
    """Holds are one-way. Any reason recorded here refuses every later admission.

    ``stop_files`` are soft stops: they refuse new admission and let active
    attempts finish within their caps. ``hard_stop_files`` and the forced-stop
    deadline also truncate active attempts (``force_stop_reason``).
    ``accepted_attempts`` are failed attempts that an approved amendment accepts:
    their retained evidence no longer holds later runs, and they stay consumed.
    """

    def __init__(self, *, admission_cutoff: float, forced_stop_deadline: float,
                 wall_clock: Callable[[], float] = time.time, stop_files: Iterable[Path] = (),
                 hard_stop_files: Iterable[Path] = (), accepted_attempts: Iterable[str] = (),
                 on_change: Callable[[], None] | None = None) -> None:
        if not admission_cutoff < forced_stop_deadline:
            raise ValueError("the admission cutoff must precede the forced-stop deadline")
        self.admission_cutoff, self.forced_stop_deadline = admission_cutoff, forced_stop_deadline
        self.wall_clock, self.on_change = wall_clock, on_change
        self.stop_files = [Path(path) for path in stop_files]
        self.hard_stop_files = [Path(path) for path in hard_stop_files]
        self.accepted_attempts = frozenset(accepted_attempts)
        self.holds: list[str] = []

    def hold(self, reason: str) -> None:
        if reason not in self.holds:
            self.holds.append(reason)
            if self.on_change is not None:
                self.on_change()

    def force_stop_reason(self) -> str | None:
        """``hard_stop`` or ``forced_stop_deadline`` when active attempts must be truncated now."""
        if any(path.exists() for path in self.hard_stop_files):
            return HARD_STOP
        if self.wall_clock() >= self.forced_stop_deadline:
            return FORCED_STOP_DEADLINE
        return None

    def force_stop_due(self) -> bool:
        return self.force_stop_reason() is not None

    def soft_stop_requested(self) -> bool:
        return any(path.exists() for path in self.stop_files)

    def admission_check(self) -> dict | None:
        """Return a hold record, or None when a new attempt may be admitted now."""
        forced = self.force_stop_reason()
        if forced is not None:
            self.hold(forced)
        elif self.soft_stop_requested():
            self.hold(SOFT_STOP)
        elif self.wall_clock() >= self.admission_cutoff:
            self.hold("admission_cutoff")
        return {"reason": "global_admission_hold", "holds": list(self.holds)} if self.holds else None

    def accept_archived(self, payload: dict) -> None:
        """Stop all new admission after a failed execution check or an unsettled usage record."""
        check = payload.get("check") or {}
        settlement = ((payload.get("orchestrator") or {}).get("usage_settlement") or {}).get("status")
        for kind in attempt_hold_kinds(check.get("passed"), check.get("failure_reasons"), settlement,
                                       check.get("classification")):
            self.hold(f"{kind}:{payload.get('attempt_id')}")

    def accept_lane_report(self, lane: str, report: dict, *, retained: bool = False) -> None:
        """Hold on any lane halt, stopped ledger, unreconciled start, unsettled reservation, or failed row.

        Rows that an approved amendment accepts, and their unresolved reservations,
        do not hold; they were consumed before this run and never run again.
        Unreconciled starts and active reservations always hold.
        """
        prefix = "retained_" if retained else ""
        accepted = self.accepted_attempts
        halted = report.get("halted")
        if halted and halted.get("reason") not in {None, "global_admission_hold", PROVIDER_PAUSE} and not retained:
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
        unresolved = [reservation for reservation in ledger.get("unresolved_reservations") or []
                      if reservation.split("~r", 1)[0] not in accepted]
        if unresolved or ledger.get("active_reservations"):
            self.hold(f"{prefix}unsettled_lane_reservation:{lane}")
        for row in report.get("entries") or []:
            if row["attempt_id"] in accepted:
                continue
            if row["status"] == "archived" and (
                    attempt_hold_kinds(row.get("check_passed"), row.get("failure_reasons"),
                                       row.get("usage_settlement"), row.get("classification"))
                    or type(row.get("usage_total_tokens")) is not int):
                self.hold(f"{prefix}failed_or_unknown_attempt:{row['attempt_id']}")
            elif row["status"] not in {"archived", "unrun", "not_started_preflight_failed"}:
                self.hold(f"{prefix}incomplete_attempt:{row['attempt_id']}")


def _utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def validate_pause_record(record: Any) -> dict:
    """A journaled provider pause: the attempt, its lane, its wall-clock pause, and its window count."""
    if type(record) is not dict or set(record) - {"recovered"} != PAUSE_RECORD_FIELDS:
        raise ValueError(f"a provider pause record must contain exactly {sorted(PAUSE_RECORD_FIELDS)}")
    for key in ("paused_at", "resume_at"):
        if isinstance(record[key], bool) or not isinstance(record[key], (int, float)) or not math.isfinite(record[key]):
            raise ValueError(f"provider pause {key} must be a finite wall-clock time")
    if (type(record["attempt_id"]) is not str or type(record["lane_id"]) is not str
            or abs(record["resume_at"] - record["paused_at"] - PROVIDER_PAUSE_SECONDS) > 1e-6
            or (record["pause_seconds"], record["window_seconds"], record["limit"])
            != (PROVIDER_PAUSE_SECONDS, PROVIDER_WINDOW_SECONDS, PROVIDER_UNAVAILABLE_LIMIT)
            or type(record["window_count"]) is not int or record["window_count"] < 1
            or record["holds_admission"] is not (record["window_count"] >= PROVIDER_UNAVAILABLE_LIMIT)):
        raise ValueError("a provider pause record differs from the revision 3 pause rule")
    return record


class ProviderPause:
    """Spec 10 (revision 3): provider capacity refusals pause new admission; the third within the window holds.

    Each ``provider_unavailable`` attempt pauses all new admission for
    ``PROVIDER_PAUSE_SECONDS`` of wall-clock time from its archive. Its window
    count is the number of such attempts, itself included, in the
    ``PROVIDER_WINDOW_SECONDS`` ending at its archive; at
    ``PROVIDER_UNAVAILABLE_LIMIT`` it holds all new admission. Records are
    journaled in the attempt's lane, so a resumed or restarted run restores them
    (``restore``): it respects an active pause, keeps counting in the window, and
    holds at start while the window still holds the limit (``limit_reached``).

    A root of a study passes ``load``, which returns every pause recorded at
    study level, and ``persist``, which records a new pause there durably before
    the run acts on it. ``refresh`` restores the study's pauses; ``record``
    refreshes first, so the window counts the refusals of every root of the study.
    """

    def __init__(self, *, wall_clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] | None = None,
                 load: Callable[[], Iterable[dict]] | None = None,
                 persist: Callable[[dict], Any] | None = None) -> None:
        self.wall_clock, self.on_change = wall_clock, on_change
        self.load, self.persist = load, persist
        self.events: list[dict] = []
        self.ended: set[str] = set()

    def restore(self, records: Iterable[dict], ended: Iterable[str] = ()) -> None:
        """Add pause records not yet known; two records of one attempt's pause must agree."""
        known = {event["attempt_id"]: event for event in self.events}
        for record in sorted(records, key=lambda item: (item["paused_at"], item["attempt_id"])):
            record = validate_pause_record(deepcopy(record))
            event = {key: record[key] for key in PAUSE_RECORD_FIELDS}
            if event["attempt_id"] in known:
                if known[event["attempt_id"]] != event:
                    raise ValueError(f"two records of the provider pause of {event['attempt_id']} differ")
                continue
            known[event["attempt_id"]] = event
            self.events.append(event)
        self.ended |= set(ended)

    def refresh(self) -> None:
        """Restore every pause recorded at study level, when this root belongs to a study."""
        if self.load is not None:
            self.restore(self.load())

    def window_count(self, at: float) -> int:
        return sum(at - PROVIDER_WINDOW_SECONDS < event["paused_at"] <= at for event in self.events)

    def record(self, attempt_id: str, lane_id: str) -> dict:
        """Pause admission for a provider_unavailable attempt; persist it, then return its journal record."""
        self.refresh()
        if any(event["attempt_id"] == attempt_id for event in self.events):
            raise ValueError(f"{attempt_id} already paused admission; an attempt is consumed once")
        now = float(self.wall_clock())
        count = self.window_count(now) + 1
        record = validate_pause_record({
            "attempt_id": attempt_id, "lane_id": lane_id, "paused_at": now,
            "resume_at": now + PROVIDER_PAUSE_SECONDS, "paused_at_utc": _utc(now),
            "resume_at_utc": _utc(now + PROVIDER_PAUSE_SECONDS), "pause_seconds": PROVIDER_PAUSE_SECONDS,
            "window_seconds": PROVIDER_WINDOW_SECONDS, "window_count": count, "limit": PROVIDER_UNAVAILABLE_LIMIT,
            "holds_admission": count >= PROVIDER_UNAVAILABLE_LIMIT})
        if self.persist is not None:
            self.persist(deepcopy(record))
        self.events.append(record)
        if self.on_change is not None:
            self.on_change()
        return deepcopy(record)

    def active(self) -> dict | None:
        """The pause in force now, as an admission refusal, or None."""
        now = self.wall_clock()
        latest = max(self.events, key=lambda event: event["resume_at"], default=None)
        if latest is None or now >= latest["resume_at"]:
            return None
        return {"reason": PROVIDER_PAUSE, "paused_by_attempt_id": latest["attempt_id"],
                "paused_by_lane_id": latest["lane_id"], "resume_at": latest["resume_at"],
                "resume_at_utc": latest["resume_at_utc"], "remaining_seconds": latest["resume_at"] - now}

    def expired_unended(self) -> list[dict]:
        """Pauses whose time is over but whose end is not journaled yet."""
        now = self.wall_clock()
        return [deepcopy(event) for event in self.events
                if event["attempt_id"] not in self.ended and now >= event["resume_at"]]

    def limit_reached(self) -> dict | None:
        """The window ending now still holds the limit: the latest refusal and the count, or None."""
        now = self.wall_clock()
        count = self.window_count(now)
        if count < PROVIDER_UNAVAILABLE_LIMIT:
            return None
        latest = max((event for event in self.events if event["paused_at"] <= now), key=lambda event: event["paused_at"])
        return {"attempt_id": latest["attempt_id"], "window_count": count}

    def summary(self) -> dict:
        return {"events": deepcopy(self.events), "ended": sorted(self.ended), "active": self.active(),
                "window_count_now": self.window_count(self.wall_clock())}


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
