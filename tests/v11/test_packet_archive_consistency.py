"""Live packet routing and content come from the registered study and archive."""

import json
import subprocess
import sys
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import review
from swarm_auth_bench.peer_reporting_v11.receipts import check_receipt
from swarm_auth_bench.peer_reporting_v11.review_plan import build_review_plan

from .test_r6_remaining_consistency import native_packet as native_packet
from .test_r6_remaining_consistency import pressure_roots as pressure_roots
from .test_r6_remaining_consistency import primary_bytes
from .test_r6_remaining_consistency import v2_compatibility as v2_compatibility


def cli(*args):
    result = subprocess.run([sys.executable, "-m", "swarm_auth_bench.peer_reporting_v11", *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.stderr == "", result.stderr
    return result.returncode, json.loads(result.stdout)


def test_pressure_export_manifest_copy_and_attempt_consistency(tmp_path, pressure_roots, native_packet):
    study, receipts, *_ = pressure_roots
    root, export, _ = native_packet
    before = primary_bytes(root)
    index = read_sealed(export / "index.json")
    row = next(row for row in index["rows"] if row["status"] == "archived")
    manifest = deepcopy(index["study_manifest"])
    assignment = next(item for item in manifest["assignments"] if item["assignment_id"] == row["assignment_id"])
    assert assignment["level"] == "P"
    assignment.update(level="L0", arm="channel_failure")
    manifest = seal({key: value for key, value in manifest.items() if key != "seal_hash"})
    plan = build_review_plan(manifest)
    index.update(study_manifest=manifest, review_plan_hash=plan["seal_hash"])
    index["study_registration"]["study_manifest_hash"] = manifest["seal_hash"]
    attempt_path = export / row["attempt_path"]
    attempt = {key: value for key, value in read_sealed(attempt_path).items() if key != "seal_hash"}
    assert attempt["execution_kind"] == "live_model"
    next(item for item in attempt["observer_outputs"] if item["source_kind"] == "final_response")["text"] = (
        "I have a concern about the operation.")
    atomic_json(attempt_path, seal(attempt))
    row["attempt_hash"] = content_hash(attempt)
    atomic_json(export / "index.json", seal({key: value for key, value in index.items() if key != "seal_hash"}))
    plan_path = tmp_path / "ordinary-plan.json"
    atomic_json(plan_path, plan)
    output = tmp_path / "inconsistent-packets"
    with pytest.raises(ValueError, match="registered study directory"):
        review.write_review_packets(export, plan, output)
    code, result = cli("review-packets", export, "--plan", plan_path, "--output", output)
    assert code == 2 and "requires --study" in result["error"]
    with pytest.raises(ValueError, match="study manifest differs"):
        review.write_review_packets(export, plan, output, study_directory=study, receipt_directory=receipts)
    code, result = cli("review-packets", export, "--plan", plan_path, "--output", output,
                       "--study", study, "--receipt-directory", receipts)
    assert code == 2 and "study manifest differs" in result["error"]
    with pytest.raises(ValueError, match="authored offline attempt"):
        review.write_review_packets(export, plan, output, replay=True)
    assert not output.exists()
    assert primary_bytes(root) == before
    check_receipt(root, receipts, study_directory=study)


@pytest.mark.parametrize("directory", ["missing", "different"])
def test_pressure_packet_study_directory_consistency(tmp_path, pressure_roots, native_packet, directory):
    study, receipts, *_ = pressure_roots
    _, export, _ = native_packet
    index = read_sealed(export / "index.json")
    selection = index["pressure_review_selection"]
    if directory == "missing":
        del index["pressure_review_study_directory"]
    else:
        index["pressure_review_study_directory"] = str(tmp_path.resolve())
    atomic_json(export / "index.json", seal({key: value for key, value in index.items() if key != "seal_hash"}))
    output = tmp_path / "inconsistent-packets"
    with pytest.raises(ValueError, match="study directory differs"):
        review.write_review_packets(export, selection, output, study_directory=study, receipt_directory=receipts)
    assert not output.exists()


def test_registered_pressure_packets_verify_archive_once(tmp_path, pressure_roots, native_packet, monkeypatch):
    study, receipts, *_ = pressure_roots
    _, export, _ = native_packet
    index = read_sealed(export / "index.json")
    calls = []
    original = review.verify_registered_export_evidence

    def verify(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(review, "verify_registered_export_evidence", verify)
    output = tmp_path / "verified-packets"
    assert review.write_review_packets(export, index["pressure_review_selection"], output,
        study_directory=study / "roots" / "..", receipt_directory=receipts)["packets"] == 1
    assert len(calls) == 1
    cli_output = tmp_path / "cli-verified-packets"
    code, result = cli("review-packets", export, "--output", cli_output, "--study", study,
                       "--receipt-directory", receipts)
    assert code == 0 and result["packets"] == 1
