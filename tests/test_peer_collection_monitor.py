"""Provisional monitor checks use synthetic local files, never a guest/provider."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def remote_reader(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("peer_monitor", scripts / "monitor_peer_collection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def read(directory):
        result = subprocess.run([sys.executable, "-B", "-c", module.REMOTE_READ, str(directory)],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    return read


def write_phase(root, *, unresolved=True, started=False, old_halt="unknown_usage_hold", journal=(), tail=b""):
    directory = root / "live-collection"
    directory.mkdir()
    entries = {"synthetic-archived": {"status": "archived", "model": "synthetic-model",
                                    "attempt_id": "synthetic-attempt", "attempt": {
                                        "check_passed": False, "failure_reasons": ["synthetic-old-failure"],
                                        "elapsed_seconds": 1}}}
    if started:
        entries["synthetic-current"] = {"status": "started", "model": "synthetic-model",
                                       "attempt_id": "synthetic-current", "attempt": {}}
    (directory / "phase-index.json").write_text(json.dumps({
        "entries": entries, "last_run": {"halted": {"reason": old_halt} if old_halt else None}}),
        encoding="utf-8")
    attempts = {"synthetic-retained": {"status": "unresolved", "observed": 7, "reservation": 100}}
    if not unresolved:
        attempts = {}
    (directory / "budget-ledger.json").write_text(json.dumps({"attempts": attempts, "stop_reason": None}),
                                                  encoding="utf-8")
    raw = b"".join((json.dumps(record) + "\n").encode() for record in journal) + tail
    (directory / "journal.jsonl").write_bytes(raw)


def record(kind, sequence, **data):
    return {"kind": kind, "sequence": sequence, "data": data}


def amendment(sequence=2):
    return record("admission_amendment_applied", sequence, amendment_hash="a" * 64,
                  amendment={"policy": "keep_unresolved_reservations", "max_unresolved_trials": 3})


def test_fresh_phase_uses_opened_journal(remote_reader, tmp_path):
    write_phase(tmp_path, unresolved=False, old_halt=None, journal=[record("run_opened", 0, resumed=False)])
    data = remote_reader(tmp_path)
    row = data["phases"]["collection"]
    assert data["verified_final_evidence"] is False
    assert row["status"] == "in_progress"
    assert row["halt_reason"] is None
    assert row["run_status_source"] == "complete_journal_record"
    assert row["admission_amendment"] is None
    assert data["phases"]["smoke"]["status"] == "unstarted"


def test_closed_unknown_hold_keeps_unknown_and_failed_check(remote_reader, tmp_path):
    write_phase(tmp_path, journal=[record("run_opened", 0),
                                   record("run_closed", 1, halted={"reason": "unknown_usage_hold"})])
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "halted"
    assert row["halt_reason"] == "unknown_usage_hold"
    assert row["unknown_usage_attempts"] == 1
    assert row["reserved_tokens"] == 100
    assert row["known_reported_tokens"] == 7
    assert row["execution_checks_failed"] == 1
    assert row["failed_execution_checks"][0]["reasons"] == ["synthetic-old-failure"]


def test_running_approved_resume_clears_stale_halt(remote_reader, tmp_path):
    write_phase(tmp_path, started=True, journal=[
        record("run_closed", 1, halted={"reason": "unknown_usage_hold"}), amendment(),
        record("run_opened", 3, resumed=True)])
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "in_progress"
    assert row["halt_reason"] is None
    assert row["historical_halt_reason"] == "unknown_usage_hold"
    assert row["latest_run_record_sequence"] == 3
    assert row["current"][0]["attempt_id"] == "synthetic-current"
    assert row["unknown_usage_attempts"] == 1
    assert row["unretained_unknown_usage_attempts"] == 0
    assert row["admission_amendment"]["hash"] == "a" * 64
    assert row["admission_amendment"]["verified_by_monitor"] is False
    assert row["retained_reservations"] == [{"reservation_id": "synthetic-retained", "reserved_tokens": 100}]
    assert row["execution_checks_failed"] == 1


def test_resumed_run_closed_with_new_halt_overrides_old_index(remote_reader, tmp_path):
    write_phase(tmp_path, journal=[amendment(), record("run_opened", 3, resumed=True),
                                   record("run_closed", 4, halted={"reason": "collection_wall_limit"})])
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "halted"
    assert row["halt_reason"] == "collection_wall_limit"
    assert row["historical_halt_reason"] == "unknown_usage_hold"
    assert row["unknown_usage_attempts"] == 1
    assert len(row["retained_reservations"]) == 1


@pytest.mark.parametrize("tail", [b'{"kind":"run_closed",',
                                     json.dumps(record("run_closed", 4, halted={"reason": "fake-close"})).encode()])
def test_unterminated_last_line_is_ignored(remote_reader, tmp_path, tail):
    write_phase(tmp_path, journal=[amendment(), record("run_opened", 3)], tail=tail)
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "in_progress"
    assert row["halt_reason"] is None
    assert row["latest_run_record_sequence"] == 3


def test_unknown_ledger_does_not_invent_a_halt(remote_reader, tmp_path):
    write_phase(tmp_path, old_halt=None, journal=[record("run_opened", 0)])
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["unknown_usage_attempts"] == row["unretained_unknown_usage_attempts"] == 1
    assert row["halt_reason"] is None
    assert row["retained_reservations"] == []


def test_closed_without_halt_does_not_reuse_historical_halt(remote_reader, tmp_path):
    write_phase(tmp_path, journal=[amendment(), record("run_opened", 3), record("run_closed", 4, halted=None)])
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "closed"
    assert row["halt_reason"] is None
    assert row["historical_halt_reason"] == "unknown_usage_hold"


def test_malformed_complete_line_is_not_silently_skipped(remote_reader, tmp_path):
    write_phase(tmp_path, journal=[record("run_opened", 3)], tail=b'{"kind":"run_closed",}\n')
    row = remote_reader(tmp_path)["phases"]["collection"]
    assert row["status"] == "snapshot_unavailable"
    assert row["snapshot_error"]
