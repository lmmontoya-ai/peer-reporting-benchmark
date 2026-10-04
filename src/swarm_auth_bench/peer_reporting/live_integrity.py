"""Compare retained budget state with independently journaled phase evidence.

This module performs no I/O. Call it after verifying the journal's hash chain
and the budget ledger's schema, seal, and identity marker, before admitting any
new work. The journal can lag a successful atomic ledger write after a crash;
the reverse must never erase a reservation, a usage observation, or a charge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import content_hash


@dataclass
class _Reservation:
    observed_floor: int = 0
    settled_floor: int | None = None
    unknown: bool = False
    requires_settled: bool = False
    notifications: dict[str, int | None] = field(default_factory=dict)


def _identifier(value: Any, description: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 200:
        raise ValueError(f"invalid journal {description}")
    return value


def _tokens(value: Any, description: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {description}")
    return value


def verify_ledger_history(ledger_state: dict | None, journal_records: list[dict]) -> None:
    """Reject retained ledger state that loses journaled spending or admission.

    Additional ledger reservations and notifications are allowed. A ledger may
    also contain a known settlement before its journal record was appended.
    Such states retain the charge and are safe for the caller's normal crash
    reconciliation. Journaled unknown usage must stay unresolved or settled;
    it cannot return to an active reservation. Known settlements must remain
    settled at no less than the journaled charge.

    This is an integrity check, not a repair operation. It does not change the
    ledger, journal, or attempt classifications. Invalid inputs raise ValueError.
    """
    if type(journal_records) is not list:
        raise ValueError("journal records must be a list")
    reservations: dict[str, _Reservation] = {}
    creations: list[dict] = []
    stopped = False
    relevant = {
        "reservation_admitted", "attempt_started", "usage_observed", "usage_settled",
        "usage_reconciled", "orphan_reservation_released", "attempt_interrupted_reconciled",
    }
    for record in journal_records:
        if type(record) is not dict or type(record.get("kind")) is not str or type(record.get("data")) is not dict:
            raise ValueError("invalid journal record")
        kind, data = record["kind"], record["data"]
        if kind == "ledger_created":
            _identifier(data.get("ledger_id"), "ledger identity")
            creations.append(data)
        if kind == "collection_stop_requested" and type(data.get("reason")) is str and data["reason"] in {
            "collection_wall_limit", "collection_token_limit", "clock_regression",
        }:
            stopped = True
        if kind not in relevant:
            continue
        reservation_id = _identifier(data.get("reservation_id"), "reservation ID")
        history = reservations.setdefault(reservation_id, _Reservation())
        if kind == "usage_observed":
            notification_id = _identifier(data.get("notification_id"), "usage notification ID")
            if "cumulative_tokens" not in data:
                raise ValueError("journal usage observation omits its value")
            usage = _tokens(data["cumulative_tokens"], "journal usage observation", nullable=True)
            if notification_id in history.notifications and history.notifications[notification_id] != usage:
                raise ValueError(f"{reservation_id}: conflicting journal usage notification")
            history.notifications[notification_id] = usage
            if usage is None:
                history.unknown = True
            else:
                history.observed_floor = max(history.observed_floor, usage)
        elif kind in {"usage_settled", "usage_reconciled", "orphan_reservation_released"}:
            if kind == "orphan_reservation_released":
                status, actual = "settled", 0
            elif kind == "usage_reconciled":
                status = "settled"
                actual = _tokens(data.get("total_tokens"), "journal reconciled usage")
            else:
                status = data.get("status")
                if type(status) is not str or status not in {"settled", "unresolved"} or "actual_tokens" not in data:
                    raise ValueError("invalid journal usage settlement")
                actual = _tokens(data["actual_tokens"], "journal settled usage", nullable=status == "unresolved")
                if status == "unresolved" and actual is not None:
                    raise ValueError("unresolved journal settlement has known usage")
            if status == "settled":
                if actual < history.observed_floor:
                    raise ValueError(f"{reservation_id}: journal settlement is below observed usage")
                history.settled_floor = max(history.settled_floor or 0, actual)
                history.requires_settled = True
            else:
                history.unknown = True
        elif kind == "attempt_interrupted_reconciled":
            status = data.get("ledger_status")
            if type(status) not in {str, type(None)} or status not in {None, "active", "unresolved", "settled"}:
                raise ValueError("invalid journal interrupted reservation status")
            history.unknown |= status == "unresolved"
            history.requires_settled |= status == "settled"

    if ledger_state is None:
        if creations or reservations or stopped:
            raise ValueError("budget ledger is missing after journaled budget activity")
        return
    if type(ledger_state) is not dict or type(ledger_state.get("attempts")) is not dict:
        raise ValueError("invalid retained budget state")
    for creation in creations:
        if ledger_state.get("ledger_id") != creation["ledger_id"]:
            raise ValueError("budget ledger identity differs from journaled creation")
        if "started_at" in creation and ledger_state.get("started_at") != creation["started_at"]:
            raise ValueError("budget ledger start time differs from journaled creation")
        if "caps_hash" in creation and content_hash(ledger_state.get("caps")) != creation["caps_hash"]:
            raise ValueError("budget ledger caps differ from journaled creation")
    if stopped and ledger_state.get("stop_generation") is not True:
        raise ValueError("budget ledger lost its journaled collection stop")

    for reservation_id, history in reservations.items():
        current = ledger_state["attempts"].get(reservation_id)
        if type(current) is not dict:
            raise ValueError(f"{reservation_id}: journaled reservation is missing from budget ledger")
        status = current.get("status")
        if type(status) is not str or status not in {"active", "unresolved", "settled"}:
            raise ValueError(f"{reservation_id}: invalid retained reservation status")
        observed = _tokens(current.get("observed"), "retained observed usage")
        reserved = _tokens(current.get("reservation"), "retained reservation")
        if observed < history.observed_floor or reserved < observed:
            raise ValueError(f"{reservation_id}: budget ledger lost journaled observed usage")
        notifications = current.get("notifications")
        if type(notifications) is not dict:
            raise ValueError(f"{reservation_id}: invalid retained usage notifications")
        for notification_id, usage in history.notifications.items():
            if notification_id not in notifications:
                raise ValueError(f"{reservation_id}: journaled usage notification is missing")
            retained = _tokens(notifications[notification_id], "retained usage observation", nullable=True)
            if retained != usage:
                raise ValueError(f"{reservation_id}: retained usage notification differs from journal")
        if history.requires_settled and status != "settled":
            raise ValueError(f"{reservation_id}: budget ledger lost a journaled settlement")
        if history.unknown and status == "active":
            raise ValueError(f"{reservation_id}: budget ledger lost an unknown-usage hold")
        if status == "settled":
            actual = _tokens(current.get("actual"), "retained settled usage")
            if actual < max(observed, history.settled_floor or 0):
                raise ValueError(f"{reservation_id}: budget ledger lost a journaled charge")
        elif current.get("actual") is not None:
            raise ValueError(f"{reservation_id}: unsettled reservation has known usage")
