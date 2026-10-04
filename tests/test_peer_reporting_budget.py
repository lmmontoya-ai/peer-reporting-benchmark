from concurrent.futures import ThreadPoolExecutor

import pytest

from swarm_auth_bench.peer_reporting.budget import BudgetLedger
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal

PLAN_HASH = "a" * 64


def limits(**changes):
    return {"max_trial_wall_seconds": 30, "drain_grace_seconds": 2,
            "max_tool_requests_per_trial": 10, "trial_observed_token_stop_target": 100,
            "reserved_tokens_per_trial": 120, "collection_wall_seconds": 300,
            "collection_observed_token_stop_target": 1000, "max_concurrency": 20, **changes}


def test_concurrent_reservations_and_restart_do_not_reset_capacity(tmp_path):
    path = tmp_path / "ledger.json"
    first = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    second = BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda n: (first if n % 2 else second).admit(str(n)), range(12)))
    assert sum(result) == 8  # Eight reservations of 120 fit within 1,000.
    restarted = BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    assert len(restarted.snapshot()["attempts"]) == 8
    assert restarted.snapshot()["ledger_id"] == first.snapshot()["ledger_id"]
    assert not restarted.admit("after-restart")


def test_unknown_usage_holds_admission_until_reconciliation(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("a")
    ledger.observe("a", "usage-1", None)
    assert not ledger.admit("b")
    ledger.observe("a", "usage-2", 20)
    assert not ledger.admit("b")  # A later notification alone is not reconciliation.
    ledger.settle("a", 20)
    assert ledger.admit("b")


def test_retained_unknowns_keep_full_capacity_and_actual_unknown(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(max_concurrency=1,
                          collection_observed_token_stop_target=240), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("a")
    ledger.observe("a", "a-usage", 20)
    ledger.settle("a", None)
    assert not ledger.admit("b")
    assert ledger.admit("b", retained_unresolved=frozenset({"a"}))
    ledger.settle("b", None)
    assert not ledger.admit("c", retained_unresolved=frozenset({"a", "b"}))  # full reservations still charge
    snapshot = ledger.snapshot()
    assert all(a["actual"] is None and a["reservation"] == 120 for a in snapshot["attempts"].values())


def test_retained_unknowns_do_not_override_wall_or_token_stop(tmp_path):
    now = [100]
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(collection_wall_seconds=1),
                          plan_hash=PLAN_HASH, create=True, clock=lambda: now[0])
    assert ledger.admit("a")
    ledger.settle("a", None)
    now[0] += 1
    assert not ledger.admit("b", retained_unresolved=frozenset({"a"}))
    assert ledger.snapshot()["stop_generation"]


def test_retained_unknown_ids_must_cover_exact_unknowns(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("a")
    ledger.settle("a", None)
    assert not ledger.admit("b", retained_unresolved=frozenset({"other"}))
    assert not ledger.admit("b", retained_unresolved=frozenset({"a", "other"}))


def test_duplicate_and_stale_usage_not_double_charged(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(), plan_hash=PLAN_HASH, create=True)
    ledger.admit("a")
    ledger.observe("a", "u1", 70)
    ledger.observe("a", "u1", 70)
    ledger.observe("a", "u2", 40)
    assert ledger.snapshot()["attempts"]["a"]["observed"] == 70
    with pytest.raises(ValueError, match="duplicate"):
        ledger.observe("a", "u1", 71)
    with pytest.raises(ValueError, match="smaller"):
        ledger.settle("a", 69)


def test_overshoot_stops_active_generation_and_admission(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.json", limits(collection_observed_token_stop_target=240),
                          plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("a") and ledger.admit("b")
    assert not ledger.snapshot()["stop_generation"]  # Full reservation is allowed.
    ledger.observe("a", "u1", 121)
    assert ledger.snapshot()["stop_generation"]
    assert not ledger.admit("c")


def test_global_deadline_survives_restart(tmp_path):
    now = [100.0]
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True, clock=lambda: now[0])
    ledger.admit("a")
    now[0] = 400.0
    restarted = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, clock=lambda: now[0])
    assert not restarted.admit("b")
    assert restarted.snapshot()["stop_reason"] == "collection_wall_limit"


def test_default_reopen_requires_existing_ledger_and_marker(tmp_path):
    path = tmp_path / "ledger.json"
    with pytest.raises(FileNotFoundError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    assert not path.exists()
    assert not path.with_suffix(".json.identity.json").exists()
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.snapshot()["plan_hash"] == PLAN_HASH
    assert ledger.marker_path.exists()
    with pytest.raises(FileExistsError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)


def test_lost_ledger_cannot_be_reopened_or_recreated_at_retained_identity(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("charged-attempt")
    marker_before = ledger.marker_path.read_bytes()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    with pytest.raises(FileNotFoundError):
        ledger.admit("after-loss")
    with pytest.raises(FileExistsError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert not path.exists()
    assert ledger.marker_path.read_bytes() == marker_before


def test_missing_marker_prevents_reopen_and_updates(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("charged-attempt")
    before = path.read_bytes()
    ledger.marker_path.unlink()
    with pytest.raises(FileNotFoundError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    with pytest.raises(FileNotFoundError):
        ledger.observe("charged-attempt", "usage", 30)
    with pytest.raises(FileExistsError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert path.read_bytes() == before
    assert not ledger.marker_path.exists()


@pytest.mark.parametrize("caps,plan_hash", [(limits(), "b" * 64), (limits(max_concurrency=19), PLAN_HASH)])
def test_different_plan_or_caps_cannot_reopen_ledger(tmp_path, caps, plan_hash):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("charged-attempt")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="plan or caps mismatch"):
        BudgetLedger(path, caps, plan_hash=plan_hash)
    assert path.read_bytes() == before


@pytest.mark.parametrize("target,field,value", [
    ("ledger", "plan_hash", "b" * 64), ("ledger", "caps", limits(max_concurrency=19)),
    ("marker", "plan_hash", "b" * 64), ("marker", "caps_hash", "b" * 64),
    ("ledger", "ledger_id", "b" * 32), ("ledger", "identity_marker_hash", "b" * 64),
    ("ledger", "started_at", 0), ("marker", "created_at", 0),
])
def test_plan_caps_and_identity_are_rechecked_on_every_update(tmp_path, target, field, value):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert ledger.admit("charged-attempt")
    changed = path if target == "ledger" else ledger.marker_path
    payload = read_sealed(changed)
    payload.pop("seal_hash")
    payload[field] = value
    atomic_json(changed, seal(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ledger.settle("charged-attempt", 30)
    with pytest.raises(ValueError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["ledger", "marker"])
@pytest.mark.parametrize("corruption", ["not json", '[]', '{"seal_hash":"wrong"}'])
def test_corrupt_files_never_reset_or_admit(tmp_path, target, corruption):
    path = tmp_path / "ledger.json"
    ledger = BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    changed = path if target == "ledger" else ledger.marker_path
    changed.write_text(corruption, encoding="utf-8")
    with pytest.raises(ValueError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    with pytest.raises(ValueError):
        ledger.admit("after-corruption")
    with pytest.raises(FileExistsError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert changed.read_text(encoding="utf-8") == corruption


def test_interrupted_creation_keeps_marker_and_blocks_automatic_reset(tmp_path, monkeypatch):
    import swarm_auth_bench.peer_reporting.budget as budget

    path = tmp_path / "ledger.json"
    original = budget.atomic_json

    def fail_ledger_write(target, value):
        if target == path:
            raise OSError("simulated ledger creation failure")
        original(target, value)

    monkeypatch.setattr(budget, "atomic_json", fail_ledger_write)
    with pytest.raises(OSError, match="creation failure"):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)
    assert path.with_suffix(".json.identity.json").exists()
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH)
    with pytest.raises(FileExistsError):
        BudgetLedger(path, limits(), plan_hash=PLAN_HASH, create=True)


@pytest.mark.parametrize("plan_hash", [None, True, "", "short", "A" * 64, "z" * 64])
def test_plan_hash_must_be_a_sealed_plan_digest(tmp_path, plan_hash):
    with pytest.raises(ValueError, match="plan_hash"):
        BudgetLedger(tmp_path / "ledger.json", limits(), plan_hash=plan_hash, create=True)


def test_creation_flag_and_plan_binding_are_explicit(tmp_path):
    with pytest.raises(TypeError, match="plan_hash"):
        BudgetLedger(tmp_path / "ledger.json", limits())
    with pytest.raises(ValueError, match="create"):
        BudgetLedger(tmp_path / "ledger.json", limits(), plan_hash=PLAN_HASH, create=1)
