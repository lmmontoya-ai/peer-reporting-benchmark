"""P-A4 receipts use exact primary-file hashes and real local Git commits."""

import json
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.receipts import check_receipt, receipt_bytes, write_receipt
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

from .live_fakes import caps_record, compat_root
from .receipt_helpers import commit_receipt, git


@pytest.fixture
def receipt_root(tmp_path):
    root = tmp_path / "compatibility"
    built = live.build_compatibility_plan(
        caps_record(),
        revision="receipt-test",
        bundle=load_bundle(),
        tool_schema_version=TOOL_SCHEMA_VERSION_P,
    )
    live.prepare_live_root(root, built)
    plan = live.read_live_plan(root)
    lane = root / plan["lanes"][0]["path"]
    attempt = lane / "attempts" / "archived" / "attempt.json"
    attempt.parent.mkdir(parents=True)
    atomic_json(attempt, seal({"offline_evidence": "archived"}))
    return root, lane, attempt, tmp_path / "receipts"


def test_valid_committed_receipt_is_deterministic_and_holds_hashes_only(receipt_root):
    root, lane, attempt, directory = receipt_root
    commit_receipt(root, directory)
    check_receipt(root, directory)
    raw = receipt_bytes(root)
    assert raw == receipt_bytes(root)
    record = json.loads(raw)
    assert set(record) == {"kind", "files", "seal_hash"}
    assert "live-plan.json" in record["files"]
    assert attempt.relative_to(root).as_posix() in record["files"]
    assert all(len(value) == 64 for value in record["files"].values())
    assert b"\r" not in raw


@pytest.mark.parametrize("state", ["missing", "uncommitted", "disk_mismatch", "committed_mismatch"])
def test_receipt_requires_exact_disk_and_head_content(receipt_root, state):
    root, lane, attempt, directory = receipt_root
    if state == "missing":
        directory.mkdir()
        git(directory, "init", "-q")
    elif state == "uncommitted":
        write_receipt(root, directory)
        git(directory, "init", "-q")
        git(directory, "add", ".")  # Staging is not a commit.
    else:
        commit_receipt(root, directory)
        path = directory / (live.read_live_plan(root)["seal_hash"] + ".json")
        path.write_bytes(path.read_bytes() + b" ")
        if state == "committed_mismatch":
            git(directory, "add", ".")
            git(directory, "commit", "-qm", "Retain different receipt bytes.")
    with pytest.raises(ValueError, match="receipt mismatch"):
        check_receipt(root, directory)


@pytest.mark.parametrize("kind", ["attempt", "journal"])
@pytest.mark.parametrize("change", ["changed", "added", "missing"])
def test_primary_file_changes_after_commit_refuse(receipt_root, kind, change):
    root, lane, attempt, directory = receipt_root
    commit_receipt(root, directory)
    path = attempt if kind == "attempt" else lane / "journal.jsonl"
    if change == "changed":
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "missing":
        path.unlink()
    elif kind == "attempt":
        extra = lane / "attempts" / "added" / "attempt.json"
        extra.parent.mkdir()
        extra.write_bytes(b"{}\n")
    else:
        extra = lane / "added" / "journal.jsonl"
        extra.parent.mkdir()
        extra.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="receipt mismatch"):
        check_receipt(root, directory)


def test_earlier_level_root_needs_no_receipt(tmp_path):
    root, _ = compat_root(tmp_path / "earlier")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    check_receipt(root, None)
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_cli_writes_an_offline_receipt_without_committing(receipt_root, capsys):
    root, lane, attempt, directory = receipt_root
    assert main(["root-receipt", str(root), "--receipt-directory", str(directory)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["live_model_calls"] == 0
    assert Path(result["receipt"]).read_bytes() == receipt_bytes(root)
    with pytest.raises(ValueError, match="receipt mismatch"):
        check_receipt(root, directory)
