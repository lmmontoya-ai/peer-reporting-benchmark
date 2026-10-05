"""Durable lane phases: one model at one effort, with its assignments run one at a time.

This is a port of the phase engine in ``peer_reporting.live``. That P1 module is
not edited, because P1 hashes its package files into sealed phase plans. P1's
generic pieces are imported unchanged: the hash-chained journal, the phase lock,
the budget ledger, the ledger-history check, and the archive summary.

Differences from P1:

- each entry carries its reasoning effort, world mode, and prompt condition, and
  the observer, preflight, evaluation, and archive receive them;
- the v1.1 bundle supplies the tools and the world audit;
- coordinator hooks bound concurrency across lanes (``slot``), refuse new
  admission (``admission_check``, consulted before preflight and again with no
  await before the durable start), force-stop active attempts (``external_stop``),
  and see every archived attempt (``on_archived``);
- there are no admission amendments: unknown usage always holds admission.

A lane seals its plan before any model call, reserves budget before the start,
writes ``attempt_started`` before the authenticated session, consumes each
attempt once, and never retries an outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

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

LIVE_VERSION = "peer-reporting-v11-live-phases-v1"
PLAN_KIND = "peer_reporting_v11_lane_phase_plan"
INDEX_KIND = "peer_reporting_v11_lane_phase_index"
ATTEMPT_KIND = "peer_reporting_v11_live_attempt"
ENTRY_LABELS = ("reasoning_effort", "world_mode", "prompt_condition", "split", "arm", "template_id", "level",
                "variant", "near_miss_type", "planned_order")
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


@asynccontextmanager
async def _no_slot() -> AsyncIterator[None]:
    yield


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
    slot: Callable[[], Any] = _no_slot
    external_stop: asyncio.Event | None = None
    on_archived: Callable[[dict], None] = field(default=lambda payload: None)

    def __post_init__(self) -> None:
        for name in ("runtime_factory", "preflight", "environment_check", "observer", "admission_check", "slot",
                     "on_archived"):
            if not callable(getattr(self, name)):
                raise ValueError(f"hook {name} must be callable; live use requires an explicit runtime factory")
        poll = self.poll_seconds
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0 < poll <= 60:
            raise ValueError("poll_seconds must be in (0, 60]")


class _PhaseState:
    """Verified retained lane evidence. Callers mutate it only under the lane lock."""

    def __init__(self, directory: Path, *, bundle: ProtocolBundle, ledger_clock: Callable[[], float] = time.time
                 ) -> None:
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
        """Every start needs a verified archived cleanup result before another start."""
        confirmed = {record["data"]["attempt_id"] for record in self.journal.of_kind("attempt_archived")
                     if record["data"]["summary"].get("cleanup_confirmed") is True}
        return [record["data"]["attempt_id"] for record in self.journal.of_kind("attempt_started")
                if record["data"]["attempt_id"] not in confirmed]

    def verify_attempts(self) -> list[str]:
        """Check every archived attempt against its retained checkpoints; return unreconciled starts."""
        self.verify_budget_history()
        started = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_started")}
        archived = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_archived")}
        unreconciled = self.verify_nonarchived_attempts()
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
        return unreconciled


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
    for position, entry in enumerate(entries):
        if (entry["model"] != plan["model"] or entry["reasoning_effort"] != plan["reasoning_effort"]
                or entry["planned_index"] != position or entry["attempt_id"] != f"{entry['entry_id']}-live-1"
                or not _ATTEMPT_ID.fullmatch(entry["attempt_id"])):
            raise ValueError("lane entry differs from its lane identity or order")
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


class _PhaseRun:
    def __init__(self, state: _PhaseState, hooks: Hooks, *, bundle: ProtocolBundle,
                 inputs: Callable[[dict], tuple[dict, str]], evaluate: Callable[..., dict],
                 binary_check: Callable[[str, dict], None] | None) -> None:
        self.state, self.hooks, self.bundle = state, hooks, bundle
        self.plan, self.index, self.journal = state.plan, state.index, state.journal
        self.directory = state.directory
        self.caps = deepcopy(state.caps)
        self.inputs, self.evaluate, self.binary_check = inputs, evaluate, binary_check

    @property
    def ledger(self) -> BudgetLedger | None:
        return self.state.ledger

    def _save(self) -> None:
        self.state.save_index()

    def reconcile(self) -> None:
        """Make the index agree with the journal after a crash; never reopen a started attempt."""
        self.state.verify_budget_history()
        self.state.verify_nonarchived_attempts()
        if self.state.ledger_recovered:
            self.journal.append("ledger_created", recovered=True, ledger_id=self.state.ledger_state()["ledger_id"])
        starts = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_started")}
        archived = {record["data"]["attempt_id"]: record for record in self.journal.of_kind("attempt_archived")}
        changed = False
        for entry in self.plan["planned_order"]:
            state = self.index["entries"][entry["entry_id"]]
            attempt_id = entry["attempt_id"]
            start = starts.get(attempt_id)
            if start is None:
                if state["status"] not in UNSTARTED:
                    raise EvidenceError(f"{attempt_id}: index records an attempt without a journaled start")
                continue
            if attempt_id in archived and state["status"] != "archived":
                summary = archived[attempt_id]["data"]["summary"]
                state["status"] = "archived"
                state["attempt"] = {**self._started_record(start), **summary, "status": "archived"}
                changed = True
            elif state["status"] in UNSTARTED or state["status"] == "started":
                state["status"] = "incomplete_interrupted"
                state["attempt"] = {**(state["attempt"] or self._started_record(start)),
                                    "status": "incomplete_interrupted"}
                reservation = start["data"]["reservation_id"]
                ledger_status = None
                if self.ledger is not None:
                    current = self.state.ledger_state()["attempts"].get(reservation)
                    if current is not None and current["status"] == "active":
                        self.ledger.settle(reservation, None)
                    ledger_status = (self.state.ledger_state()["attempts"].get(reservation) or {}).get("status")
                self.journal.append("attempt_interrupted_reconciled", entry_id=entry["entry_id"],
                                    attempt_id=attempt_id, reservation_id=reservation, ledger_status=ledger_status,
                                    note="started attempt without an archive; consumed, never rerun")
                changed = True
        if self.ledger is not None:
            admitted = {record["data"]["reservation_id"] for record in self.journal.of_kind("reservation_admitted")}
            started = {record["data"]["reservation_id"] for record in starts.values()}
            planned = {entry["attempt_id"] for entry in self.plan["planned_order"]}
            for reservation, current in self.state.ledger_state()["attempts"].items():
                if reservation.split("~r", 1)[0] not in planned:
                    raise EvidenceError(f"budget reservation {reservation!r} does not belong to this lane")
                if reservation not in started and current["status"] == "active":
                    # Admitted, but the start record was never written: no session start was possible.
                    self.ledger.settle(reservation, 0)
                    self.journal.append("orphan_reservation_released", reservation_id=reservation,
                                        journaled_admission=reservation in admitted)
                    changed = True
        if changed or self.state.ledger_recovered:
            self._save()

    @staticmethod
    def _started_record(start: dict) -> dict:
        data = start["data"]
        return {"attempt_id": data["attempt_id"], "reservation_id": data["reservation_id"],
                "path": f"attempts/{data['attempt_id']}", "started_journal_seq": start["sequence"],
                "status": "started"}

    def _ensure_ledger(self) -> BudgetLedger:
        if self.ledger is None:
            # The lane wall clock starts at the first reservation, not at sealing.
            self.journal.append("ledger_creating", path=LEDGER_FILE)
            self.state.ledger = BudgetLedger(self.state.ledger_path, self.caps, plan_hash=self.state.plan_hash,
                                             create=True, clock=self.hooks.ledger_clock)
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

    def _ledger_hold(self, *, refresh: bool) -> dict | None:
        self.state.verify_budget_history()
        if self.ledger is None:
            return None
        state = self.ledger.snapshot() if refresh else self.state.ledger_state()
        summary = _ledger_summary(state)
        if state["stop_generation"]:
            return {"reason": state["stop_reason"] or "ledger_stop", "ledger": summary}
        if summary["unresolved_reservations"]:
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

    def _pending(self) -> list[dict]:
        return [entry for entry in self.plan["planned_order"]
                if self.index["entries"][entry["entry_id"]]["status"] in UNSTARTED]

    async def run(self, *, implementation_changes: list[str]) -> dict:
        self.journal.append("run_opened", resumed=True, live_version=LIVE_VERSION,
                            call_starts=self.state.call_starts, implementation_changes=implementation_changes)
        halted = None
        task = asyncio.current_task()
        cancelled = False
        pending = self._pending()
        initial_hold = self.hooks.admission_check() if pending else None
        if initial_hold is not None:
            halted = initial_hold
            self.journal.append("admission_held", entry_id=pending[0]["entry_id"], **initial_hold)
        elif pending and await self._environment() is None:
            halted = {"reason": "environment_unverified"}
        elif pending:
            for entry in self.plan["planned_order"]:
                if self.index["entries"][entry["entry_id"]]["status"] not in UNSTARTED:
                    continue
                if self.state.call_starts >= self.plan["maximum_live_calls"]:
                    halted = {"reason": "maximum_live_calls_reached"}
                    break
                async with self.hooks.slot():
                    hold = self.hooks.admission_check() or self._ledger_hold(refresh=True)
                    if hold is not None:
                        halted = hold
                        self.journal.append("admission_held", entry_id=entry["entry_id"], **hold)
                        break
                    halted = await self._run_entry(entry)
                # The adapter absorbs cancellation to reconcile its attempt; the lane must still stop.
                if task is not None and task.cancelling():
                    cancelled = True
                    halted = {"reason": "cancelled"}
                if halted is not None:
                    break
        self.index["last_run"] = {"halted": halted, "resumed": True}
        self.journal.append("run_closed", halted=halted, call_starts=self.state.call_starts)
        self._save()
        if cancelled:
            raise asyncio.CancelledError()
        return lane_report(self.state)

    async def _run_entry(self, entry: dict) -> dict | None:
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
            self.state.verify_budget_history()
            ledger = self._ensure_ledger()
            reservation = self._reservation_id(attempt_id)
            if not ledger.admit(reservation):
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
                    call_start_number=self.state.call_starts + 1,
                    after_preflight_repair=bool(state["preflight_failures"]))
            except Exception:
                ledger.settle(reservation, 0)  # no start record, so no session start was attempted
                raise
            state["status"] = "started"
            state["attempt"] = self._started_record(started)
            self._save()
            attempt_dir.mkdir(parents=True, exist_ok=False)
            handed_off = True
            await self._execute(entry, state, runtime, preflight, fixture, instructions, reservation, attempt_dir)
        finally:
            if not handed_off:
                await _close_quietly(runtime)
        if state["attempt"].get("cleanup_confirmed") is not True:
            return self._ledger_hold(refresh=False) or {"reason": "cleanup_unreconciled", "attempt_id": attempt_id}
        return None

    async def _execute(self, entry: dict, state: dict, runtime: Any, preflight: dict, fixture: dict,
                       instructions: str, reservation: str, attempt_dir: Path) -> None:
        attempt_id, model = entry["attempt_id"], entry["model"]
        ledger = self.ledger
        stop = asyncio.Event()
        stop_reasons: list[str] = []
        failures: list[str] = []
        usage_failures: list[str] = []
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
                request_stop("parent_stop_or_forced_deadline")
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
            result = await self.hooks.observer(
                fixture, instructions, directory=attempt_dir, attempt_id=attempt_id, requested_model=model,
                reasoning_effort=entry["reasoning_effort"], world_mode=entry["world_mode"], bundle=self.bundle,
                caps=deepcopy(self.caps), qualification=deepcopy(preflight), runtime=runtime,
                collection_stop=stop, clock=self.hooks.clock, sleep=self.hooks.sleep,
                event_sink=event_sink, usage_callback=usage_callback)
        except Exception as error:
            observer_error = f"{type(error).__name__}: {error}"[:2000]
            failures.append(f"observer raised: {observer_error}")
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        sink_closed = True
        try:
            sink_log.close()
        except Exception as error:
            sink_closed = False
            failures.append(f"event sink close: {type(error).__name__}: {error}")
        if result is not None and not isinstance(result, dict):
            failures.append("observer returned a non-object result")
            result = None
        if result is not None:
            result, exact = _json_value(result)
            if not exact:
                failures.append("observer result was not exact JSON; non-JSON values were kept as text")
        usage = (result or {}).get("usage") or {}
        total = usage.get("total_tokens")
        if type(total) is not int or total < 0 or usage_failures:
            total = None
        try:
            ledger.settle(reservation, total)
            settlement = {"status": "settled" if total is not None else "unresolved", "actual_tokens": total}
        except ValueError as error:
            ledger.settle(reservation, None)
            settlement = {"status": "unresolved", "actual_tokens": None, "conflict": str(error)}
        self.journal.append("usage_settled", attempt_id=attempt_id, reservation_id=reservation, **settlement)
        check = self.evaluate(result, fixture=fixture, entry=entry, preflight=preflight, bundle=self.bundle,
                              orchestrator_failures=failures, observer_error=observer_error)
        sink_checkpoint = {"count": sink_log.count, "final_hash": sink_log.last_hash, "closed": sink_closed}
        payload = {
            "kind": ATTEMPT_KIND, "live_version": LIVE_VERSION, "phase": self.plan["phase"],
            "plan_hash": self.state.plan_hash, "lane_id": self.plan["lane_id"], "entry_id": entry["entry_id"],
            "attempt_id": attempt_id, "attempt_number": 1, "primary": True, "model": model,
            "reasoning_effort": entry["reasoning_effort"], "world_mode": entry["world_mode"],
            "prompt_condition": entry["prompt_condition"], "reservation_id": reservation,
            "started_journal_seq": state["attempt"]["started_journal_seq"], "preflight": preflight,
            "instructions_hash": content_hash(instructions), "fixture_hash": content_hash(fixture),
            "observer_result": result, "observer_error": observer_error,
            "orchestrator": {"evidence_failures": failures, "usage_failures": usage_failures,
                             "collection_stop_reasons": stop_reasons, "sink_checkpoint": sink_checkpoint,
                             "usage_settlement": settlement, "ledger_after": _ledger_summary(self.state.ledger_state())},
            "check": check,
            "behavioral_observation": self.plan["phase"] != "compatibility",
            "count_in_collection_denominator": self.plan["phase"] == "collection",
        }
        atomic_json(attempt_dir / "attempt.json", seal(payload))
        summary = attempt_summary(payload)
        self.journal.append("attempt_archived", entry_id=entry["entry_id"], attempt_id=attempt_id, summary=summary)
        state["status"] = "archived"
        state["attempt"] = {**state["attempt"], **summary, "status": "archived"}
        self._save()
        self.hooks.on_archived(deepcopy(payload))


def lane_report(state: _PhaseState) -> dict:
    plan, index = state.plan, state.index
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
            "observed_total_tokens": attempt.get("observed_total_tokens"),
            "elapsed_seconds": attempt.get("elapsed_seconds"), "tool_request_count": attempt.get("tool_request_count"),
            **{key: entry.get(key) for key in ENTRY_LABELS},
        })
    counts: dict[str, int] = {}
    for row in entries:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    passed = [row for row in entries if row["status"] == "archived" and row["check_passed"] is True]
    return {
        "phase": plan["phase"], "lane_id": plan["lane_id"], "model": plan["model"],
        "reasoning_effort": plan["reasoning_effort"], "directory": str(state.directory), "plan_hash": state.plan_hash,
        "live_model_call_starts": state.call_starts, "maximum_live_calls": plan["maximum_live_calls"],
        "planned_order": [entry["attempt_id"] for entry in plan["planned_order"]],
        "realized_order": [record["data"]["attempt_id"] for record in state.journal.of_kind("attempt_started")],
        "status_counts": counts, "entries": entries,
        "qualified": (len(passed) == len(entries) and all(type(row["usage_total_tokens"]) is int for row in passed))
        if plan["phase"] == "compatibility" else None,
        "valid_outcomes": len(passed) if plan["phase"] != "compatibility" else None,
        "halted": (index.get("last_run") or {}).get("halted"), "ledger": _ledger_summary(state.ledger_state()),
        "behavioral_observation": plan["phase"] != "compatibility",
        "resource_observations": [{key: row[key] for key in ("attempt_id", "model", "reasoning_effort",
                                                             "termination_kind", "usage_total_tokens",
                                                             "observed_total_tokens", "elapsed_seconds",
                                                             "tool_request_count")}
                                  for row in entries if row["status"] == "archived"],
    }


async def run_lane_phase(directory: Path, *, plan_hash: str, hooks: Hooks, bundle: ProtocolBundle,
                         inputs: Callable[[dict], tuple[dict, str]], evaluate: Callable[..., dict],
                         binary_check: Callable[[str, dict], None] | None = None) -> dict:
    """Resume one sealed lane under its lock; the plan must be the one the parent plan sealed."""
    directory = Path(directory)
    if not (directory / PLAN_FILE).is_file():
        raise EvidenceError("no sealed lane plan exists; refusing to create one at run time")
    with _exclusive(directory / LOCK_FILE):
        state = _PhaseState(directory, bundle=bundle, ledger_clock=hooks.ledger_clock)
        try:
            if state.plan_hash != plan_hash:
                raise LivePhaseError("the sealed lane plan differs from the parent live plan")
            sealed_sources = state.plan["implementation_hashes"]
            current_sources = implementation_hashes()
            changes = sorted(name for name in set(sealed_sources) | set(current_sources)
                             if sealed_sources.get(name) != current_sources.get(name))
            run = _PhaseRun(state, hooks, bundle=bundle, inputs=inputs, evaluate=evaluate,
                            binary_check=binary_check)
            run.reconcile()
            state.verify_attempts()
            return await run.run(implementation_changes=changes)
        finally:
            state.journal.close()


def verify_lane_phase(directory: Path, *, bundle: ProtocolBundle) -> dict:
    """Verify retained lane evidence without writing; return its report."""
    state = _PhaseState(Path(directory), bundle=bundle)
    try:
        unreconciled = state.verify_attempts()
        report = lane_report(state)
    finally:
        state.journal.close()
    report["unreconciled_starts"] = unreconciled
    report["journal"] = {"count": state.journal.count, "final_hash": state.journal.last_hash}
    return report


def plain_json(value: Any) -> Any:
    """Exactly what a sealed JSON file can hold."""
    return json.loads(canonical_json(value))
