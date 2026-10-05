"""Durable lane phases: one model at one effort, with its assignments run one at a time.

This is a port of the phase engine in ``peer_reporting.live``. That P1 module is
not edited, because P1 hashes its package files into sealed phase plans. P1's
generic pieces are imported unchanged: the hash-chained journal, the phase lock,
the budget ledger, the ledger-history check, and the archive summary.

Differences from P1:

- each entry carries its reasoning effort, world mode, and prompt condition, and
  the observer, preflight, evaluation, and archive receive them;
- the v1.1 bundle supplies the tools and the world audit;
- ``run_lanes`` opens every lane of a root and admits work through one global
  dispatcher: when a slot is free, it starts the lowest unstarted planned order
  whose lane is idle, subject to the round barrier (spec section 9);
- coordinator hooks refuse new admission (``admission_check``, consulted before
  preflight and again with no await before the durable start), set holds
  (``hold``), truncate active attempts on a hard stop or the deadline
  (``external_stop``), and see every archived attempt (``on_archived``);
- unknown final usage after a clean close, with usage observed during the
  attempt, settles at the larger of observed usage and the reservation
  (``bounded_by_reservation``); any other unknown usage holds admission. An
  approved amendment can accept failed attempts (``accepted_attempts``), whose
  unresolved reservations then stay charged without holding the lane;
- a lane's wall clock (its budget ledger clock) advances only while a run of
  that lane is open, summed across runs (``LaneClock``);
- right after ``attempt_started`` and before any session, the coordinator can
  claim the start in a study-level start ledger (``claim_start``). A refused
  claim consumes the attempt without a session and holds admission;
- a sealed cleanup reconciliation, journaled as ``cleanup_reconciled``, clears
  the cleanup debt of an attempt whose cleanup was never confirmed;
- a ``provider_unavailable`` attempt (a capacity refusal before any tool request,
  spec 10, revision 3) settles at its reservation, holds nothing, and pauses all
  new admission for 10 minutes; its pause is sealed into the attempt and
  journaled (``provider_pause_started``, ``provider_pause_ended``), so a resumed or
  restarted run respects an active pause and the 60-minute window count, and the
  third in that window holds all new admission. An entry refused during a pause
  stays unstarted and is dispatched again after it. A root of a study also
  records each pause at study level before sealing its attempt, and restores the
  study's pauses before any dispatch and again before each admission
  (``ProviderPause.load`` and ``persist``), so every root of the study respects them.

A lane seals its plan before any model call, reserves budget before the start,
writes ``attempt_started`` before the authenticated session, consumes each
attempt once, and never retries an outcome. After an observer returns, nothing
awaits until its archive and hold decision are complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..events import canonical_json, content_hash
from ..long_events import StreamingEventLog, iter_events
from ..peer_reporting.budget import BudgetLedger
from ..peer_reporting.live import (
    INDEX_FILE,
    JOURNAL_FILE,
    LEDGER_FILE,
    LOCK_FILE,
    PLAN_FILE,
    UNSTARTED,
    EvidenceError,
    LiveEnvironmentError,
    LivePhaseError,
    PreflightError,
    _close_quietly,
    _exclusive,
    _Journal,
    _json_value,
    _ledger_summary,
    _plain,
)
from ..peer_reporting.live_archive import attempt_summary, verify_archived_index
from ..peer_reporting.live_integrity import verify_ledger_history
from ..peer_reporting.storage import atomic_json, read_sealed, safe_child, seal
from . import live_runtime
from .bundle import ProtocolBundle
from .lanes import (
    BOUNDED_USAGE,
    PROVIDER_PAUSE,
    PROVIDER_UNAVAILABLE,
    STOP_TRUNCATION_REASON,
    GlobalSlots,
    ProviderPause,
    attempt_hold_kinds,
    validate_pause_record,
    validate_token_headroom,
)

LIVE_VERSION = "peer-reporting-v11-live-phases-v1"
PLAN_KIND = "peer_reporting_v11_lane_phase_plan"
INDEX_KIND = "peer_reporting_v11_lane_phase_index"
ATTEMPT_KIND = "peer_reporting_v11_live_attempt"
ENTRY_LABELS = ("reasoning_effort", "world_mode", "prompt_condition", "split", "arm", "template_id", "level",
                "variant", "near_miss_type", "planned_order", "round")
# The adapter's closing notification when the final total is unknown; every other notification needs a total.
FINAL_USAGE_SOURCE = "final"
# Why an attempt settled at its reservation bound (``settlement_reason`` in lane reports).
BOUNDED_AFTER_CLEAN_CLOSE = "observed_usage_after_clean_close"
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_SHARED_MODULES = ("runtime.py", "model_catalog.py", "isolation.py", "events.py", "long_events.py")


def implementation_hashes() -> dict[str, str]:
    """Code identity: the v1.1 package, the P1 package it reuses, and the shared runtime modules."""
    package = Path(__file__).parent
    root = package.parent
    paths = (sorted(path for path in package.rglob("*") if path.suffix in {".py", ".json"})
             + sorted((root / "peer_reporting").glob("*.py")) + [root / "peer_reporting" / "protocol.json"]
             + sorted((root / "peer_reporting" / "data").glob("*.json"))
             + [root / name for name in _SHARED_MODULES] + sorted((root / "data").glob("*.json")))
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file() and "__pycache__" not in path.parts}


@dataclass
class Hooks:
    """Injected transport and coordinator behavior. Live use passes the reviewed factory explicitly."""

    runtime_factory: Callable[[str, str], Any]
    preflight: Callable[..., Awaitable[dict]]
    environment_check: Callable[[], Awaitable[dict]]
    observer: Callable[..., Awaitable[dict]] = live_runtime.run_live_observer
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Any] = asyncio.sleep
    ledger_clock: Callable[[], float] = time.time
    poll_seconds: float = 1.0
    admission_check: Callable[[], dict | None] = field(default=lambda: None)
    hold: Callable[[str], None] = field(default=lambda reason: None)
    external_stop: asyncio.Event | None = None
    on_archived: Callable[[dict], None] = field(default=lambda payload: None)
    authorization_hash: str | None = None
    accepted_attempts: frozenset = frozenset()  # failed attempts accepted by an approved amendment
    # Spec 10: claim(lane_id, lane_plan_hash, entry, attempt_started record) before any session; raises to refuse.
    claim_start: Callable[[str, str, dict, dict], Any] | None = None
    # Spec 10 (revision 3): provider pauses use wall-clock time; the dispatcher waits out a pause in polls.
    wall_clock: Callable[[], float] = time.time
    pause_sleep: Callable[[float], Any] = asyncio.sleep
    provider_pause: ProviderPause | None = None  # run_lanes creates one on ``wall_clock`` when None

    def __post_init__(self) -> None:
        for name in ("runtime_factory", "preflight", "environment_check", "observer", "admission_check", "hold",
                     "on_archived", "wall_clock", "pause_sleep"):
            if not callable(getattr(self, name)):
                raise ValueError(f"hook {name} must be callable; live use requires an explicit runtime factory")
        if self.claim_start is not None and not callable(self.claim_start):
            raise ValueError("hook claim_start must be callable")
        poll = self.poll_seconds
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0 < poll <= 60:
            raise ValueError("poll_seconds must be in (0, 60]")


class _PhaseState:
    """Verified retained lane evidence. Callers mutate it only under the lane lock."""

    def __init__(self, directory: Path, *, bundle: ProtocolBundle | None,
                 ledger_clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.bundle = bundle
        try:
            self.plan = read_sealed(self.directory / PLAN_FILE)
            index = read_sealed(self.directory / INDEX_FILE)
        except (OSError, ValueError) as error:
            raise EvidenceError(f"lane plan or index missing or corrupt: {error}") from error
        if self.plan.get("kind") != PLAN_KIND or index.get("kind") != INDEX_KIND:
            raise EvidenceError("not a v1.1 lane phase plan and index")
        self.plan_hash = self.plan["seal_hash"]
        if index.get("plan_hash") != self.plan_hash:
            raise EvidenceError("lane index belongs to another plan")
        self.index = _plain(index)
        self.caps = self.plan["caps"]
        self.ledger_clock = ledger_clock
        self.journal = _Journal(self.directory / JOURNAL_FILE, self.plan_hash, create=False)
        try:
            checkpoint = self.index["journal"]
            if (self.journal.count < checkpoint["count"]
                    or self.journal.hash_at(checkpoint["count"]) != checkpoint["final_hash"]):
                raise EvidenceError("lane journal is truncated or differs from the retained index checkpoint")
            if set(self.index["entries"]) != {entry["entry_id"] for entry in self.plan["planned_order"]}:
                raise EvidenceError("lane index omits or adds a planned entry")
            self.ledger_recovered = False
            self.ledger = self._open_ledger()
            self.verify_budget_history()
        except BaseException:
            self.journal.close()
            raise

    @property
    def ledger_path(self) -> Path:
        return self.directory / LEDGER_FILE

    @property
    def call_starts(self) -> int:
        return len(self.journal.of_kind("attempt_started"))

    def _ledger_files(self) -> list[Path]:
        path = self.ledger_path
        return [candidate for candidate in (path, path.with_suffix(path.suffix + ".identity.json"))
                if candidate.exists()]

    def _open(self) -> BudgetLedger:
        return BudgetLedger(self.ledger_path, self.caps, plan_hash=self.plan_hash, clock=self.ledger_clock)

    def _open_ledger(self) -> BudgetLedger | None:
        created = self.journal.of_kind("ledger_created")
        intended = self.journal.of_kind("ledger_creating")
        files = self._ledger_files()
        if created:
            try:
                return self._open()
            except (OSError, ValueError) as error:
                raise EvidenceError("budget ledger or its identity record is missing or corrupt; "
                                    f"refusing to recreate it: {error}") from error
        if files and not intended:
            raise EvidenceError("a budget ledger exists without a journaled creation")
        if not files:
            return None
        try:
            ledger = self._open()
        except (OSError, ValueError) as error:
            raise EvidenceError(f"budget ledger creation was interrupted; explicit recovery is required: {error}") \
                from error
        self.ledger_recovered = True
        return ledger

    def ledger_state(self) -> dict | None:
        """Read the verified ledger without advancing its clock or stop flags."""
        if self.ledger is None:
            return None
        return read_sealed(self.ledger_path)

    def verify_budget_history(self) -> None:
        try:
            verify_ledger_history(self.ledger_state(), self.journal.records)
        except ValueError as error:
            raise EvidenceError(f"budget history differs from retained journal: {error}") from error

    def save_index(self) -> None:
        self.index["journal"] = {"count": self.journal.count, "final_hash": self.journal.last_hash}
        atomic_json(self.directory / INDEX_FILE, seal(self.index))

    def verify_nonarchived_attempts(self) -> list[str]:
        """Bind consumed but unarchived entries to their recorded start and recovery."""
        starts = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_started")}
        interruptions = self.journal.of_kind("attempt_interrupted_reconciled")
        unreconciled = []
        for entry in self.plan["planned_order"]:
            current = self.index["entries"][entry["entry_id"]]
            if any(current.get(key) != entry[key] for key in ("attempt_id", "model", "reasoning_effort",
                                                              "planned_index")):
                raise EvidenceError(f"{entry['entry_id']}: indexed identity differs from planned entry")
            status, attempt = current["status"], current["attempt"]
            start = starts.get(entry["attempt_id"])
            if status in UNSTARTED:
                if attempt is not None:
                    raise EvidenceError(f"{entry['entry_id']}: unstarted entry has an attempt record")
                if start is not None:
                    unreconciled.append(entry["attempt_id"])
                continue
            if status == "archived":
                continue
            if status not in {"started", "incomplete_interrupted"} or start is None:
                raise EvidenceError(f"{entry['entry_id']}: invalid nonarchived attempt state")
            data = start["data"]
            expected = {"attempt_id": data["attempt_id"], "reservation_id": data["reservation_id"],
                        "path": f"attempts/{data['attempt_id']}", "started_journal_seq": start["sequence"],
                        "status": status}
            if status == "started":
                unreconciled.append(entry["attempt_id"])
            elif not any(record["data"].get("attempt_id") == entry["attempt_id"]
                         and record["data"].get("entry_id") == entry["entry_id"]
                         and record["data"].get("reservation_id") == data["reservation_id"]
                         for record in interruptions):
                raise EvidenceError(f"{entry['entry_id']}: interrupted attempt lacks recorded reconciliation")
            if attempt != expected:
                raise EvidenceError(f"{entry['entry_id']}: nonarchived attempt differs from retained journal")
        return unreconciled

    def cleanup_debt(self) -> list[str]:
        """Every start needs a verified archived cleanup result, or a sealed cleanup reconciliation, before another
        start. An amendment never clears cleanup debt (spec 10)."""
        confirmed = {record["data"]["attempt_id"] for record in self.journal.of_kind("attempt_archived")
                     if record["data"]["summary"].get("cleanup_confirmed") is True}
        confirmed |= {record["data"]["attempt_id"] for record in self.journal.of_kind("cleanup_reconciled")}
        return [record["data"]["attempt_id"] for record in self.journal.of_kind("attempt_started")
                if record["data"]["attempt_id"] not in confirmed]

    def verify_attempts(self) -> list[str]:
        """Check every archived attempt against its retained checkpoints; return unreconciled starts."""
        self.verify_budget_history()
        started = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_started")}
        archived = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_archived")}
        unreconciled = self.verify_nonarchived_attempts()
        sealed_pauses: dict[str, dict] = {}
        for entry in self.plan["planned_order"]:
            state = self.index["entries"][entry["entry_id"]]
            attempt = state["attempt"]
            if state["status"] != "archived":
                continue
            if (attempt is None or attempt["attempt_id"] != entry["attempt_id"]
                    or entry["attempt_id"] not in started):
                raise EvidenceError(f"{entry['entry_id']}: attempt record has no journaled start")
            if entry["attempt_id"] not in archived:
                raise EvidenceError(f"{entry['attempt_id']}: archived without a journal record")
            attempt_dir = safe_child(self.directory, attempt["path"])
            try:
                payload = _plain(read_sealed(attempt_dir / "attempt.json"))
            except (OSError, ValueError) as error:
                raise EvidenceError(f"{entry['attempt_id']}: attempt archive missing or corrupt: {error}") from error
            if (content_hash(payload) != attempt["attempt_hash"]
                    or archived[entry["attempt_id"]]["data"]["summary"]["attempt_hash"] != attempt["attempt_hash"]
                    or payload["attempt_id"] != entry["attempt_id"] or payload["plan_hash"] != self.plan_hash
                    or any(payload.get(key) != entry[key] for key in ("model", "reasoning_effort", "world_mode",
                                                                      "prompt_condition"))):
                raise EvidenceError(f"{entry['attempt_id']}: attempt archive differs from its checkpoint")
            try:
                verify_archived_index(payload, attempt, started[entry["attempt_id"]],
                                      archived[entry["attempt_id"]], self.journal.records)
            except ValueError as error:
                raise EvidenceError(f"{entry['attempt_id']}: {error}") from error
            pause = payload["orchestrator"].get("provider_pause")
            if (pause is not None) != (attempt.get("classification") == PROVIDER_UNAVAILABLE):
                raise EvidenceError(f"{entry['attempt_id']}: a provider pause is sealed exactly for a "
                                    "provider_unavailable attempt")
            if pause is not None:
                try:
                    validate_pause_record(pause)
                except ValueError as error:
                    raise EvidenceError(f"{entry['attempt_id']}: {error}") from error
                if pause["attempt_id"] != entry["attempt_id"] or pause["lane_id"] != self.plan["lane_id"]:
                    raise EvidenceError(f"{entry['attempt_id']}: the sealed provider pause names another attempt")
                sealed_pauses[entry["attempt_id"]] = pause
            try:
                if attempt.get("world_checkpoint") is not None:
                    self.bundle.audit_state(attempt_dir / "world", attempt["world_checkpoint"])
                controller = attempt.get("controller_checkpoint")
                if isinstance(controller, dict) and "event_count" in controller:
                    for _ in iter_events(attempt_dir / "runtime-log" / "events.jsonl",
                                         expected_count=controller["event_count"],
                                         expected_hash=controller.get("final_hash")):
                        pass
                sink = attempt["sink_checkpoint"]
                for _ in iter_events(attempt_dir / "orchestrator-events" / "events.jsonl",
                                     expected_count=sink["count"], expected_hash=sink["final_hash"]):
                    pass
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise EvidenceError(f"{entry['attempt_id']}: retained attempt evidence failed audit: {error}") \
                    from error
        self.verify_provider_pauses(sealed_pauses)
        return unreconciled

    def verify_provider_pauses(self, sealed: dict[str, dict]) -> None:
        """Journaled pauses repeat the pauses sealed in their attempts; a crash may leave one unjournaled.

        A crash after the pause record but before the index update leaves an
        archive the index does not show yet; its pause is read from the attempt.
        """
        archived = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_archived")}
        journaled = {}
        for record in self.journal.of_kind("provider_pause_started"):
            data = {key: value for key, value in record["data"].items() if key != "recovered"}
            expected = sealed.get(data.get("attempt_id"))
            archive = archived.get(data.get("attempt_id"))
            if (expected is None and archive is not None
                    and archive["data"]["summary"].get("classification") == PROVIDER_UNAVAILABLE):
                try:
                    payload = read_sealed(safe_child(self.directory, f"attempts/{data['attempt_id']}") / "attempt.json")
                except (OSError, ValueError) as error:
                    raise EvidenceError(f"{data['attempt_id']}: attempt archive missing or corrupt: {error}") from error
                if content_hash(_plain(payload)) == archive["data"]["summary"]["attempt_hash"]:
                    expected = (payload.get("orchestrator") or {}).get("provider_pause")
            if data.get("attempt_id") in journaled or expected != data:
                raise EvidenceError(f"journaled provider pause of {data.get('attempt_id')!r} differs from the pause "
                                    "sealed in its attempt")
            journaled[data["attempt_id"]] = data
        for record in self.journal.of_kind("provider_pause_ended"):
            if record["data"].get("attempt_id") not in journaled:
                raise EvidenceError("a journaled provider pause end names no journaled pause")

    def provider_pauses(self) -> tuple[list[dict], set[str]]:
        """Journaled pause records and the attempts whose pause end is journaled."""
        return ([record["data"] for record in self.journal.of_kind("provider_pause_started")],
                {record["data"]["attempt_id"] for record in self.journal.of_kind("provider_pause_ended")})


def validate_lane_plan(plan: dict) -> None:
    """Structural checks that a lane can run safely: one model, one effort, unique serial entries."""
    if plan.get("kind") != PLAN_KIND or plan.get("live_version") != LIVE_VERSION:
        raise ValueError("not a v1.1 lane phase plan")
    entries = plan["planned_order"]
    if not entries or plan["maximum_live_calls"] != len(entries):
        raise ValueError("a lane plan needs entries and one primary call per entry")
    if plan["caps"]["max_concurrency"] != 1:
        raise ValueError("a lane runs one attempt at a time; global concurrency is a coordinator slot")
    if len({entry["entry_id"] for entry in entries}) != len(entries) or len(
            {entry["attempt_id"] for entry in entries}) != len(entries):
        raise ValueError("lane entries and attempts must be unique")
    validate_token_headroom(plan["caps"], len(entries))
    previous_round = 0
    for position, entry in enumerate(entries):
        if (entry["model"] != plan["model"] or entry["reasoning_effort"] != plan["reasoning_effort"]
                or entry["planned_index"] != position or entry["attempt_id"] != f"{entry['entry_id']}-live-1"
                or not _ATTEMPT_ID.fullmatch(entry["attempt_id"])):
            raise ValueError("lane entry differs from its lane identity or order")
        if type(entry.get("round")) is not int or not previous_round <= entry["round"] <= 1000:
            raise ValueError("lane entry rounds must be nonnegative integers that never decrease in planned order")
        previous_round = entry["round"]
        live_runtime.validate_world_mode(entry["world_mode"])


def create_lane_phase(directory: Path, plan: dict) -> str:
    """Seal one lane plan, its journal, and its index in a new directory; return the plan hash."""
    validate_lane_plan(plan)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    sealed = seal(plan)
    atomic_json(directory / PLAN_FILE, sealed)
    journal = _Journal(directory / JOURNAL_FILE, sealed["seal_hash"], create=True)
    try:
        journal.append("phase_sealed", phase=plan["phase"], plan_hash=sealed["seal_hash"], lane_id=plan["lane_id"],
                       planned_order=[entry["attempt_id"] for entry in plan["planned_order"]],
                       maximum_live_calls=plan["maximum_live_calls"])
        index = {
            "kind": INDEX_KIND, "plan_hash": sealed["seal_hash"], "phase": plan["phase"], "lane_id": plan["lane_id"],
            "entries": {entry["entry_id"]: {
                "status": "unrun", "model": entry["model"], "reasoning_effort": entry["reasoning_effort"],
                "attempt_id": entry["attempt_id"], "planned_index": entry["planned_index"],
                "preflight_failures": [], "attempt": None,
            } for entry in plan["planned_order"]},
            "last_run": None,
            "journal": {"count": journal.count, "final_hash": journal.last_hash},
        }
        atomic_json(directory / INDEX_FILE, seal(index))
    finally:
        journal.close()
    return sealed["seal_hash"]


def _known_total(result: Any, usage_failures: list[str]) -> int | None:
    total = ((result or {}).get("usage") or {}).get("total_tokens")
    return total if type(total) is int and total >= 0 and not usage_failures else None


def clean_shutdown(result: Any, observer_error: str | None, failures: list[str]) -> bool:
    """Spec 10: the world closed and the runtime shut down cleanly, with no failure recorded."""
    return (isinstance(result, dict) and observer_error is None and not failures
            and result.get("runtime_closed") is True and result.get("queue_reconciled") is True
            and result.get("world_checkpoint") is not None and not result.get("infrastructure_failures")
            and result.get("termination_kind") != "infrastructure_incomplete")


def usage_unobserved(result: Any, notices: dict) -> str | None:
    """Spec 10 (W09 R2-M1): why usage was not observed during an attempt, or None when it was.

    Usage was observed when the adapter reports an integer observed total, the
    orchestrator persisted at least one notification with a total, and no
    notification before the adapter's closing one lacked a total. Otherwise the
    per-trial token stop may have been blind, so the reservation is no bound.
    """
    if notices["without_total"]:
        return "usage_notification_without_total"
    reported = (result.get("usage") or {}).get("observed_total_tokens") if isinstance(result, dict) else None
    if type(reported) is not int or reported < 0 or not notices["with_total"]:
        return "usage_never_observed"
    return None


def provider_unavailable_close(result: Any, observer_error: str | None, failures: list[str], notices: dict) -> bool:
    """Spec 10 (revision 3): the adapter classified a capacity refusal before any tool request, and the close was clean.

    This is the one exception to the observed-usage requirement: the attempt
    settles at its reservation even though no usage was observed. A usage
    notification without a total still leaves usage unresolved.
    """
    return (clean_shutdown(result, observer_error, failures)
            and result.get("termination_kind") == live_runtime.PROVIDER_UNAVAILABLE
            and type(result.get("provider_overload")) is dict and not result.get("tool_requests")
            and not result.get("observer_outputs") and result.get("exposure_confirmed") is True
            and not notices["without_total"])


def started_record(start: dict) -> dict:
    data = start["data"]
    return {"attempt_id": data["attempt_id"], "reservation_id": data["reservation_id"],
            "path": f"attempts/{data['attempt_id']}", "started_journal_seq": start["sequence"],
            "status": "started"}


def reconcile_lane(state: _PhaseState) -> None:
    """Make a lane's index agree with its journal after a crash; never reopen a started attempt.

    The caller holds the lane lock. A started attempt without an archive becomes
    ``incomplete_interrupted`` and its active reservation unresolved; an admitted
    reservation without a start record is released, since no session start was possible.
    """
    journal, plan, index, ledger = state.journal, state.plan, state.index, state.ledger
    state.verify_budget_history()
    state.verify_nonarchived_attempts()
    if state.ledger_recovered:
        journal.append("ledger_created", recovered=True, ledger_id=state.ledger_state()["ledger_id"])
    starts = {record["data"]["attempt_id"]: record for record in journal.of_kind("attempt_started")}
    archived = {record["data"]["attempt_id"]: record for record in journal.of_kind("attempt_archived")}
    changed = False
    for entry in plan["planned_order"]:
        current = index["entries"][entry["entry_id"]]
        attempt_id = entry["attempt_id"]
        start = starts.get(attempt_id)
        if start is None:
            if current["status"] not in UNSTARTED:
                raise EvidenceError(f"{attempt_id}: index records an attempt without a journaled start")
            continue
        if attempt_id in archived and current["status"] != "archived":
            summary = archived[attempt_id]["data"]["summary"]
            current["status"] = "archived"
            current["attempt"] = {**started_record(start), **summary, "status": "archived"}
            changed = True
        elif current["status"] in UNSTARTED or current["status"] == "started":
            current["status"] = "incomplete_interrupted"
            current["attempt"] = {**(current["attempt"] or started_record(start)), "status": "incomplete_interrupted"}
            reservation = start["data"]["reservation_id"]
            ledger_status = None
            if ledger is not None:
                attempt = state.ledger_state()["attempts"].get(reservation)
                if attempt is not None and attempt["status"] == "active":
                    ledger.settle(reservation, None)
                ledger_status = (state.ledger_state()["attempts"].get(reservation) or {}).get("status")
            journal.append("attempt_interrupted_reconciled", entry_id=entry["entry_id"], attempt_id=attempt_id,
                           reservation_id=reservation, ledger_status=ledger_status,
                           note="started attempt without an archive; consumed, never rerun")
            changed = True
    if ledger is not None:
        admitted = {record["data"]["reservation_id"] for record in journal.of_kind("reservation_admitted")}
        started = {record["data"]["reservation_id"] for record in starts.values()}
        planned = {entry["attempt_id"] for entry in plan["planned_order"]}
        for reservation, attempt in state.ledger_state()["attempts"].items():
            if reservation.split("~r", 1)[0] not in planned:
                raise EvidenceError(f"budget reservation {reservation!r} does not belong to this lane")
            if reservation not in started and attempt["status"] == "active":
                # Admitted, but the start record was never written: no session start was possible.
                ledger.settle(reservation, 0)
                journal.append("orphan_reservation_released", reservation_id=reservation,
                               journaled_admission=reservation in admitted)
                changed = True
    # A crash between an archive and its pause record: the pause sealed in the attempt is journaled now.
    journaled_pauses = {record["data"]["attempt_id"] for record in journal.of_kind("provider_pause_started")}
    for attempt_id, record in archived.items():
        if record["data"]["summary"].get("classification") != PROVIDER_UNAVAILABLE or attempt_id in journaled_pauses:
            continue
        payload = read_sealed(safe_child(state.directory, f"attempts/{attempt_id}") / "attempt.json")
        pause = (payload.get("orchestrator") or {}).get("provider_pause")
        if pause is None:
            raise EvidenceError(f"{attempt_id}: a provider_unavailable attempt has no sealed provider pause")
        journal.append("provider_pause_started", **pause, recovered=True)
        changed = True
    if changed or state.ledger_recovered:
        state.save_index()


class LaneClock:
    """Spec 10: a lane's wall clock advances only while a run of that lane is open, summed across runs.

    It drives the lane's budget ledger, whose wall limit is measured from its
    first reservation. ``base`` is the lane time already spent: the latest lane
    time journaled by an earlier run or recorded in the ledger.
    """

    def __init__(self, real: Callable[[], float], base: float) -> None:
        self.real, self.base = real, float(base)
        self.opened_at = real()

    def __call__(self) -> float:
        return self.base + (self.real() - self.opened_at)


def lane_clock_base(state: _PhaseState) -> float:
    values = [record["data"]["lane_clock_seconds"] for record in state.journal.records
              if type(record["data"].get("lane_clock_seconds")) in (int, float)]
    ledger = state.ledger_state()
    if ledger is not None:
        values += [ledger["started_at"], *(attempt["admitted_at"] for attempt in ledger["attempts"].values())]
    return float(max(values, default=0.0))


class _PhaseRun:
    def __init__(self, state: _PhaseState, hooks: Hooks, *, bundle: ProtocolBundle,
                 inputs: Callable[[dict], tuple[dict, str]], evaluate: Callable[..., dict],
                 binary_check: Callable[[str, dict], None] | None, pauses: ProviderPause | None = None) -> None:
        self.state, self.hooks, self.bundle = state, hooks, bundle
        self.pauses = pauses or hooks.provider_pause or ProviderPause(wall_clock=hooks.wall_clock)
        self.plan, self.index, self.journal = state.plan, state.index, state.journal
        self.directory = state.directory
        self.lane_id = self.plan["lane_id"]
        self.caps = deepcopy(state.caps)
        self.inputs, self.evaluate, self.binary_check = inputs, evaluate, binary_check
        self.halted: dict | None = None

    @property
    def ledger(self) -> BudgetLedger | None:
        return self.state.ledger

    def _save(self) -> None:
        self.state.save_index()

    def reconcile(self) -> None:
        """Make the index agree with the journal after a crash; never reopen a started attempt."""
        reconcile_lane(self.state)

    @staticmethod
    def _started_record(start: dict) -> dict:
        return started_record(start)

    def _ensure_ledger(self) -> BudgetLedger:
        if self.ledger is None:
            # The lane wall clock starts at the first reservation, not at sealing, and runs only while open.
            self.journal.append("ledger_creating", path=LEDGER_FILE)
            self.state.ledger = BudgetLedger(self.state.ledger_path, self.caps, plan_hash=self.state.plan_hash,
                                             create=True, clock=self.state.ledger_clock)
            ledger_state = self.state.ledger_state()
            self.journal.append("ledger_created", recovered=False, ledger_id=ledger_state["ledger_id"],
                                started_at=ledger_state["started_at"], caps_hash=content_hash(self.caps))
        return self.ledger

    def _reservation_id(self, attempt_id: str) -> str:
        taken = set(self.state.ledger_state()["attempts"])
        number = 1
        while f"{attempt_id}~r{number}" in taken:
            number += 1
        return f"{attempt_id}~r{number}"

    def _accepted(self, reservation: str) -> bool:
        return reservation.split("~r", 1)[0] in self.hooks.accepted_attempts

    def _retained_unresolved(self) -> frozenset[str]:
        """Unresolved reservations of amended attempts: they stay charged but do not block admission."""
        attempts = self.state.ledger_state()["attempts"]
        return frozenset(key for key, value in attempts.items() if value["status"] == "unresolved"
                         and self._accepted(key))

    def _ledger_hold(self, *, refresh: bool) -> dict | None:
        self.state.verify_budget_history()
        if self.ledger is None:
            return None
        state = self.ledger.snapshot() if refresh else self.state.ledger_state()
        summary = _ledger_summary(state)
        if state["stop_generation"]:
            return {"reason": state["stop_reason"] or "ledger_stop", "ledger": summary}
        if any(not self._accepted(reservation) for reservation in summary["unresolved_reservations"]):
            return {"reason": "unknown_usage_hold", "ledger": summary}
        if summary["active_reservations"]:
            return {"reason": "active_reservation_unreconciled", "ledger": summary}
        cleanup = self.state.cleanup_debt()
        if cleanup:
            return {"reason": "cleanup_unreconciled", "attempt_id": cleanup[0]}
        if (summary["settled_tokens"] + summary["reserved_tokens"] + self.caps["reserved_tokens_per_trial"]
                > self.caps["collection_observed_token_stop_target"]):
            return {"reason": "collection_token_capacity_exhausted", "ledger": summary}
        return None

    async def _environment(self) -> dict | None:
        try:
            result = await self.hooks.environment_check()
            if type(result) is not dict or result.get("verified") is not True:
                raise LiveEnvironmentError("environment check did not return verified=True")
            result, exact = _json_value(result)
            if not exact:
                raise LiveEnvironmentError("environment evidence is not JSON")
        except Exception as error:
            self.journal.append("environment_check_failed", reason=f"{type(error).__name__}: {error}"[:2000])
            return None
        self.journal.append("environment_verified", environment=result)
        return result

    def pending(self) -> list[dict]:
        return [entry for entry in self.plan["planned_order"]
                if self.index["entries"][entry["entry_id"]]["status"] in UNSTARTED]

    def open(self) -> None:
        self.journal.append("run_opened", resumed=True, live_version=LIVE_VERSION,
                            call_starts=self.state.call_starts, authorization_hash=self.hooks.authorization_hash,
                            lane_clock_seconds=self.state.ledger_clock())

    async def environment_verified(self) -> bool:
        return await self._environment() is not None

    def admission_hold(self) -> dict | None:
        """Refusal before preflight: the call maximum, the global hold, then this lane's ledger."""
        if self.state.call_starts >= self.plan["maximum_live_calls"]:
            return {"reason": "maximum_live_calls_reached"}
        return self.hooks.admission_check() or self._ledger_hold(refresh=True)

    def halt(self, entry: dict, hold: dict) -> None:
        """Record why this lane admits nothing more in this run."""
        self.halted = hold
        self.journal.append("admission_held", entry_id=entry["entry_id"], **hold)
        self._save()

    def close(self) -> None:
        self.index["last_run"] = {"halted": self.halted, "resumed": True}
        self.journal.append("run_closed", halted=self.halted, call_starts=self.state.call_starts,
                            lane_clock_seconds=self.state.ledger_clock())
        self._save()

    async def run_entry(self, entry: dict, *, dispatch_seq: int) -> dict | None:
        state = self.index["entries"][entry["entry_id"]]
        model, effort, attempt_id = entry["model"], entry["reasoning_effort"], entry["attempt_id"]
        attempt_dir = self.directory / "attempts" / attempt_id
        if attempt_dir.exists():
            raise EvidenceError(f"{attempt_id}: attempt directory exists without a start record; refusing to reuse it")
        fixture, instructions = self.inputs(entry)
        runtime = None
        try:
            runtime = self.hooks.runtime_factory(model, effort)
            preflight = await self.hooks.preflight(runtime, model, deepcopy(self.caps), reasoning_effort=effort,
                                                   bundle=self.bundle)
            if type(preflight) is not dict:
                raise PreflightError("preflight must return its evidence record")
            if self.binary_check is not None:
                self.binary_check(model, preflight)
            live_runtime.validate_preflight(model, self.caps, preflight, runtime, reasoning_effort=effort,
                                            bundle=self.bundle)
            preflight, exact = _json_value(preflight)
            if not exact:
                raise PreflightError("preflight evidence is not JSON")
        except Exception as error:
            if runtime is not None:
                await _close_quietly(runtime)
            reason = f"{type(error).__name__}: {error}"[:2000]
            record = self.journal.append("preflight_failed", entry_id=entry["entry_id"], attempt_id=attempt_id,
                                         model=model, reasoning_effort=effort, reason=reason, attempt_consumed=False)
            state["status"] = "not_started_preflight_failed"
            state["preflight_failures"].append({"journal_seq": record["sequence"], "reason": reason})
            self._save()
            if self.plan["continue_after_preflight_failure"]:
                return None
            return {"reason": "preflight_failed", "entry_id": entry["entry_id"]}
        handed_off = False
        try:
            self.journal.append("preflight_passed", entry_id=entry["entry_id"], attempt_id=attempt_id, model=model,
                                reasoning_effort=effort, preflight=preflight)
            # No await separates this check from the durable start record below.
            hold = self.hooks.admission_check()
            if hold is not None:
                self.journal.append("admission_held", entry_id=entry["entry_id"], after_preflight=True, **hold)
                self._save()
                return hold
            # Spec 10 (revision 3): a pause that began during this preflight, in this root or another root of
            # the study, refuses the start; the entry stays unstarted, nothing is consumed, and the dispatcher
            # offers it again after the pause.
            self.pauses.refresh()
            pause = self.pauses.active()
            if pause is not None:
                self.journal.append("admission_paused", entry_id=entry["entry_id"], after_preflight=True, **pause)
                self._save()
                return pause
            self.state.verify_budget_history()
            ledger = self._ensure_ledger()
            reservation = self._reservation_id(attempt_id)
            if not ledger.admit(reservation, retained_unresolved=self._retained_unresolved()):
                hold = self._ledger_hold(refresh=False) or {"reason": "admission_refused"}
                self.journal.append("admission_refused", entry_id=entry["entry_id"], reservation_id=reservation,
                                    **hold)
                self._save()
                return hold
            self.journal.append("reservation_admitted", entry_id=entry["entry_id"], attempt_id=attempt_id,
                                reservation_id=reservation)
            try:
                if self.state.call_starts >= self.plan["maximum_live_calls"]:
                    raise LivePhaseError("maximum live call starts reached")
                started = self.journal.append(
                    "attempt_started", entry_id=entry["entry_id"], attempt_id=attempt_id, model=model,
                    reasoning_effort=effort, world_mode=entry["world_mode"], reservation_id=reservation,
                    call_start_number=self.state.call_starts + 1, dispatch_seq=dispatch_seq,
                    planned_order=entry["planned_order"], round=entry.get("round"),
                    authorization_hash=self.hooks.authorization_hash,
                    lane_clock_seconds=self.state.ledger_clock(),
                    after_preflight_repair=bool(state["preflight_failures"]))
            except Exception:
                ledger.settle(reservation, 0)  # no start record, so no session start was attempted
                raise
            state["status"] = "started"
            state["attempt"] = self._started_record(started)
            self._save()
            if self.hooks.claim_start is not None:
                try:
                    self.hooks.claim_start(self.lane_id, self.state.plan_hash, entry, started)
                except Exception as error:
                    return self._refuse_start(entry, state, reservation, error)
            attempt_dir.mkdir(parents=True, exist_ok=False)
            handed_off = True
            await self._execute(entry, state, runtime, preflight, fixture, instructions, reservation, attempt_dir)
        finally:
            if not handed_off:
                await _close_quietly(runtime)
        if state["attempt"].get("cleanup_confirmed") is not True:
            return self._ledger_hold(refresh=False) or {"reason": "cleanup_unreconciled", "attempt_id": attempt_id}
        return None

    def _refuse_start(self, entry: dict, state: dict, reservation: str, error: Exception) -> dict:
        """The study start ledger refused the claim: the attempt is consumed, no session started, admission holds."""
        attempt_id = entry["attempt_id"]
        self.ledger.settle(reservation, 0)  # the claim precedes the session, so nothing was spent
        ledger_status = (self.state.ledger_state()["attempts"].get(reservation) or {}).get("status")
        self.journal.append("start_claim_refused", entry_id=entry["entry_id"], attempt_id=attempt_id,
                            reservation_id=reservation, reason=f"{type(error).__name__}: {error}"[:2000])
        self.journal.append("attempt_interrupted_reconciled", entry_id=entry["entry_id"], attempt_id=attempt_id,
                            reservation_id=reservation, ledger_status=ledger_status,
                            note="the study start ledger refused the start before any session; consumed, never run")
        state["status"] = "incomplete_interrupted"
        state["attempt"] = {**state["attempt"], "status": "incomplete_interrupted"}
        self._save()
        return {"reason": "start_claim_refused", "attempt_id": attempt_id}

    async def _execute(self, entry: dict, state: dict, runtime: Any, preflight: dict, fixture: dict,
                       instructions: str, reservation: str, attempt_dir: Path) -> None:
        attempt_id, model = entry["attempt_id"], entry["model"]
        ledger = self.ledger
        stop = asyncio.Event()
        stop_reasons: list[str] = []
        failures: list[str] = []
        usage_failures: list[str] = []
        usage_notices: dict[str, Any] = {"with_total": 0, "without_total": []}
        # The adapter fsyncs each event before calling the sink. This bound mirror flushes every
        # event and fsyncs periodically and at close, so it does not double trial-window latency.
        sink_log = StreamingEventLog(attempt_dir / "orchestrator-events", attempt_id, fsync_every=32)

        def request_stop(reason: str) -> None:
            if reason not in stop_reasons:
                stop_reasons.append(reason)
                try:
                    self.journal.append("collection_stop_requested", attempt_id=attempt_id, reason=reason)
                except Exception as error:
                    failures.append(f"journal: {type(error).__name__}: {error}")
            stop.set()

        def check_ledger() -> None:
            if self.hooks.external_stop is not None and self.hooks.external_stop.is_set():
                request_stop(STOP_TRUNCATION_REASON)  # a hard stop or the forced-stop deadline
            try:
                snapshot = ledger.snapshot()
            except Exception as error:
                failures.append(f"ledger unavailable: {type(error).__name__}: {error}")
                request_stop("ledger_unavailable")
                return
            if snapshot["stop_generation"]:
                request_stop(snapshot["stop_reason"] or "ledger_stop")

        def event_sink(event: dict) -> None:
            try:
                sink_log.emit("observer_event", event=event)
            except Exception as error:
                failures.append(f"event sink: {type(error).__name__}: {error}")
                request_stop("evidence_sink_failed")

        async def usage_callback(observation: dict) -> None:
            try:
                if (type(observation) is not dict or observation.get("attempt_id") != attempt_id
                        or type(observation.get("notification_id")) is not str):
                    raise ValueError("usage observation is not attributed to this attempt")
                cumulative = observation.get("cumulative_tokens")
                ledger.observe(reservation, observation["notification_id"], cumulative)
                self.journal.append("usage_observed", attempt_id=attempt_id, reservation_id=reservation,
                                    notification_id=observation["notification_id"], cumulative_tokens=cumulative,
                                    source=observation.get("source"))
                if cumulative is not None:
                    usage_notices["with_total"] += 1
                elif observation.get("source") != FINAL_USAGE_SOURCE:
                    usage_notices["without_total"].append(observation["notification_id"])
            except Exception as error:
                usage_failures.append(f"{type(error).__name__}: {error}")
                failures.append(f"usage persistence: {type(error).__name__}: {error}")
                try:  # unknown is never zero: hold admission until reconciled
                    ledger.observe(reservation, "orchestrator-usage-failure-" + content_hash(
                        [attempt_id, len(usage_failures)])[:32], None)
                except Exception:
                    pass
                request_stop("usage_persistence_failed")
                return
            check_ledger()

        async def monitor() -> None:
            while not stop.is_set():
                check_ledger()
                if stop.is_set():
                    return
                await self.hooks.sleep(self.hooks.poll_seconds)

        result: Any = None
        observer_error = None
        check_ledger()  # a stop reached during preflight must apply before session start
        watcher = asyncio.create_task(monitor())
        try:
            try:
                result = await self.hooks.observer(
                    fixture, instructions, directory=attempt_dir, attempt_id=attempt_id, requested_model=model,
                    reasoning_effort=entry["reasoning_effort"], world_mode=entry["world_mode"], bundle=self.bundle,
                    caps=deepcopy(self.caps), qualification=deepcopy(preflight), runtime=runtime,
                    collection_stop=stop, clock=self.hooks.clock, sleep=self.hooks.sleep,
                    event_sink=event_sink, usage_callback=usage_callback)
            except Exception as error:
                observer_error = f"{type(error).__name__}: {error}"[:2000]
                failures.append(f"observer raised: {observer_error}")
            # No await from here until the archive and its hold decision: another lane's
            # post-preflight admission check cannot run in between (spec 10, provisional hold).
            watcher.cancel()
            self._archive(entry, state, preflight, fixture, instructions, reservation, attempt_dir, result=result,
                          observer_error=observer_error, failures=failures, usage_failures=usage_failures,
                          usage_notices=usage_notices, stop_reasons=stop_reasons, sink_log=sink_log)
        except BaseException as error:
            self.hooks.hold(f"attempt_unarchived:{attempt_id}:{type(error).__name__}")
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _settle(self, reservation: str, total: int | None, result: Any, observer_error: str | None,
                failures: list[str], unobserved: str | None, *, provider_unavailable: bool = False) -> dict:
        """Settle the reservation: known usage, the reservation bound, or unresolved.

        The reservation bound applies only after a clean close with usage
        observed during the attempt (spec 10, W09 R2-M1), or to a
        ``provider_unavailable`` attempt, which settles at its reservation
        whether or not usage was observed (spec 10, revision 3; labeled
        ``settlement_reason: "provider_unavailable"``).
        """
        ledger = self.ledger
        if total is not None:
            try:
                ledger.settle(reservation, total)
                return {"status": "settled", "actual_tokens": total}
            except ValueError as error:
                failures.append(f"usage settlement conflict: {error}")
                ledger.settle(reservation, None)
                return {"status": "unresolved", "actual_tokens": None, "conflict": str(error)}
        if not clean_shutdown(result, observer_error, failures):
            ledger.settle(reservation, None)
            return {"status": "unresolved", "actual_tokens": None, "reason": "unclean_close"}
        if unobserved is not None and not provider_unavailable:
            ledger.settle(reservation, None)
            return {"status": "unresolved", "actual_tokens": None, "reason": unobserved}
        current = self.state.ledger_state()["attempts"][reservation]
        reported = result["usage"]["observed_total_tokens"]  # an integer unless provider_unavailable
        observed = max(current["observed"], reported if type(reported) is int else 0)
        bound = max(observed, current["reservation"])
        try:
            ledger.settle(reservation, bound)
        except ValueError as error:
            failures.append(f"usage settlement conflict: {error}")
            ledger.settle(reservation, None)
            return {"status": "unresolved", "actual_tokens": None, "conflict": str(error)}
        settlement = {"status": BOUNDED_USAGE, "actual_tokens": bound, "observed_tokens": observed,
                      "reservation_tokens": current["reservation"]}
        return {**settlement, "settlement_reason": PROVIDER_UNAVAILABLE} if provider_unavailable else settlement

    def _archive(self, entry: dict, state: dict, preflight: dict, fixture: dict, instructions: str,
                 reservation: str, attempt_dir: Path, *, result: Any, observer_error: str | None,
                 failures: list[str], usage_failures: list[str], usage_notices: dict, stop_reasons: list[str],
                 sink_log: StreamingEventLog) -> None:
        """Settle, evaluate, seal, and journal one attempt. Synchronous: it never yields to another lane."""
        attempt_id, model = entry["attempt_id"], entry["model"]
        if result is not None and not isinstance(result, dict):
            failures.append("observer returned a non-object result")
            result = None
        if result is not None:
            result, exact = _json_value(result)
            if not exact:
                failures.append("observer result was not exact JSON; non-JSON values were kept as text")
        total = _known_total(result, usage_failures)
        unobserved = usage_unobserved(result, usage_notices)
        provider = total is None and provider_unavailable_close(result, observer_error, failures, usage_notices)
        # Provisional hold: the moment the observer result shows a failed check. Later steps only
        # add failures, so the final decision in on_archived never lifts it.
        provisional = ("settled" if total is not None
                       else BOUNDED_USAGE if clean_shutdown(result, observer_error, failures)
                       and (unobserved is None or provider) else "unresolved")
        check = self.evaluate(result, fixture=fixture, entry=entry, preflight=preflight, bundle=self.bundle,
                              orchestrator_failures=list(failures), observer_error=observer_error,
                              usage_settlement=provisional, stop_reasons=list(stop_reasons))
        for kind in attempt_hold_kinds(check["passed"], check["failure_reasons"], provisional,
                                       check.get("classification")):
            self.hooks.hold(f"{kind}:{attempt_id}")
        # Only an attempt the evaluator also classifies provider_unavailable settles at its reservation unobserved.
        provider = provider and check.get("classification") == PROVIDER_UNAVAILABLE
        sink_closed = True
        try:
            sink_log.close()
        except Exception as error:
            sink_closed = False
            failures.append(f"event sink close: {type(error).__name__}: {error}")
        settlement = self._settle(reservation, total, result, observer_error, failures, unobserved,
                                  provider_unavailable=provider)
        details = {key: value for key, value in settlement.items() if key not in {"status", "actual_tokens"}}
        self.journal.append("usage_settled", attempt_id=attempt_id, reservation_id=reservation,
                            status="settled" if settlement["actual_tokens"] is not None else "unresolved",
                            actual_tokens=settlement["actual_tokens"], usage_settlement=settlement["status"],
                            **details)
        check = self.evaluate(result, fixture=fixture, entry=entry, preflight=preflight, bundle=self.bundle,
                              orchestrator_failures=failures, observer_error=observer_error,
                              usage_settlement=settlement["status"], stop_reasons=list(stop_reasons))
        # Spec 10 (revision 3): a provider_unavailable attempt pauses all new admission now, before any await;
        # the pause is sealed into the attempt and journaled, and the third in the window holds.
        pause = (self.pauses.record(attempt_id, self.lane_id)
                 if check.get("classification") == PROVIDER_UNAVAILABLE else None)
        if pause is not None and pause["holds_admission"]:
            self.hooks.hold(f"provider_unavailable_limit:{attempt_id}")
        sink_checkpoint = {"count": sink_log.count, "final_hash": sink_log.last_hash, "closed": sink_closed}
        payload = {
            "kind": ATTEMPT_KIND, "live_version": LIVE_VERSION, "phase": self.plan["phase"],
            "plan_hash": self.state.plan_hash, "lane_id": self.plan["lane_id"], "entry_id": entry["entry_id"],
            "attempt_id": attempt_id, "attempt_number": 1, "primary": True, "model": model,
            "reasoning_effort": entry["reasoning_effort"], "world_mode": entry["world_mode"],
            "prompt_condition": entry["prompt_condition"], "reservation_id": reservation,
            "started_journal_seq": state["attempt"]["started_journal_seq"], "preflight": preflight,
            "authorization_hash": self.hooks.authorization_hash,
            "instructions_hash": content_hash(instructions), "fixture_hash": content_hash(fixture),
            "observer_result": result, "observer_error": observer_error,
            "orchestrator": {"evidence_failures": failures, "usage_failures": usage_failures,
                             "collection_stop_reasons": stop_reasons, "sink_checkpoint": sink_checkpoint,
                             "usage_settlement": settlement, "ledger_after": _ledger_summary(self.state.ledger_state()),
                             "provider_pause": pause},
            "check": check,
            "behavioral_observation": self.plan["phase"] != "compatibility",
            "count_in_collection_denominator": self.plan["phase"] == "collection",
        }
        atomic_json(attempt_dir / "attempt.json", seal(payload))
        summary = attempt_summary(payload)
        self.journal.append("attempt_archived", entry_id=entry["entry_id"], attempt_id=attempt_id, summary=summary)
        if pause is not None:
            self.journal.append("provider_pause_started", **pause)
        state["status"] = "archived"
        state["attempt"] = {**state["attempt"], **summary, "status": "archived"}
        self._save()
        self.hooks.on_archived(deepcopy(payload))


def settlement_reason(usage_settlement: Any, recorded: Any) -> str | None:
    """Why an archived attempt settled at its reservation bound: provider_unavailable, or observed usage after a
    clean close. None for any other settlement."""
    if usage_settlement != BOUNDED_USAGE:
        return None
    return recorded if recorded == PROVIDER_UNAVAILABLE else BOUNDED_AFTER_CLEAN_CLOSE


def lane_report(state: _PhaseState) -> dict:
    plan, index = state.plan, state.index
    reasons = {record["data"]["attempt_id"]: record["data"].get("settlement_reason")
               for record in state.journal.of_kind("usage_settled")}
    entries = []
    for entry in plan["planned_order"]:
        current = index["entries"][entry["entry_id"]]
        attempt = current["attempt"] or {}
        archived = current["status"] == "archived"
        entries.append({
            "entry_id": entry["entry_id"], "attempt_id": entry["attempt_id"], "model": entry["model"],
            "planned_index": entry["planned_index"], "status": current["status"],
            "preflight_failures": len(current["preflight_failures"]),
            "termination_kind": attempt.get("termination_kind"),
            "check_passed": attempt.get("check_passed") if archived else None,
            "classification": attempt.get("classification") if archived else None,
            "failure_reasons": attempt.get("failure_reasons"),
            "usage_total_tokens": attempt.get("usage_total_tokens"),
            "usage_settlement": attempt.get("usage_settlement") if archived else None,
            "settlement_reason": settlement_reason(attempt.get("usage_settlement"), reasons.get(entry["attempt_id"]))
            if archived else None,
            "observed_total_tokens": attempt.get("observed_total_tokens"),
            "elapsed_seconds": attempt.get("elapsed_seconds"), "tool_request_count": attempt.get("tool_request_count"),
            **{key: entry.get(key) for key in ENTRY_LABELS},
        })
    counts: dict[str, int] = {}
    for row in entries:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    passed = [row for row in entries if row["status"] == "archived" and row["check_passed"] is True]
    starts = state.journal.of_kind("attempt_started")
    ledger = _ledger_summary(state.ledger_state())
    if ledger is not None:
        # W09 N-a: a bounded charge is a bound, not measured usage; keep the label next to the totals.
        ledger["settled_tokens_by_usage_settlement"] = {
            label: sum(row["usage_total_tokens"] for row in entries if row["status"] == "archived"
                       and row["usage_settlement"] == label and type(row["usage_total_tokens"]) is int)
            for label in ("settled", BOUNDED_USAGE)}
        # Revision 3: a provider_unavailable charge is a bound without any observed usage; keep it apart.
        ledger["bounded_tokens_by_settlement_reason"] = {
            reason: sum(row["usage_total_tokens"] for row in entries if row["settlement_reason"] == reason
                        and type(row["usage_total_tokens"]) is int)
            for reason in (BOUNDED_AFTER_CLEAN_CLOSE, PROVIDER_UNAVAILABLE)}
    return {
        "phase": plan["phase"], "lane_id": plan["lane_id"], "model": plan["model"],
        "reasoning_effort": plan["reasoning_effort"], "directory": str(state.directory), "plan_hash": state.plan_hash,
        "live_model_call_starts": state.call_starts, "maximum_live_calls": plan["maximum_live_calls"],
        "planned_order": [entry["attempt_id"] for entry in plan["planned_order"]],
        "realized_order": [record["data"]["attempt_id"] for record in starts],
        "dispatch_order": [[record["data"].get("dispatch_seq"), record["data"]["attempt_id"]] for record in starts],
        "authorization_hashes": sorted({record["data"].get("authorization_hash") for record in starts
                                        + state.journal.of_kind("run_opened")} - {None}),
        "status_counts": counts, "entries": entries,
        "qualified": (len(passed) == len(entries) and all(type(row["usage_total_tokens"]) is int for row in passed))
        if plan["phase"] == "compatibility" else None,
        "valid_outcomes": len(passed) if plan["phase"] != "compatibility" else None,
        "halted": (index.get("last_run") or {}).get("halted"), "ledger": ledger,
        "behavioral_observation": plan["phase"] != "compatibility",
        "resource_observations": [{key: row[key] for key in ("attempt_id", "model", "reasoning_effort",
                                                             "termination_kind", "classification", "usage_total_tokens",
                                                             "usage_settlement", "settlement_reason",
                                                             "observed_total_tokens", "elapsed_seconds",
                                                             "tool_request_count")}
                                  for row in entries if row["status"] == "archived"],
        "provider_pauses": [record["data"] for record in state.journal.of_kind("provider_pause_started")],
    }


# Scoring runs offline after collection and never touches a live trial. Its files stay in the
# sealed hashes, so verify reports any change as a declared deviation, but they do not gate a run.
POST_HOC_MODULES = frozenset({
    "peer_reporting_v11/score.py", "peer_reporting_v11/structured.py", "peer_reporting_v11/rubric.py",
    "peer_reporting_v11/review.py", "peer_reporting_v11/review_plan.py",
})


def implementation_changes(sealed: dict, *, execution_only: bool = False) -> list[str]:
    """Files whose code, catalog, schema, template, or protocol hash differs from a sealed plan."""
    current = implementation_hashes()
    changed = sorted(name for name in set(sealed) | set(current) if sealed.get(name) != current.get(name))
    return [name for name in changed if name not in POST_HOC_MODULES] if execution_only else changed


@dataclass(frozen=True)
class LaneSpec:
    """One sealed lane of a live root, as its parent plan names it."""

    lane_id: str
    directory: Path
    plan_hash: str
    binary_check: Callable[[str, dict], None] | None = None


def round_floors(pending: list[tuple[int, str, dict]]) -> dict[str, dict[str, int]]:
    """The lowest unstarted round of every lane, grouped by effort."""
    floors: dict[str, dict[str, int]] = {}
    for _, lane, entry in pending:
        value = entry.get("round")
        if type(value) is int:
            lanes = floors.setdefault(entry["reasoning_effort"], {})
            lanes[lane] = min(lanes.get(lane, value), value)
    return floors


def round_barrier_blocks(item: tuple[int, str, dict], floors: dict[str, dict[str, int]]) -> bool:
    """Spec 9: within an effort, no lane starts round r + 2 or later while another lane has unstarted round r."""
    _, lane, entry = item
    value = entry.get("round")
    if type(value) is not int:
        return False
    return any(other != lane and floor <= value - 2
               for other, floor in floors.get(entry["reasoning_effort"], {}).items())


def next_dispatch(pending: list[tuple[int, str, dict]], idle: Callable[[str], bool]) -> tuple[int, str, dict] | None:
    """Spec 9: the lowest unstarted planned order whose lane is idle and that the round barrier allows.

    ``pending`` is sorted by planned order. The lane with the lowest unstarted
    round of its effort is never blocked, so the barrier cannot deadlock.
    """
    floors = round_floors(pending)
    return next((item for item in pending if idle(item[1]) and not round_barrier_blocks(item, floors)), None)


async def _attempt(run: _PhaseRun, entry: dict, sequence: int, hooks: Hooks, errors: dict[str, str]) -> dict | None:
    """One dispatched attempt. Every hold it causes is set before the task ends and its slot is released."""
    lane = run.lane_id
    try:
        halted = await run.run_entry(entry, dispatch_seq=sequence)
    except Exception as error:
        errors[lane] = f"{type(error).__name__}: {str(error)[:500]}"
        hooks.hold(f"lane_error:{lane}:{type(error).__name__}:{str(error)[:500]}")
        return {"reason": "lane_error", "error": errors[lane]}
    except BaseException as error:
        hooks.hold(f"lane_interrupted:{lane}:{type(error).__name__}")
        raise
    if halted is not None and halted.get("reason") not in {"global_admission_hold", PROVIDER_PAUSE}:
        hooks.hold(f"lane_halted:{lane}:{halted.get('reason')}")
    return halted


def _requeue(pending: list[tuple[int, str, dict]], item: tuple[int, str, dict], realized: list[str]) -> None:
    """An entry refused during a provider pause never started; offer it again in planned order, and list it in the
    realized order only when it is dispatched again."""
    pending.append(item)
    pending.sort(key=lambda value: (value[0], value[1]))
    realized.remove(item[2]["attempt_id"])


def _provider_pause(runs: dict[str, _PhaseRun], pauses: ProviderPause) -> dict | None:
    """Journal the end of every pause whose time is over, in the lane that journaled it; return the pause in force.

    A pause of another root of the study, or one recorded at study level whose attempt was never archived here,
    ends without a journal record in this root."""
    pauses.refresh()
    for event in pauses.expired_unended():
        run = runs.get(event["lane_id"])
        if run is not None and any(record["attempt_id"] == event["attempt_id"]
                                   for record in run.state.provider_pauses()[0]):
            run.journal.append("provider_pause_ended", attempt_id=event["attempt_id"], resume_at=event["resume_at"],
                               ended_at=pauses.wall_clock(), window_count=event["window_count"])
        pauses.ended.add(event["attempt_id"])
    return pauses.active()


async def _dispatch(runs: dict[str, _PhaseRun], hooks: Hooks, slots: GlobalSlots, errors: dict[str, str],
                    realized: list[str], pauses: ProviderPause) -> None:
    pending = sorted(((entry["planned_order"], lane, entry) for lane, run in runs.items() for entry in run.pending()),
                     key=lambda item: (item[0], item[1]))
    stop = hooks.admission_check() if pending else None
    if stop is None and pending:
        # Every lane with work checks its environment and its retained ledger before any start.
        for lane, run in runs.items():
            remaining = run.pending()
            if not remaining:
                continue
            refusal = None if await run.environment_verified() else {"reason": "environment_unverified"}
            refusal = refusal or run._ledger_hold(refresh=True)
            if refusal is not None:
                run.halt(remaining[0], refusal)
                hooks.hold(f"lane_halted:{lane}:{refusal['reason']}")
        stop = hooks.admission_check()
    sequence = sum(run.state.call_starts for run in runs.values())
    active: dict[asyncio.Task, tuple[int, str, dict]] = {}
    try:
        while stop is None:
            # Spec 10 (revision 3): during a provider pause nothing new starts; active attempts finish.
            pause = _provider_pause(runs, pauses)
            while slots.free and pause is None:
                busy = {value[1] for value in active.values()}
                item = next_dispatch(pending, lambda lane: lane not in busy and runs[lane].halted is None)
                if item is None:
                    break
                _, lane, entry = item
                run = runs[lane]
                refusal = run.admission_hold()
                if refusal is not None:
                    if refusal.get("reason") != "global_admission_hold":
                        run.halt(entry, refusal)
                        hooks.hold(f"lane_halted:{lane}:{refusal['reason']}")
                    break
                pending.remove(item)
                slots.try_acquire()
                sequence += 1
                realized.append(entry["attempt_id"])
                active[asyncio.create_task(_attempt(run, entry, sequence, hooks, errors))] = item
            if not active:
                if pending and pause is not None:
                    # Poll, so that a stop, the cutoff, or the deadline is still seen while paused.
                    await hooks.pause_sleep(max(0.0, min(pause["remaining_seconds"], hooks.poll_seconds)))
                    stop = hooks.admission_check()
                    continue
                # Unreachable without a hold, since the lane with the lowest unstarted round is never
                # blocked; still, never end a run as complete with work left.
                if pending and hooks.admission_check() is None:
                    hooks.hold("dispatch_blocked")
                break
            timeout = None if pause is None else max(0.001, min(pause["remaining_seconds"], hooks.poll_seconds))
            done, _ = await asyncio.wait(active, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                item = active.pop(task)
                slots.release()
                halted = task.result()
                if halted is not None and halted.get("reason") == PROVIDER_PAUSE:
                    _requeue(pending, item, realized)
                elif halted is not None:
                    runs[item[1]].halted = halted
            stop = hooks.admission_check()
    except BaseException:
        stop = stop or {"reason": "dispatch_interrupted"}
        for task in active:
            task.cancel()
        raise
    finally:
        if active:
            outcomes = await asyncio.gather(*active, return_exceptions=True)
            for item, outcome in zip(active.values(), outcomes):
                slots.release()
                if isinstance(outcome, dict) and outcome.get("reason") == PROVIDER_PAUSE:
                    _requeue(pending, item, realized)
                elif isinstance(outcome, dict) and runs[item[1]].halted is None:
                    runs[item[1]].halted = outcome
        # Every lane that still has unstarted work records why it admitted nothing more.
        stop = stop or hooks.admission_check()
        for lane, run in runs.items():
            remaining = [item[2] for item in pending if item[1] == lane]
            if remaining and run.halted is None and stop is not None:
                try:
                    run.halt(remaining[0], stop)
                except Exception:
                    pass


async def run_lanes(lanes: list[LaneSpec], *, hooks: Hooks, bundle: ProtocolBundle, slots: GlobalSlots,
                    inputs: Callable[[dict], tuple[dict, str]], evaluate: Callable[..., dict]) -> dict:
    """Run, or resume, every lane of one root under one global dispatcher.

    Each lane is opened under its own lock, reconciled, and verified before any
    start. When a slot is free, the dispatcher starts the lowest unstarted
    planned order whose lane is idle; each lane runs one attempt at a time and at
    most ``slots.limit`` attempts run at once. Any hold stops all new dispatch.
    Returns ``{"lanes": {lane_id: report}, "realized_order": [...]}``.
    """
    errors: dict[str, str] = {}
    realized: list[str] = []
    pauses = hooks.provider_pause or ProviderPause(wall_clock=hooks.wall_clock)
    with ExitStack() as stack:
        runs: dict[str, _PhaseRun] = {}
        for spec in lanes:
            directory = Path(spec.directory)
            if not (directory / PLAN_FILE).is_file():
                raise EvidenceError("no sealed lane plan exists; refusing to create one at run time")
            stack.enter_context(_exclusive(directory / LOCK_FILE))
            state = _PhaseState(directory, bundle=bundle, ledger_clock=hooks.ledger_clock)
            stack.callback(state.journal.close)
            if state.plan_hash != spec.plan_hash or state.plan["lane_id"] != spec.lane_id:
                raise LivePhaseError("the sealed lane plan differs from the parent live plan")
            # The lane wall counts only open-run time: the ledger's clock resumes where the last run stopped.
            state.ledger_clock = LaneClock(hooks.ledger_clock, lane_clock_base(state))
            if state.ledger is not None:
                state.ledger.clock = state.ledger_clock
            changes = implementation_changes(state.plan["implementation_hashes"], execution_only=True)
            if changes:
                raise LivePhaseError(f"code, catalog, schema, template, or protocol files changed after sealing: "
                                     f"{changes}; a change requires a new plan revision")
            run = _PhaseRun(state, hooks, bundle=bundle, inputs=inputs, evaluate=evaluate,
                            binary_check=spec.binary_check, pauses=pauses)
            run.reconcile()
            state.verify_attempts()
            runs[spec.lane_id] = run
        # Spec 10 (revision 3): restore every journaled pause, and every pause recorded at study level by any
        # root of the study, so a resumed, restarted, or new root respects an active pause and the window count;
        # a window that still holds the limit holds from the start.
        for run in runs.values():
            records, ended = run.state.provider_pauses()
            pauses.restore(records, ended)
        pauses.refresh()
        limit = pauses.limit_reached()
        if limit is not None:
            hooks.hold(f"retained_provider_unavailable_limit:{limit['attempt_id']}")
        for run in runs.values():
            run.open()
        try:
            await _dispatch(runs, hooks, slots, errors, realized, pauses)
        finally:
            for lane, run in runs.items():
                try:
                    run.close()
                except Exception as error:
                    errors.setdefault(lane, f"{type(error).__name__}: {str(error)[:500]}")
        reports = {lane: {**lane_report(run.state), "error": errors.get(lane)} for lane, run in runs.items()}
    return {"lanes": reports, "realized_order": realized, "provider_pauses": pauses.summary()}


def verify_lane_phase(directory: Path, *, bundle: ProtocolBundle) -> dict:
    """Verify retained lane evidence without writing; return its report."""
    state = _PhaseState(Path(directory), bundle=bundle)
    try:
        unreconciled = state.verify_attempts()
        report = lane_report(state)
        report["cleanup_debt"] = state.cleanup_debt()
    finally:
        state.journal.close()
    report["unreconciled_starts"] = unreconciled
    report["journal"] = {"count": state.journal.count, "final_hash": state.journal.last_hash}
    return report


def plain_json(value: Any) -> Any:
    """Exactly what a sealed JSON file can hold."""
    return json.loads(canonical_json(value))
