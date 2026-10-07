"""A4 repair and retained evidence checks. All roots and usage are authored offline."""

import hashlib
import json
import os
import subprocess
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
    BINDING_KIND,
    BIT_MASKS,
    REPAIR_KIND,
    _check_attempts,
    build_ledger_repair_binding,
    single_bit_candidates,
)

from .live_fakes import caps_record, fake_bundle, study_rows, write_study

DATA = Path(__file__).parent / "data" / "ledger-bitflip-grid"
LANE = "gpt-6-luna-xhigh"
APPROVAL = "Approved A4 offline repair."


def committed_evidence(directory, raw, identity, journal):
    directory.mkdir()
    for name, value in (("budget-ledger.json", raw), ("budget-ledger.json.identity.json", identity),
                        ("journal.jsonl", journal)):
        (directory / name).write_bytes(value)
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "Offline synthetic repair evidence.")):
        subprocess.run(["git", "-C", str(directory), "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                        "-c", "user.name=Offline test", "-c", "user.email=offline@example.invalid", *args],
                       check=True, capture_output=True)
    return directory


def bind_lane(lane, evidence):
    binding = build_ledger_repair_binding(evidence, study=lane["study"].name, root=lane["root"].name,
                                         lane_id=LANE, plan_hash=lane["plan"]["seal_hash"], commit="HEAD",
                                         approval_text=APPROVAL)
    atomic_json(lane["binding"], binding)
    return binding


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
    result = {"root": root, "study": study, "directory": lane_dir, "plan": plan, "lane_plan": lane_plan,
              "budget": budget, "path": path, "original": path.read_bytes(), "reservation": reservation,
              "binding": tmp_path / "binding.json"}
    raw = result["original"]
    evidence = committed_evidence(tmp_path / "evidence", flipped(raw, raw.index(b"notifications") + 4),
                                  path.with_suffix(".json.identity.json").read_bytes(),
                                  (lane_dir / "journal.jsonl").read_bytes())
    result["evidence"] = evidence
    bind_lane(result, evidence)
    return result


def corrupt_lane(lane):
    raw = lane["path"].read_bytes()
    offset = raw.index(b"notifications") + 4
    lane["path"].write_bytes(flipped(raw, offset))
    return offset


def command(lane, capsys):
    code = main(["repair-ledger", str(lane["root"]), "--study", str(lane["study"]), "--lane", LANE,
                 "--reason", "Single-bit storage corruption.", "--approval-text", APPROVAL,
                 "--binding", str(lane["binding"])])
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
        assert "approved binding" in report["error"]
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
                                    "missing_binding", "binding", "resealed_binding",
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
    elif damage in {"missing_binding", "binding", "resealed_binding"}:
        binding_path = path.parent / f"binding-{record['binding_hash']}.json"
        if damage == "missing_binding":
            binding_path.unlink()
        else:
            binding = read_sealed(binding_path)
            binding["commit"] = "0" * 40
            if damage == "resealed_binding":
                binding.pop("seal_hash")
                binding = seal(binding)
            atomic_json(binding_path, binding)
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



def test_binding_command_pins_committed_real_grid_evidence(tmp_path, capsys):
    output = tmp_path / "binding.json"
    assert main(["ledger-repair-binding", str(DATA), "--study", "social-study", "--root", "social-grid-v1",
                 "--lane", LANE, "--plan-hash", "a" * 64, "--commit", "8cbe359",
                 "--approval-text", APPROVAL, "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    binding = read_sealed(output)
    assert report["binding"] == binding and report["live_model_calls"] == 0
    assert binding["kind"] == BINDING_KIND
    assert binding["commit"] == "8cbe359fef80a26d79ffd1d7733e3116b47958f8"
    assert binding["corrupt_sha256"] == "c36e197e5fe087106cb926f4056fbcb7cd154c38d7c173b820744c5a83ca524e"
    assert binding["restored_sha256"] == "3a2f1ac6c96b8cabddf19a4620f6ba7317719042de69546bf07028a4e7f4f235"
    assert (binding["byte_offset"], binding["bit_mask"]) == (36838, 0x20)
    assert binding["identity_seal_hash"] == "fbc5a63a727670a07d9c5011ab2d1c5a1d8eef4346e2f20b3c3fff70553b2b30"
    assert binding["journal"] == {
        "count": 837, "final_hash": "1da94371017eab2d8d71689875ef6de93ef4047d505e7c0cecb11dd6870df970",
        "sha256": "a56ccce6049ae9878198cffa7c70523717311bae0e4091cd427c0ec81b2af5f8"}


@pytest.mark.parametrize("damage", ["dirty", "uncommitted", "zero", "several"])
def test_binding_refuses_uncommitted_or_nonunique_evidence(lane, tmp_path, monkeypatch, damage):
    evidence = lane["evidence"]
    if damage == "dirty":
        path = evidence / "budget-ledger.json"
        path.write_bytes(flipped(path.read_bytes(), 0, 1))
    elif damage == "uncommitted":
        evidence = tmp_path / "outside-git"
        evidence.mkdir()
    else:
        monkeypatch.setattr(ledger_repair, "single_bit_candidates", lambda *a, **k: [] if damage == "zero" else [{}, {}])
    with pytest.raises(ValueError):
        build_ledger_repair_binding(evidence, study="study", root="a4-test", lane_id=LANE,
                                   plan_hash=lane["plan"]["seal_hash"], commit="HEAD", approval_text=APPROVAL)


@pytest.mark.parametrize("field", sorted(ledger_repair.BINDING_FIELDS) + ["count", "final_hash", "sha256"])
def test_every_changed_binding_value_refuses(lane, capsys, field):
    corrupt_lane(lane)
    binding = read_sealed(lane["binding"])
    owner = binding["journal"] if field in {"count", "final_hash", "sha256"} else binding
    value = owner[field]
    owner[field] = value + 1 if type(value) is int else "changed"
    atomic_json(lane["binding"], binding)
    before = file_hashes(lane["root"])
    assert command(lane, capsys)[0] == 2
    assert file_hashes(lane["root"]) == before


@pytest.mark.parametrize("field", ["study", "root", "lane_id", "plan_hash", "corrupt_sha256", "restored_sha256",
                                   "byte_offset", "bit_mask", "identity_seal_hash", "count", "final_hash", "sha256",
                                   "approval_text"])
def test_resealed_binding_pins_are_compared_to_live_files(lane, capsys, field):
    corrupt_lane(lane)
    binding = read_sealed(lane["binding"])
    binding.pop("seal_hash")
    owner = binding["journal"] if field in {"count", "final_hash", "sha256"} else binding
    value = owner[field]
    if field == "bit_mask":
        owner[field] = 1
    elif type(value) is int:
        owner[field] += 1
    else:
        owner[field] = "0" * 64 if len(value) == 64 else "other"
    atomic_json(lane["binding"], seal(binding))
    before = file_hashes(lane["root"])
    assert command(lane, capsys)[0] == 2
    assert file_hashes(lane["root"]) == before


def rewrite_journal(path, edit):
    records = list(iter_events(path))
    edit(records)
    previous = "0" * 64
    for record in records:
        record.pop("hash")
        record["previous_hash"] = previous
        record["hash"] = previous = content_hash(record)
    path.write_bytes("".join(canonical_json(record) + "\n" for record in records).encode())


@pytest.mark.parametrize("fabrication", ["rollback_unresolved", "admitted_at", "settlement", "ledger_id"])
@pytest.mark.parametrize("binding", [False, True])
def test_astra_fabrications_refuse_without_or_with_genuine_binding(lane, capsys, fabrication, binding):
    journal_path = lane["directory"] / "journal.jsonl"
    if fabrication == "rollback_unresolved":
        # Legitimate observation crash window, followed by a fabricated active snapshot.
        entry = lane["lane_plan"]["planned_order"][0]
        reservation = entry["attempt_id"] + "~r2"
        assert lane["budget"].admit(reservation)
        journal = _Journal(journal_path, lane["lane_plan"]["seal_hash"], create=False)
        try:
            journal.append("reservation_admitted", reservation_id=reservation, attempt_id=entry["attempt_id"],
                           entry_id=entry["entry_id"])
        finally:
            journal.close()
        active = lane["path"].read_bytes()
        lane["budget"].observe(reservation, "unknown-crash-window", None)
        assert read_sealed(lane["path"])["attempts"][reservation]["status"] == "unresolved"
        lane["path"].write_bytes(active)
    else:
        state = read_sealed(lane["path"])
        state.pop("seal_hash")
        if fabrication == "admitted_at":
            state["attempts"][lane["reservation"]]["admitted_at"] += 1
        elif fabrication == "settlement":
            state["attempts"][lane["reservation"]]["actual"] = 1600
            def lower(records):
                next(record for record in records if record["kind"] == "usage_settled")["data"]["actual_tokens"] = 1600
            rewrite_journal(journal_path, lower)
        else:
            marker_path = lane["path"].with_suffix(".json.identity.json")
            marker = read_sealed(marker_path)
            marker.pop("seal_hash")
            marker["ledger_id"] = "0" * 32
            marker = seal(marker)
            atomic_json(marker_path, marker)
            state["ledger_id"] = marker["ledger_id"]
            state["identity_marker_hash"] = marker["seal_hash"]
            def change_id(records):
                next(record for record in records if record["kind"] == "ledger_created")["data"]["ledger_id"] = marker["ledger_id"]
            rewrite_journal(journal_path, change_id)
        atomic_json(lane["path"], seal(state))
    corrupt_lane(lane)
    # These inputs meet A4's local seal/history conditions.
    marker = read_sealed(lane["path"].with_suffix(".json.identity.json"))
    (candidate,) = single_bit_candidates(lane["path"].read_bytes(), marker, lane["lane_plan"]["caps"],
                                        plan_hash=lane["lane_plan"]["seal_hash"])
    _check_attempts(candidate["state"], list(iter_events(journal_path)), lane["lane_plan"]["caps"])
    before = file_hashes(lane["root"])
    if binding:
        assert command(lane, capsys)[0] == 2
    else:
        with pytest.raises(SystemExit) as error:
            main(["repair-ledger", str(lane["root"]), "--study", str(lane["study"]), "--lane", LANE,
                  "--reason", "Single-bit storage corruption.", "--approval-text", APPROVAL])
        assert error.value.code == 2
        capsys.readouterr()
    assert file_hashes(lane["root"]) == before


@pytest.mark.parametrize("step", ["before_retention", "after_retention", "after_replacement",
                                  "before_declaration", "before_completion"])
def test_interrupted_repair_resumes_and_verification_requires_completion(lane, capsys, monkeypatch, step):
    corrupt_lane(lane)
    atomic = ledger_repair.atomic_json
    retain = ledger_repair._retain_bytes
    append = _Journal.append_prepared
    with monkeypatch.context() as patch:
        def interrupted_atomic(path, value):
            if step == "before_completion" and value.get("kind") == REPAIR_KIND and value.get("status") == "complete":
                raise OSError("interrupted before completion")
            atomic(path, value)
            if step == "after_replacement" and path == lane["path"]:
                raise OSError("interrupted after replacement")
        def interrupted_retain(path, raw):
            if step == "before_retention":
                raise OSError("interrupted before retention")
            retain(path, raw)
            if step == "after_retention":
                raise OSError("interrupted after retention")
        def interrupted_append(journal, event):
            if step == "before_declaration":
                raise OSError("interrupted before declaration")
            return append(journal, event)
        patch.setattr(ledger_repair, "atomic_json", interrupted_atomic)
        patch.setattr(ledger_repair, "_retain_bytes", interrupted_retain)
        patch.setattr(_Journal, "append_prepared", interrupted_append)
        assert command(lane, capsys)[0] == 2
    prepared = read_sealed(lane["directory"] / "ledger-repairs" / "repair-1.json")
    assert prepared["status"] == "prepared"
    intended = bytes.fromhex(prepared["declaration_bytes"])
    assert intended.endswith(b"\n")
    with pytest.raises(ValueError, match="prepared but not completed"):
        ledger_repair.verify_root_ledger_repairs(lane["root"], lane["plan"],
                                               live.lane_journals(lane["root"], lane["plan"]))
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 2
    capsys.readouterr()
    code, report = command(lane, capsys)
    assert code == 0 and report["repair"]["status"] == "complete"
    assert report["repair"]["recorded_utc"] == prepared["recorded_utc"]
    assert lane["path"].read_bytes() == lane["original"]
    assert len([record for record in iter_events(lane["directory"] / "journal.jsonl")
                if record["kind"] == "ledger_repaired"]) == 1
    assert (lane["directory"] / "journal.jsonl").read_bytes().endswith(intended)
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 0
    capsys.readouterr()


def interrupted_declaration_write(lane, capsys, monkeypatch, length=17, *, short_return=False):
    """Astra's wrapped stream persists part of the actual declaration inside write()."""
    journal_path = lane["directory"] / "journal.jsonl"
    checkpoint = journal_path.read_bytes()
    initialize = _Journal.__init__

    class InterruptedStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, encoded):
            intended = encoded.encode("utf-8")
            prepared = read_sealed(lane["directory"] / "ledger-repairs" / "repair-1.json")
            assert prepared["status"] == "prepared"
            assert bytes.fromhex(prepared["declaration_bytes"]) == intended
            size = {"half": len(intended) // 2, "without_newline": len(intended) - 1,
                    "full": len(intended)}.get(length, length)
            self.stream.write(intended[:size].decode("utf-8"))
            self.stream.flush()
            os.fsync(self.stream.fileno())
            if short_return:
                return size
            raise OSError("interrupted inside declaration write")

    with monkeypatch.context() as patch:
        def interrupted_init(journal, path, run_id, *, create):
            initialize(journal, path, run_id, create=create)
            if path == journal_path:
                journal._stream = InterruptedStream(journal._stream)
        patch.setattr(_Journal, "__init__", interrupted_init)
        code, report = command(lane, capsys)
        assert code == 2
        assert report["error"] == ("OSError: short journal write" if short_return
                                   else "OSError: interrupted inside declaration write")
    prepared = read_sealed(lane["directory"] / "ledger-repairs" / "repair-1.json")
    intended = bytes.fromhex(prepared["declaration_bytes"])
    tail = journal_path.read_bytes()[len(checkpoint):]
    assert tail and intended.startswith(tail)
    assert journal_path.read_bytes() == checkpoint + tail
    assert lane["path"].read_bytes() == lane["original"]
    return checkpoint, intended


@pytest.mark.parametrize("length,short_return", [(17, True), (17, False), (1, False), ("half", False),
                                               ("without_newline", False), ("full", False)])
def test_interrupted_declaration_write_resumes_verifies_and_exports(lane, capsys, monkeypatch, tmp_path,
                                                                   length, short_return):
    corrupt_lane(lane)
    checkpoint, intended = interrupted_declaration_write(lane, capsys, monkeypatch, length,
                                                         short_return=short_return)
    journal_path = lane["directory"] / "journal.jsonl"
    if length != "full":
        with pytest.raises(ValueError, match="unterminated event"):
            list(iter_events(journal_path))
        with pytest.raises(ValueError, match="unterminated event"):
            _Journal(journal_path, lane["lane_plan"]["seal_hash"], create=False)
    else:
        with pytest.raises(ValueError, match="prepared but not completed"):
            ledger_repair.verify_root_ledger_repairs(lane["root"], lane["plan"],
                                                   live.lane_journals(lane["root"], lane["plan"]))
    before = file_hashes(lane["root"])
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 2
    capsys.readouterr()
    assert main(["export-review", str(lane["root"]), "--study", str(lane["study"]),
                 "--output", str(tmp_path / "refused-export"), "--no-score"]) == 2
    capsys.readouterr()
    assert not (tmp_path / "refused-export").exists()
    assert file_hashes(lane["root"]) == before
    fsync_sizes = []
    fsync = os.fsync
    with monkeypatch.context() as patch:
        def recorded_fsync(fd):
            fsync_sizes.append(os.fstat(fd).st_size)
            fsync(fd)
        patch.setattr(ledger_repair.os, "fsync", recorded_fsync)
        code, report = command(lane, capsys)
    assert code == 0 and report["repair"]["status"] == "complete"
    assert "declaration_bytes" not in report["repair"]
    assert journal_path.read_bytes() == checkpoint + intended
    if length != "full":
        assert fsync_sizes[:2] == [len(checkpoint), len(checkpoint) + len(intended)]
    assert len([event for event in iter_events(journal_path) if event["kind"] == "ledger_repaired"]) == 1
    assert main(["verify", str(lane["root"]), "--study", str(lane["study"])]) == 0
    capsys.readouterr()
    assert main(["export-review", str(lane["root"]), "--study", str(lane["study"]),
                 "--output", str(tmp_path / "export"), "--no-score"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("damage", ["nonprefix", "unrelated_record", "extra_record", "partial_newline",
                                   "changed_complete", "binding", "target", "retained", "checkpoint",
                                   "intended_bytes", "intended_chain", "intended_data"])
def test_interrupted_declaration_refusals_change_nothing(lane, capsys, monkeypatch, damage):
    corrupt_lane(lane)
    checkpoint, intended = interrupted_declaration_write(lane, capsys, monkeypatch)
    journal_path = lane["directory"] / "journal.jsonl"
    record_path = lane["directory"] / "ledger-repairs" / "repair-1.json"
    prepared = read_sealed(record_path)
    if damage == "nonprefix":
        journal_path.write_bytes(checkpoint + flipped(intended[:17], 0, 1))
    elif damage in {"unrelated_record", "extra_record"}:
        journal_path.write_bytes(checkpoint + (intended if damage == "extra_record" else b""))
        journal = _Journal(journal_path, lane["lane_plan"]["seal_hash"], create=False)
        try:
            journal.append("operator_note", note="unrelated record")
        finally:
            journal.close()
    elif damage == "partial_newline":
        journal_path.write_bytes(checkpoint + intended[:17] + b"\n")
    elif damage == "changed_complete":
        event = json.loads(intended)
        event["wall_time"] = "changed"
        event.pop("hash")
        event["hash"] = content_hash(event)
        journal_path.write_bytes(checkpoint + (canonical_json(event) + "\n").encode())
    elif damage == "binding":
        binding = read_sealed(lane["binding"])
        binding.pop("seal_hash")
        binding["commit"] = "0" * 40
        atomic_json(lane["binding"], seal(binding))
    elif damage == "target":
        lane["path"].write_bytes(flipped(lane["original"], 0, 1))
    elif damage == "retained":
        retained = record_path.parent / f"{prepared['corrupt_sha256']}.corrupt"
        retained.write_bytes(flipped(retained.read_bytes(), 0, 1))
    elif damage == "checkpoint":
        journal_path.write_bytes(flipped(checkpoint, 0, 1) + intended[:17])
    else:
        prepared.pop("seal_hash")
        if damage == "intended_bytes":
            prepared["declaration_bytes"] = "invalid"
        else:
            event = json.loads(intended)
            if damage == "intended_chain":
                event["previous_hash"] = "0" * 64
            else:
                event["data"]["repair_hash"] = "0" * 64
            event.pop("hash")
            event["hash"] = content_hash(event)
            prepared["declaration_bytes"] = (canonical_json(event) + "\n").encode().hex()
        atomic_json(record_path, seal(prepared))
    before = file_hashes(lane["root"])
    binding_before = lane["binding"].read_bytes()
    assert command(lane, capsys)[0] == 2
    assert file_hashes(lane["root"]) == before
    assert lane["binding"].read_bytes() == binding_before


@pytest.mark.parametrize("damage", ["retained", "target", "prepared", "journal_tail", "journal_bytes", "binding"])
def test_resumption_checks_all_partial_repair_evidence(lane, capsys, monkeypatch, damage):
    corrupt_lane(lane)
    with monkeypatch.context() as patch:
        def interrupted(journal, event):
            raise OSError("interrupted declaration")
        patch.setattr(_Journal, "append_prepared", interrupted)
        assert command(lane, capsys)[0] == 2
    path = lane["directory"] / "ledger-repairs" / "repair-1.json"
    prepared = read_sealed(path)
    if damage == "retained":
        retained = path.parent / f"{prepared['corrupt_sha256']}.corrupt"
        retained.write_bytes(flipped(retained.read_bytes(), 0, 1))
    elif damage == "target":
        lane["path"].write_bytes(flipped(lane["original"], 0, 1))
    elif damage == "prepared":
        prepared.pop("seal_hash")
        prepared["original_byte"] += 1
        atomic_json(path, seal(prepared))
    elif damage == "journal_tail":
        journal = _Journal(lane["directory"] / "journal.jsonl", lane["lane_plan"]["seal_hash"], create=False)
        try:
            journal.append("operator_note", note="changed since checkpoint")
        finally:
            journal.close()
    elif damage == "journal_bytes":
        journal_path = lane["directory"] / "journal.jsonl"
        journal_path.write_bytes(journal_path.read_bytes().replace(b'{', b'{ ', 1))
    else:
        binding_path = path.parent / f"binding-{prepared['binding_hash']}.json"
        value = read_sealed(binding_path)
        value.pop("seal_hash")
        value["commit"] = "0" * 40
        atomic_json(binding_path, seal(value))
    before = file_hashes(lane["root"])
    assert command(lane, capsys)[0] == 2
    assert file_hashes(lane["root"]) == before


@pytest.mark.parametrize("damage", ["retained", "binding"])
def test_prior_roots_recheck_repairs_in_ledger_prepare_verify_and_export(lane, capsys, tmp_path, damage):
    corrupt_lane(lane)
    assert command(lane, capsys)[0] == 0
    rows, fixtures, source = live.load_study(lane["study"], "smoke")
    ledger = live.prior_root_ledger([lane["root"]], phase="smoke", source=source,
                                  study_directory=lane["study"], bundle=fake_bundle())
    successor_plan = live.build_assignment_plan("smoke", rows, fixtures, caps_record(), revision="successor",
                                                source=source, gate_evidence={}, bundle=fake_bundle(),
                                                consumed_attempts=ledger)
    successor = lane["study"] / "roots" / "successor"
    live.prepare_live_root(successor, successor_plan, study_directory=lane["study"],
                           prior_roots=[lane["root"]], bundle=fake_bundle())
    assert live.verify_live_root(successor, study_directory=lane["study"], prior_roots=[lane["root"]],
                                 bundle=fake_bundle())["consumed_attempt_ledger"]["checked"]
    record = read_sealed(lane["directory"] / "ledger-repairs" / "repair-1.json")
    suffix = f"{record['corrupt_sha256']}.corrupt" if damage == "retained" else f"binding-{record['binding_hash']}.json"
    (lane["directory"] / "ledger-repairs" / suffix).unlink()
    with pytest.raises((OSError, ValueError)):
        live.prior_root_ledger([lane["root"]], phase="smoke", source=source,
                               study_directory=lane["study"], bundle=fake_bundle())
    refused = lane["study"] / "roots" / "refused"
    with pytest.raises((OSError, ValueError)):
        live.prepare_live_root(refused, successor_plan, study_directory=lane["study"],
                               prior_roots=[lane["root"]], bundle=fake_bundle())
    assert not refused.exists()
    with pytest.raises((OSError, ValueError)):
        live.verify_live_root(successor, study_directory=lane["study"], prior_roots=[lane["root"]], bundle=fake_bundle())
    assert main(["export-review", str(successor), "--study", str(lane["study"]), "--prior-root", str(lane["root"]),
                 "--output", str(tmp_path / "export"), "--no-score"]) == 2
    capsys.readouterr()
    assert not (tmp_path / "export").exists()



@pytest.mark.parametrize("location", ["caps_key", "caps_value", "caps_open", "caps_close", "identity_key", "syntax"])
def test_binding_handles_single_bit_damage_without_parsing_corrupt_json(lane, tmp_path, location):
    original = lane["original"]
    offsets = {"caps_key": original.index(b'"caps"') + 2,
               "caps_value": original.index(b'"reserved_tokens_per_trial":') + len(b'"reserved_tokens_per_trial":'),
               "caps_open": original.index(b'"caps":') + len(b'"caps":'),
               "caps_close": original.index(b',"identity_marker_hash"') - 1,
               "identity_key": original.index(b'"identity_marker_hash"') + 2, "syntax": 1}
    offset = offsets[location]
    raw = flipped(original, offset, 1)
    evidence = committed_evidence(tmp_path / "syntax-evidence", raw,
                                  lane["path"].with_suffix(".json.identity.json").read_bytes(),
                                  (lane["directory"] / "journal.jsonl").read_bytes())
    binding = bind_lane(lane, evidence)
    assert (binding["byte_offset"], binding["bit_mask"]) == (offset, 1)
    assert binding["restored_sha256"] == hashlib.sha256(original).hexdigest()


def test_second_repair_resumes_and_rechecks_first_repair(lane, capsys, tmp_path, monkeypatch):
    corrupt_lane(lane)
    assert command(lane, capsys)[0] == 0
    raw = flipped(lane["path"].read_bytes(), len(lane["original"]) - 1, 1)
    evidence = committed_evidence(tmp_path / "second-evidence", raw,
                                  lane["path"].with_suffix(".json.identity.json").read_bytes(),
                                  (lane["directory"] / "journal.jsonl").read_bytes())
    bind_lane(lane, evidence)
    lane["path"].write_bytes(raw)
    with monkeypatch.context() as patch:
        def interrupted(journal, event):
            raise OSError("interrupted second repair")
        patch.setattr(_Journal, "append_prepared", interrupted)
        assert command(lane, capsys)[0] == 2
    assert command(lane, capsys)[0] == 0
    report = live.verify_live_root(lane["root"], study_directory=lane["study"], bundle=fake_bundle())
    assert len(report["ledger_repairs"]) == 2
    first = read_sealed(lane["directory"] / "ledger-repairs" / "repair-1.json")
    (lane["directory"] / "ledger-repairs" / f"{first['corrupt_sha256']}.corrupt").unlink()
    with pytest.raises(OSError):
        live.verify_live_root(lane["root"], study_directory=lane["study"], bundle=fake_bundle())
