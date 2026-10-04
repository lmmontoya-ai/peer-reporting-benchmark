from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.budget import BudgetLedger
from swarm_auth_bench.peer_reporting.live_integrity import verify_ledger_history
from swarm_auth_bench.peer_reporting.storage import read_sealed

RESERVATION = "attempt-1~r1"


def record(kind, **data):
    return {"kind": kind, "data": data}


def event(kind, **data):
    return record(kind, reservation_id=RESERVATION, **data)


def current(**changes):
    return {"status": "active", "reservation": 75000, "observed": 0, "actual": None,
            "notifications": {}, "admitted_at": 1000.0, **changes}


def state(attempt=None, **changes):
    return {"ledger_id": "a" * 32, "started_at": 1000.0, "caps": {"tokens": 225000},
            "stop_generation": False, "attempts": {RESERVATION: current() if attempt is None else attempt},
            **changes}


def created(snapshot):
    return record("ledger_created", ledger_id=snapshot["ledger_id"], started_at=snapshot["started_at"],
                  caps_hash=content_hash(snapshot["caps"]))


def test_retained_usage_and_settlement_are_compared_without_mutation():
    snapshot = state(current(status="settled", actual=90, observed=70,
                             notifications={"u1": 70, "u2": 40, "u3": None}))
    records = [created(snapshot), event("reservation_admitted"), event("attempt_started"),
               event("usage_observed", notification_id="u1", cumulative_tokens=70),
               event("usage_observed", notification_id="u1", cumulative_tokens=70),
               event("usage_observed", notification_id="u2", cumulative_tokens=40),
               event("usage_observed", notification_id="u3", cumulative_tokens=None),
               event("usage_settled", status="unresolved", actual_tokens=None),
               event("usage_reconciled", total_tokens=90, evidence="provider export")]
    before = deepcopy((snapshot, records))
    verify_ledger_history(snapshot, records)
    assert (snapshot, records) == before


@pytest.mark.parametrize("kind, extra", [
    ("reservation_admitted", {}), ("attempt_started", {}),
    ("usage_observed", {"notification_id": "u1", "cumulative_tokens": 10}),
    ("usage_settled", {"status": "settled", "actual_tokens": 10}),
    ("usage_reconciled", {"total_tokens": 10}), ("orphan_reservation_released", {}),
    ("attempt_interrupted_reconciled", {"ledger_status": "unresolved"}),
])
def test_every_journaled_reservation_must_remain(kind, extra):
    with pytest.raises(ValueError, match="reservation is missing"):
        verify_ledger_history(state(attempts={}), [event(kind, **extra)])


@pytest.mark.parametrize("damage, message", [
    ({"observed": 49}, "observed usage"),
    ({"reservation": 49}, "observed usage"),
    ({"notifications": {}}, "notification is missing"),
    ({"notifications": {"u1": 49}}, "notification differs"),
    ({"notifications": {"u1": None}}, "notification differs"),
    ({"notifications": {"u1": True}}, "retained usage observation"),
])
def test_usage_observation_cannot_be_lost_or_changed(damage, message):
    snapshot = state(current(observed=50, notifications={"u1": 50}))
    snapshot["attempts"][RESERVATION].update(damage)
    with pytest.raises(ValueError, match=message):
        verify_ledger_history(snapshot, [event("usage_observed", notification_id="u1", cumulative_tokens=50)])


@pytest.mark.parametrize("status, actual", [("active", None), ("unresolved", None), ("settled", 79)])
def test_journaled_known_settlement_cannot_be_rolled_back(status, actual):
    with pytest.raises(ValueError, match="settlement|charge"):
        verify_ledger_history(state(current(status=status, actual=actual)),
                              [event("usage_settled", status="settled", actual_tokens=80)])


@pytest.mark.parametrize("unknown", [
    event("usage_observed", notification_id="u1", cumulative_tokens=None),
    event("usage_settled", status="unresolved", actual_tokens=None),
    event("attempt_interrupted_reconciled", ledger_status="unresolved"),
])
def test_unknown_usage_cannot_revert_to_active(unknown):
    snapshot = state(current(notifications={"u1": None}))
    with pytest.raises(ValueError, match="unknown-usage hold"):
        verify_ledger_history(snapshot, [unknown])


def test_latest_reconciliation_charge_is_retained():
    records = [event("usage_settled", status="unresolved", actual_tokens=None),
               event("usage_reconciled", total_tokens=110)]
    with pytest.raises(ValueError, match="charge"):
        verify_ledger_history(state(current(status="settled", actual=100)), records)
    verify_ledger_history(state(current(status="settled", actual=110)), records)
    verify_ledger_history(state(current(status="settled", actual=120)), records)


def test_orphan_release_requires_retained_settlement():
    records = [event("reservation_admitted"), event("orphan_reservation_released")]
    with pytest.raises(ValueError, match="settlement"):
        verify_ledger_history(state(), records)
    verify_ledger_history(state(current(status="settled", actual=0)), records)


def test_ledger_ahead_of_journal_crash_transitions_keep_all_charges(tmp_path):
    caps = {"max_trial_wall_seconds": 180, "drain_grace_seconds": 10, "max_tool_requests_per_trial": 16,
            "trial_observed_token_stop_target": 60000, "reserved_tokens_per_trial": 75000,
            "collection_wall_seconds": 660, "collection_observed_token_stop_target": 225000,
            "max_concurrency": 1}
    ledger = BudgetLedger(tmp_path / "ledger.json", caps, plan_hash="b" * 64, create=True, clock=lambda: 1000.0)
    snapshot = read_sealed(ledger.path)
    records = [created(snapshot)]
    verify_ledger_history(snapshot, records)
    # Each atomic write can succeed just before a process dies before journal.append.
    assert ledger.admit(RESERVATION)
    verify_ledger_history(read_sealed(ledger.path), records)
    records += [event("reservation_admitted"), event("attempt_started")]
    ledger.observe(RESERVATION, "u1", 40000)
    verify_ledger_history(read_sealed(ledger.path), records)
    records += [event("usage_observed", notification_id="u1", cumulative_tokens=40000)]
    ledger.observe(RESERVATION, "u2", None)
    verify_ledger_history(read_sealed(ledger.path), records)
    records += [event("usage_observed", notification_id="u2", cumulative_tokens=None)]
    ledger.settle(RESERVATION, None)
    verify_ledger_history(read_sealed(ledger.path), records)
    records += [event("usage_settled", status="unresolved", actual_tokens=None)]
    ledger.settle(RESERVATION, 50000)
    verify_ledger_history(read_sealed(ledger.path), records)
    records += [event("usage_reconciled", total_tokens=50000)]
    verify_ledger_history(read_sealed(ledger.path), records)
    # A formerly valid, sealed snapshot has the same identity but has lost spending.
    with pytest.raises(ValueError, match="reservation is missing"):
        verify_ledger_history(snapshot, records)


@pytest.mark.parametrize("changes, message", [
    ({"ledger_id": "b" * 32}, "identity"), ({"started_at": 1100.0}, "start time"),
    ({"caps": {"tokens": 999999}}, "caps"),
])
def test_ledger_creation_binding_prevents_replacement(changes, message):
    snapshot = state()
    journal = [created(snapshot)]
    snapshot.update(changes)
    with pytest.raises(ValueError, match=message):
        verify_ledger_history(snapshot, journal)


@pytest.mark.parametrize("reason", ["collection_wall_limit", "collection_token_limit", "clock_regression"])
def test_journaled_budget_stop_cannot_be_reset(reason):
    journal = [record("collection_stop_requested", reason=reason)]
    with pytest.raises(ValueError, match="collection stop"):
        verify_ledger_history(state(), journal)
    verify_ledger_history(state(stop_generation=True), journal)


def test_non_budget_stop_does_not_imply_a_ledger_flag():
    verify_ledger_history(state(), [record("collection_stop_requested", reason="evidence_sink_failed")])


def test_missing_ledger_allowed_only_before_budget_activity():
    verify_ledger_history(None, [record("phase_sealed"), record("ledger_creating")])
    with pytest.raises(ValueError, match="ledger is missing"):
        verify_ledger_history(None, [created(state())])
    with pytest.raises(ValueError, match="ledger is missing"):
        verify_ledger_history(None, [event("attempt_started")])


@pytest.mark.parametrize("snapshot, journal", [
    ([], []), ({"attempts": []}, []), (None, {}), (None, [None]), (None, [{"kind": "x"}]),
    (state(), [event("usage_observed", notification_id="u1")]),
    (state(), [event("usage_observed", notification_id="u1", cumulative_tokens=True)]),
    (state(), [event("usage_settled", status=[], actual_tokens=None)]),
    (state(), [event("usage_settled", status="unresolved", actual_tokens=1)]),
    (state(), [event("usage_reconciled", total_tokens=None)]),
    (state(), [event("attempt_interrupted_reconciled", ledger_status=[])]),
    (state(), [record("attempt_started")]),
])
def test_malformed_inputs_raise_value_error(snapshot, journal):
    with pytest.raises(ValueError):
        verify_ledger_history(snapshot, journal)


def test_conflicting_journal_notifications_are_not_silently_collapsed():
    records = [event("usage_observed", notification_id="same", cumulative_tokens=10),
               event("usage_observed", notification_id="same", cumulative_tokens=20)]
    with pytest.raises(ValueError, match="conflicting"):
        verify_ledger_history(state(), records)


def test_journal_cannot_claim_a_settlement_below_observed_usage():
    records = [event("usage_observed", notification_id="u1", cumulative_tokens=20),
               event("usage_settled", status="settled", actual_tokens=10)]
    with pytest.raises(ValueError, match="settlement is below"):
        verify_ledger_history(state(), records)
