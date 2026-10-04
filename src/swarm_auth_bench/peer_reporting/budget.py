"""Persisted reservations for a single collection, with cross-process file locks.

This is an offline-tested controller component. Provider usage stops remain soft;
the live adapter must stop generation when this ledger raises stop_generation.
"""

from __future__ import annotations

import math
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ..events import content_hash
from .config import validate_caps
from .storage import atomic_json, read_sealed, seal

LEDGER_VERSION = "peer-reporting-budget-v2"
IDENTITY_VERSION = "peer-reporting-budget-identity-v2"


def _digest(value: Any) -> bool:
    return (type(value) is str and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _time_value(value: Any) -> bool:
    return (type(value) in (int, float) and value >= 0
            and (type(value) is int or math.isfinite(value)))


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _fields(value: Any, expected: set[str], description: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"invalid budget {description} fields")


@contextmanager
def _locked(path: Path):
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class BudgetLedger:
    """Open one plan's ledger, or explicitly create its ledger and retained marker.

    ``plan_hash`` is the sealed collection plan's hash. ``create=True`` is only
    for a new path with neither a ledger nor an identity marker. The marker is
    written first and never updated, so loss of a ledger cannot reset spending
    through the creation path. Incomplete creation requires explicit recovery;
    this class never silently repairs or replaces retained evidence.
    """

    def __init__(self, path: Path, caps: dict, *, plan_hash: str, create: bool = False,
                 clock: Callable[[], float] = time.time):
        validate_caps(caps)
        if not _digest(plan_hash):
            raise ValueError("plan_hash must be a sealed plan's lowercase SHA-256 hash")
        if type(create) is not bool:
            raise ValueError("create must be a boolean")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.marker_path = self.path.with_suffix(self.path.suffix + ".identity.json")
        self.caps = dict(caps)
        self.plan_hash = plan_hash
        self.clock = clock
        self._marker_hash: str | None = None
        with _locked(self.lock_path):
            if create:
                if self.path.exists() or self.marker_path.exists():
                    raise FileExistsError("budget ledger or retained identity already exists; creation refused")
                started_at = self._now()
                marker = seal({
                    "schema_version": IDENTITY_VERSION, "ledger_id": secrets.token_hex(16),
                    "plan_hash": plan_hash, "caps_hash": content_hash(self.caps), "created_at": started_at,
                })
                atomic_json(self.marker_path, marker)
                atomic_json(self.path, seal({
                    "schema_version": LEDGER_VERSION, "ledger_id": marker["ledger_id"],
                    "plan_hash": plan_hash, "identity_marker_hash": marker["seal_hash"],
                    "caps": self.caps, "started_at": started_at, "attempts": {},
                    "stop_generation": False, "stop_reason": None,
                }))
            self._read_verified()

    def _now(self) -> int | float:
        value = self.clock()
        if not _time_value(value):
            raise ValueError("budget clock must return a finite nonnegative number")
        return value

    def _read_verified(self) -> dict[str, Any]:
        """Require both sealed files and their original binding under the lock."""
        marker = read_sealed(self.marker_path)
        _fields(marker, {"schema_version", "ledger_id", "plan_hash", "caps_hash", "created_at", "seal_hash"},
                "identity marker")
        ledger_id = marker["ledger_id"]
        if (marker["schema_version"] != IDENTITY_VERSION or type(ledger_id) is not str
                or len(ledger_id) != 32 or any(character not in "0123456789abcdef" for character in ledger_id)
                or not _time_value(marker["created_at"])):
            raise ValueError("invalid budget identity marker")
        if marker["plan_hash"] != self.plan_hash or marker["caps_hash"] != content_hash(self.caps):
            raise ValueError("budget identity marker plan or caps mismatch")
        if self._marker_hash is not None and marker["seal_hash"] != self._marker_hash:
            raise ValueError("budget identity marker changed")
        state = read_sealed(self.path)
        _fields(state, {"schema_version", "ledger_id", "plan_hash", "identity_marker_hash", "caps",
                        "started_at", "attempts", "stop_generation", "stop_reason", "seal_hash"}, "ledger")
        if (state["schema_version"] != LEDGER_VERSION or state["ledger_id"] != ledger_id
                or state["plan_hash"] != self.plan_hash or state["identity_marker_hash"] != marker["seal_hash"]
                or state["caps"] != self.caps or state["started_at"] != marker["created_at"]):
            raise ValueError("budget ledger identity, plan, caps, or start time mismatch")
        validate_caps(state["caps"])
        if (not _time_value(state["started_at"]) or type(state["attempts"]) is not dict
                or type(state["stop_generation"]) is not bool
                or type(state["stop_reason"]) not in (str, type(None))
                or state["stop_reason"] not in {None, "collection_wall_limit", "clock_regression",
                                                 "collection_token_limit"}):
            raise ValueError("invalid budget ledger state")
        for attempt_id, attempt in state["attempts"].items():
            if type(attempt_id) is not str or not 1 <= len(attempt_id) <= 200:
                raise ValueError("invalid budget attempt ID")
            _fields(attempt, {"status", "reservation", "observed", "actual", "notifications", "admitted_at"},
                    "attempt")
            if (type(attempt["status"]) is not str or attempt["status"] not in {"active", "unresolved", "settled"}
                    or not _integer(attempt["reservation"], self.caps["reserved_tokens_per_trial"])
                    or not _integer(attempt["observed"]) or attempt["reservation"] < attempt["observed"]
                    or not _time_value(attempt["admitted_at"]) or type(attempt["notifications"]) is not dict):
                raise ValueError("invalid budget attempt state")
            if attempt["status"] == "settled":
                if not _integer(attempt["actual"], attempt["observed"]):
                    raise ValueError("invalid settled budget usage")
            elif attempt["actual"] is not None:
                raise ValueError("unsettled budget attempt has final usage")
            for notification_id, usage in attempt["notifications"].items():
                if (type(notification_id) is not str or not 1 <= len(notification_id) <= 200
                        or (usage is not None and not _integer(usage))):
                    raise ValueError("invalid budget usage notification")
        self._marker_hash = marker["seal_hash"]
        return state

    @staticmethod
    def _totals(state: dict) -> tuple[int, int, bool]:
        settled = sum(a["actual"] for a in state["attempts"].values() if a["status"] == "settled")
        reserved = sum(a["reservation"] for a in state["attempts"].values() if a["status"] != "settled")
        unknown = any(a["status"] == "unresolved" for a in state["attempts"].values())
        return settled, reserved, unknown

    def _update(self, operation: Callable[[dict], Any]) -> Any:
        with _locked(self.lock_path):
            sealed = self._read_verified()
            state = {k: v for k, v in sealed.items() if k != "seal_hash"}
            elapsed = self._now() - state["started_at"]
            if elapsed < 0 or elapsed >= self.caps["collection_wall_seconds"]:
                state.update(stop_generation=True, stop_reason="collection_wall_limit" if elapsed >= 0
                             else "clock_regression")
            result = operation(state)
            settled, reserved, _ = self._totals(state)
            observed = settled + sum(a["observed"] for a in state["attempts"].values()
                                     if a["status"] != "settled")
            target = self.caps["collection_observed_token_stop_target"]
            if settled + reserved > target or observed >= target:
                state.update(stop_generation=True, stop_reason="collection_token_limit")
            atomic_json(self.path, seal(state))
            return result

    def admit(self, attempt_id: str) -> bool:
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 200:
            raise ValueError("bounded attempt ID required")

        def operation(state: dict) -> bool:
            if attempt_id in state["attempts"]:
                raise ValueError("attempt already reserved; use a distinct recovery ID")
            settled, reserved, unknown = self._totals(state)
            active = sum(a["status"] != "settled" for a in state["attempts"].values())
            initial = self.caps["reserved_tokens_per_trial"]
            if (state["stop_generation"] or unknown or active >= self.caps["max_concurrency"]
                    or settled + reserved + initial > self.caps["collection_observed_token_stop_target"]):
                return False
            state["attempts"][attempt_id] = {
                "status": "active", "reservation": initial, "observed": 0,
                "actual": None, "notifications": {}, "admitted_at": self._now(),
            }
            return True
        return self._update(operation)

    def observe(self, attempt_id: str, notification_id: str, cumulative_tokens: int | None) -> None:
        if not isinstance(notification_id, str) or not notification_id or len(notification_id) > 200:
            raise ValueError("bounded notification ID required")
        if cumulative_tokens is not None and (type(cumulative_tokens) is not int or cumulative_tokens < 0):
            raise ValueError("usage must be a nonnegative integer or explicitly unknown")

        def operation(state: dict) -> None:
            attempt = state["attempts"][attempt_id]
            if attempt["status"] == "settled":
                raise ValueError("cannot charge a settled attempt again")
            notifications = attempt["notifications"]
            if notification_id in notifications:
                if notifications[notification_id] != cumulative_tokens:
                    raise ValueError("conflicting duplicate usage notification")
                return
            notifications[notification_id] = cumulative_tokens
            if cumulative_tokens is None:
                attempt["status"] = "unresolved"
            else:
                attempt["observed"] = max(attempt["observed"], cumulative_tokens)
                attempt["reservation"] = max(attempt["reservation"], cumulative_tokens)
        self._update(operation)

    def settle(self, attempt_id: str, actual_tokens: int | None) -> None:
        if actual_tokens is not None and (type(actual_tokens) is not int or actual_tokens < 0):
            raise ValueError("settled usage must be a nonnegative integer or unknown")

        def operation(state: dict) -> None:
            attempt = state["attempts"][attempt_id]
            if attempt["status"] == "settled":
                raise ValueError("attempt already settled")
            if actual_tokens is None:
                attempt["status"] = "unresolved"
            elif actual_tokens < attempt["observed"]:
                raise ValueError("reconciled total is smaller than observed usage")
            else:
                attempt.update(status="settled", actual=actual_tokens)
        self._update(operation)

    def snapshot(self) -> dict:
        self._update(lambda state: None)
        with _locked(self.lock_path):
            return self._read_verified()
