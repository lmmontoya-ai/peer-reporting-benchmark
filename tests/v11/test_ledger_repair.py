"""A4 repair and retained evidence checks. All roots and usage are authored offline."""

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.long_events import iter_events
from swarm_auth_bench.peer_reporting.budget import BudgetLedger
from swarm_auth_bench.peer_reporting.live import _exclusive, _Journal
from swarm_auth_bench.peer_reporting.storage import atomic_json, check_seal, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import ledger_repair, live
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.ledger_repair import (
    BIT_MASKS,
    REPAIR_KIND,
    _check_attempts,
    single_bit_candidates,
)

from .live_fakes import caps_record, fake_bundle, study_rows, write_study

DATA = Path(__file__).parent / "data" / "ledger-bitflip-grid"
LANE = "gpt-6-luna-xhigh"


def flipped(raw, offset, mask=0x20):
    return raw[:offset] + bytes([raw[offset] ^ mask]) + raw[offset + 1:]


def test_real_grid_evidence_has_one_candidate_and_exact_journal_reconstruction():
    corrupt = (DATA / "budget-ledger.json").read_bytes()
    marker = read_sealed(DATA / "budget-ledger.json.identity.json")
    caps = json.loads(corrupt)["caps"]
    (candidate,) = single_bit_candidates(corrupt, marker, caps, plan_hash=marker["plan_hash"])
    # The supplied evidence's key starts at 36,834; its changed F is four bytes later.
    assert len(corrupt) == 45323
    assert corrupt.index(b"notiFications") == 36834
    assert (candidate["byte_offset"], candidate["bit_mask"]) == (36838, 0x20)
    assert corrupt[36838] == ord("F") and candidate["bytes"][36838] == ord("f")
    check_seal(candidate["state"])
    records = list(iter_events(DATA / "journal.jsonl"))
    index = read_sealed(DATA / "phase-index.json")
    assert index["plan_hash"] == marker["plan_hash"]
    checkpoint = index["journal"]
    assert records[checkpoint["count"] - 1]["hash"] == checkpoint["final_hash"]
    reconstruction = _check_attempts(candidate["state"], records, caps)
    assert len(records) == 837 and len(reconstruction) == 101
    assert Counter(attempt["status"] for attempt in reconstruction.values()) == {"settled": 100, "active": 1}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """Use the existing offline builders; no runtime, environment check or live runner."""
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    caps = caps_record()
    rows, fixtures = study_rows(cells=(("L1", "violation", "guided", "xhigh", "normal"),))
    study = write_study(tmp_path / "study", rows, fixtures, caps=caps)
    _, _, source = live.load_study(study, "smoke")
    built = live.build_assignment_plan("smoke", rows, fixtures, caps, revision="a4-test", source=source,
                                       gate_evidence={}, bundle=fake_bundle())
    root = study / "roots" / "a4-test"
    live.prepare_live_root(root, built, study_directory=study, bundle=fake_bundle())
    plan = live.read_live_plan(root)
    lane_dir = root / "lanes" / LANE
    lane_plan = read_sealed(lane_dir / "phase-plan.json")
    path = lane_dir / "budget-ledger.json"
    journal = _Journal(lane_dir / "journal.jsonl", lane_plan["seal_hash"], create=False)
    try:
        journal.append("ledger_creating", path=path.name)
        budget = BudgetLedger(path, lane_plan["caps"], plan_hash=lane_plan["seal_hash"], create=True,
                              clock=lambda: 10)
        state = read_sealed(path)
        journal.append("ledger_created", ledger_id=state["ledger_id"], started_at=state["started_at"],
                       caps_hash=content_hash(state["caps"]))
        entry = lane_plan["planned_order"][0]
        reservation = entry["attempt_id"] + "~r1"
        attribution = {"attempt_id": entry["attempt_id"], "entry_id": entry["entry_id"]}
        assert budget.admit(reservation)
        journal.append("reservation_admitted", reservation_id=reservation, **attribution)
        for notification, usage in (("u-1", 1500), ("u-2", 1200), ("u-3", None), ("u-4", 1600)):
            budget.observe(reservation, notification, usage)
            journal.append("usage_observed", reservation_id=reservation, notification_id=notification,
                           cumulative_tokens=usage, **attribution)
        budget.settle(reservation, 1700)
        journal.append("usage_settled", reservation_id=reservation, status="settled", actual_tokens=1700,
                       **attribution)
    finally:
        journal.close()
    return {"root": root, "study": study, "directory": lane_dir, "plan": plan, "lane_plan": lane_plan,
            "budget": budget, "path": path, "original": path.read_bytes(), "reservation": reservation}


def corrupt_lane(lane):
    raw = lane["path"].read_bytes()
    offset = raw.index(b"notifications") + 4
    lane["path"].write_bytes(flipped(raw, offset))
    return offset


def command(lane, capsys):
    code = main(["repair-ledger", str(lane["root"]), "--study", str(lane["study"]), "--lane", LANE,
                 "--reason", "Single-bit storage corruption.", "--approval-text", "Approved A4 offline repair."])
    return code, json.loads(capsys.readouterr().out)


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and path.suffix != ".lock"}


def test_cli_repair_retains_seals_journals_verifies_and_exports(lane, capsys, tmp_path):
    offset = corrupt_lane(lane)
    corrupt = lane["path"].read_bytes()
    before = file_hashes(lane["root"])
    with pytest.raises(ValueError, match="seal mismatch"):
        BudgetLedger(lane["path"], lane["lane_plan"]["caps"], plan_hash=lane["lane_plan"]["seal_hash"])
    code, report = command(lane, capsys)
    assert code == 0 and report["status"] == "repaired" and report["live_model_calls"] == 0
    assert lane["path"].read_bytes() == lane["original"]
    record = read_sealed(lane["root"] / report["repair_record"])
    assert record == report["repair"] and record["kind"] == REPAIR_KIND
    assert (record["byte_offset"], record["bit_mask"]) == (offset, 0x20)
    retained = lane["directory"] / "ledger-repairs" / f"{record['corrupt_sha256']}.corrupt"
    assert retained.read_bytes() == corrupt
    entries = list(iter_events(lane["directory"] / "journal.jsonl"))
    assert entries[-1]["kind"] == "ledger_repaired"
    assert entries[-1]["data"]["repair_hash"] == record["seal_hash"]
    changed = {name for name, value in before.items() if file_hashes(lane["root"])[name] != value}
    assert changed == {f"lanes/{LANE}/budget-ledger.json", f"lanes/{LANE}/journal.jsonl"}
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ledger_repairs"] == [{"record": report["repair_record"], **record}]
    assert verified["lanes"][LANE]["ledger"]["settled_tokens"] == 1700
    assert main(["export-review", str(lane["root"]), "--study", str(lane["study"]),
                 "--output", str(tmp_path / "export"), "--no-score"]) == 0
    exported = json.loads(capsys.readouterr().out)
    index = read_sealed(tmp_path / "export" / "index.json")
    assert exported["ledger_repairs"] == index["ledger_repairs"] == verified["ledger_repairs"]
    assert index["declared_deviations"] == [{"kind": "ledger_repaired", "lane_id": LANE,
                                            "repair_hash": record["seal_hash"]}]
    assert index["analysis_exclusions"] == [] and index["lane_errors"] == {}


@pytest.mark.parametrize("damage", ["two_bits", "invalid_json", "extra_notification", "extra_reservation",
                                    "notification_value", "observed_max", "settlement_actual", "settlement_status",
                                    "reservation_amount", "identity_mismatch", "identity_caps", "identity_seal",
                                    "valid", "journal_chain", "structural", "marker_binding", "noncanonical"])
def test_cli_refusals_leave_evidence_unchanged(lane, capsys, damage):
    if damage == "two_bits":
        corrupt_lane(lane)
        lane["path"].write_bytes(flipped(lane["path"].read_bytes(), 0, 1))
    elif damage == "invalid_json":
        # No one-bit candidate can replace either of these two NULs with JSON punctuation.
        lane["path"].write_bytes(b"\0\0" + lane["original"][2:])
    elif damage == "noncanonical":
        state = json.loads(lane["original"])
        lane["path"].write_bytes((json.dumps(state, sort_keys=True, indent=2) + "\n").encode())
        corrupt_lane(lane)
    elif damage == "extra_reservation":
        lane["budget"].admit("extra-reservation")
        lane["budget"].observe("extra-reservation", "extra-notification", 5)
        corrupt_lane(lane)
    elif damage in {"extra_notification", "notification_value", "observed_max", "settlement_actual",
                    "settlement_status", "reservation_amount", "structural", "marker_binding"}:
        state = read_sealed(lane["path"])
        state.pop("seal_hash")
        attempt = state["attempts"][lane["reservation"]]
        if damage == "extra_notification":
            attempt["notifications"]["extra-notification"] = 5
        elif damage == "notification_value":
            attempt["notifications"]["u-1"] = 1400
        elif damage == "observed_max":
            attempt["observed"] = 1700
        elif damage == "settlement_actual":
            attempt["actual"] = 1701
        elif damage == "settlement_status":
            attempt.update(status="unresolved", actual=None)
        elif damage == "reservation_amount":
            attempt["reservation"] += 1
        elif damage == "structural":
            attempt["admitted_at"] = -1
        elif damage == "marker_binding":
            state["identity_marker_hash"] = "0" * 64
        atomic_json(lane["path"], seal(state))
        corrupt_lane(lane)
    elif damage.startswith("identity_"):
        marker_path = lane["path"].with_suffix(".json.identity.json")
        marker = read_sealed(marker_path)
        marker.pop("seal_hash")
        if damage == "identity_mismatch":
            marker["ledger_id"] = "0" * 32
        elif damage == "identity_caps":
            marker["caps_hash"] = "0" * 64
        atomic_json(marker_path, seal(marker))
        if damage == "identity_seal":
            marker_path.write_bytes(flipped(marker_path.read_bytes(), 2))
        corrupt_lane(lane)
    elif damage == "journal_chain":
        corrupt_lane(lane)
        path = lane["directory"] / "journal.jsonl"
        path.write_bytes(path.read_bytes().replace(b'"u-1"', b'"u-X"'))
    before = file_hashes(lane["root"])
    code, report = command(lane, capsys)
    assert code == 2 and report["live_model_calls"] == 0
    assert file_hashes(lane["root"]) == before
    assert not (lane["directory"] / "ledger-repairs").exists()
    if damage in {"extra_notification", "extra_reservation", "notification_value", "observed_max",
                  "settlement_actual", "settlement_status", "reservation_amount"}:
        assert "exact journal reconstruction" in report["error"]
    elif damage == "valid":
        assert "already verifies" in report["error"]


def test_cli_refuses_repeat_of_the_same_corrupt_bytes(lane, capsys):
    corrupt_lane(lane)
    corrupt = lane["path"].read_bytes()
    assert command(lane, capsys)[0] == 0
    lane["path"].write_bytes(corrupt)
    before = file_hashes(lane["root"])
    code, report = command(lane, capsys)
    assert code == 2 and "same corrupt hash already exists" in report["error"]
    assert file_hashes(lane["root"]) == before


@pytest.mark.parametrize("damage", ["altered", "deleted", "record", "resealed_record", "missing_record",
                                    "wrong_bit_target", "multiple_bit_mask", "reconstruction_hash"])
def test_verify_and_export_refuse_tampered_repair_evidence(lane, capsys, tmp_path, damage):
    corrupt_lane(lane)
    assert command(lane, capsys)[0] == 0
    path = lane["directory"] / "ledger-repairs" / "repair-1.json"
    record = read_sealed(path)
    retained = path.parent / f"{record['corrupt_sha256']}.corrupt"
    if damage == "altered":
        retained.write_bytes(flipped(retained.read_bytes(), 0, 1))
    elif damage == "deleted":
        retained.unlink()
    elif damage == "missing_record":
        path.unlink()
    else:
        record["byte_offset"] += 1
        if damage in {"wrong_bit_target", "multiple_bit_mask", "reconstruction_hash"}:
            raw = retained.read_bytes()
            if damage == "wrong_bit_target":
                record["byte_offset"] -= 1
                record["restored_sha256"] = "0" * 64
            if damage == "multiple_bit_mask":
                record["byte_offset"] -= 1
                record["bit_mask"] = 3
                raw = flipped(lane["original"], record["byte_offset"], 3)
                record["corrupt_sha256"] = hashlib.sha256(raw).hexdigest()
                (path.parent / f"{record['corrupt_sha256']}.corrupt").write_bytes(raw)
            if damage == "reconstruction_hash":
                record["byte_offset"] -= 1
                record["journal_reconstruction_hash"] = "0" * 64
            record["original_byte"] = raw[record["byte_offset"]]
            record["restored_byte"] = record["original_byte"] ^ record["bit_mask"]
        if damage != "record":
            record.pop("seal_hash")
            record = seal(record)
        atomic_json(path, record)
        if damage in {"wrong_bit_target", "multiple_bit_mask", "reconstruction_hash"}:
            # Rebind the declaration in a valid chain to exercise the byte/hash
            # checks themselves, rather than only the seal or journal binding.
            journal_path = lane["directory"] / "journal.jsonl"
            entries = list(iter_events(journal_path))
            entries[-1]["data"]["repair_hash"] = record["seal_hash"]
            entries[-1]["data"]["corrupt_sha256"] = record["corrupt_sha256"]
            entries[-1]["data"]["restored_sha256"] = record["restored_sha256"]
            entries[-1].pop("hash")
            entries[-1]["hash"] = content_hash(entries[-1])
            journal_path.write_bytes("".join(canonical_json(entry) + "\n" for entry in entries).encode())
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert main(["export-review", str(lane["root"]), "--study", str(lane["study"]),
                 "--output", str(tmp_path / "export"), "--no-score"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("lock", ["coordinator.lock", f"lanes/{LANE}/phase.lock"])
def test_repair_refuses_root_or_lane_already_locked(lane, capsys, lock):
    corrupt_lane(lane)
    before = file_hashes(lane["root"])
    with _exclusive(lane["root"] / lock):
        code, report = command(lane, capsys)
    assert code == 2 and "another process holds" in report["error"]
    assert file_hashes(lane["root"]) == before


def test_repair_refuses_ambiguous_candidates_before_journal_comparison(lane, capsys, monkeypatch):
    corrupt_lane(lane)
    monkeypatch.setattr(ledger_repair, "single_bit_candidates", lambda *a, **k: [{}, {}])
    code, report = command(lane, capsys)
    assert code == 2 and "found 2" in report["error"]
    assert not (lane["directory"] / "ledger-repairs").exists()


@pytest.mark.parametrize("location", ["json_quote", "seal_key", "seal_digest", "seal_quote", "newline", "utf8"])
def test_search_matches_brute_force_for_syntax_seal_newline_and_utf8(lane, location):
    lane["budget"].admit("é")
    original = lane["path"].read_bytes()
    marker = read_sealed(lane["path"].with_suffix(".json.identity.json"))
    caps, plan_hash = lane["lane_plan"]["caps"], lane["lane_plan"]["seal_hash"]
    offsets = {"json_quote": 1, "seal_key": original.index(b'"seal_hash"') + 2,
               "seal_digest": original.index(b'"seal_hash":"') + len(b'"seal_hash":"'),
               "seal_quote": original.index(b',"started_at"') - 1,
               "newline": len(original) - 1, "utf8": original.index("é".encode())}
    corrupt = flipped(original, offsets[location], 0x80 if location == "utf8" else 1)
    expected = []
    for offset in range(len(corrupt)):
        for mask in BIT_MASKS:
            raw = flipped(corrupt, offset, mask)
            try:
                ledger_repair._decode_candidate(raw, marker, caps, plan_hash)
            except (ValueError, TypeError, KeyError, UnicodeError):
                continue
            expected.append((offset, mask))
    assert expected == [(offsets[location], 0x80 if location == "utf8" else 1)]
    assert [(candidate["byte_offset"], candidate["bit_mask"])
            for candidate in single_bit_candidates(corrupt, marker, caps, plan_hash=plan_hash)] == expected


def test_historical_repair_verifies_after_further_ordinary_ledger_updates(lane, capsys):
    corrupt_lane(lane)
    assert command(lane, capsys)[0] == 0
    assert lane["budget"].admit("later")
    lane["budget"].observe("later", "later-usage", 5)
    lane["budget"].settle("later", 5)
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 0
    assert len(json.loads(capsys.readouterr().out)["ledger_repairs"]) == 1
