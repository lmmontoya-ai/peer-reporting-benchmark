import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.score import build_review_bundle, review_template

spec = importlib.util.spec_from_file_location("validate_peer_review_upload", Path(__file__).parents[1] / "scripts" / "validate_peer_review_upload.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def bundle():
    attempt = {"fixture": {"public": {}, "packet": "public evidence", "truth": {
        "violating_actor_ids": [], "focal_operation_id": None,
        "expected_task": {"ready_ids": [], "total_size_kib": 0}}},
        "eligible": True, "exposure_confirmed": True, "termination_kind": "natural_end",
        "accepted_reports": [], "observer_outputs": [{"output_id": "raw-final", "source_kind": "final_response", "text": "Done. \u201cEvidence\u201d: 300 KiB."}], "task_submissions": []}
    data = build_review_bundle(attempt, "Read the records.")
    return data["packet"], {"attempt": attempt, "review_bindings": data["bindings"], "review_packet_hash": data["packet"]["review_packet_hash"]}


def test_pending_is_valid_bound_but_unresolved():
    packet, controller = bundle()
    result = module.validate_upload(review_template(packet), packet, controller)
    assert result["private_bindings_verified"]
    assert result["final_human_record_count"] == result["required_labels_resolved_output_count"] == 0


def test_reject_foreign_hash_peer_id_and_changed_evidence():
    packet, _ = bundle()
    review = review_template(packet)
    review["review_packet_hash"] = "different"
    with pytest.raises(ValueError, match="hash mismatch"):
        module.validate_upload(review, packet)
    review = review_template(packet)
    review["labels_by_output_id"]["ref-peer"] = {}
    with pytest.raises(ValueError, match="unknown output"):
        module.validate_upload(review, packet)
    changed = deepcopy(packet)
    changed["common_instructions"] = "Changed"
    with pytest.raises(ValueError, match="content hash"):
        module.validate_upload(review_template(packet), changed)


def test_existing_scorer_label_constraints():
    packet, _ = bundle()
    review = review_template(packet)
    record = next(iter(review["labels_by_output_id"].values()))
    record["labels"]["supported_focal_concern"] = True
    with pytest.raises(ValueError, match="requires supported_shared_concern"):
        module.validate_upload(review, packet)


def test_cli_reads_utf8_packet_and_review(tmp_path, monkeypatch, capsys):
    packet, controller = bundle()
    for name, value in (("packet", packet), ("review", review_template(packet)), ("controller", controller)):
        (tmp_path / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["validate", str(tmp_path / "review.json"), "--packet",
                                    str(tmp_path / "packet.json"), "--controller", str(tmp_path / "controller.json")])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result["private_bindings_verified"]
    assert result["final_human_record_count"] == 0
