import importlib.util
import json
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.score import build_review_bundle
from swarm_auth_bench.peer_reporting.storage import atomic_json, seal

spec = importlib.util.spec_from_file_location("export_public_peer_results", Path(__file__).parents[1] / "scripts/export_public_peer_results.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def packet():
    attempt = {"fixture": {"public": {}, "packet": json.dumps({"record": "authored evidence"}), "truth": {
        "violating_actor_ids": [], "focal_operation_id": None, "expected_task": {"ready_ids": [], "total_size_kib": 0}}},
        "eligible": True, "exposure_confirmed": True, "termination_kind": "natural_end", "accepted_reports": [],
        "observer_outputs": [{"output_id": "msg_" + "a" * 48, "source_kind": "final_response",
                              "text": "Only authored evidence. <script>alert(1)</script>"}], "task_submissions": []}
    return build_review_bundle(attempt, "Read the authoritative records.")["packet"]


def row():
    value = {key: 0 for key in module.ROW_FIELDS}
    value.update(assignment_id="benchmark-assignment", planned_order=1, split="collection", model="gpt-6-astra",
                 prompt_condition="active", N=4, K=1, block=1, variant="main", status="archived",
                 verified_model_observation=True, eligible=True, exposure_confirmed=True, termination_kind="natural_end",
                 valid_close=True, task_outcome=True, pending_output_count=1, final_human_output_count=0,
                 scorer_version="peer-reporting-score-v4", rubric_version="peer-reporting-rubric-v2",
                 attempt_id="private-attempt", evidence_error=None, review_error=None)
    value["resource"] = {"usage_total_tokens": None, "usage_settlement": "unresolved", "observed_total_tokens": 25,
                         "elapsed_seconds": 2.0, "drain_elapsed_seconds": .2, "tool_request_count": 1,
                         "collection_stop_reasons": [], "cleanup_confirmed": True, "thread_id": "private-thread"}
    value["endpoints"] = {name: {"applicable": True, "value": None, "reason": "review_pending",
                                "true_output_ids": [], "unresolved_output_ids": ["msg_" + "a" * 48],
                                "conditional_denominator": False} for name in module.ENDPOINTS}
    return value


def test_projection_omits_private_maps_errors_and_output_ids_preserves_unknowns():
    source = row()
    source.update(controller_record="controller/private.json", evidence_error="C:/private/host", api_key="private-secret")
    projected = module.project_row(source)
    text = json.dumps(projected)
    for private in ("private-attempt", "private-thread", "C:/private/host", "private-secret", "unresolved_output_ids", "msg_" + "a" * 48):
        assert private not in text
    assert projected["resource"]["usage_total_tokens"] is None
    assert projected["resource"]["usage_settlement"] == "unresolved"
    assert projected["pending_output_count"] == 1
    assert projected["final_human_output_count"] == 0
    assert all(endpoint["value"] is None for endpoint in projected["endpoints"].values())


@pytest.mark.parametrize("secret", [
    "sk-" + "x" * 30, "Bearer " + "x" * 30, "hf_" + "x" * 30, "ghp_" + "x" * 30,
    "github_pat_" + "x" * 30, "reviewer@example.org", "https://private.example.org/path",
    "C:/Users/Private Name/key.json", "/home/private/auth.json", "127.0.0.1:8443", "private.ts.net",
    "thread_" + "x" * 20, "turn_" + "x" * 20, "019bdd64-535e-740e-93f6-6e40aa8bd967",
    '"api_key": "small-secret"', "-----BEGIN PRIVATE KEY-----\nprivate-data\n-----END PRIVATE KEY-----",
])
def test_free_text_sensitive_strings_redacted(secret):
    redactor = module.PublicRedactor()
    redacted = redactor.text(secret)
    assert secret not in redacted
    assert "public-redaction-" in redacted
    assert redactor.text(secret) == redacted


def test_independent_public_output_names_and_consistent_reply_links():
    source = packet()
    review_id = source["outputs"][0]["review_output_id"]
    source["outputs"][0]["text"] += " Related output " + review_id
    source["observed_peer_messages"] = [{"event_id": "ref-" + "b" * 32, "source_kind": "scripted_peer_message",
        "sender_id": "peer-a", "audience": "release-room", "addressed_to": "observer", "recipient": "observer",
        "reply_to": review_id, "sequence": 2, "delivery_status": "stored", "text": "Stored reply."}]
    projected = module.project_evidence(source, "benchmark-assignment")
    text = json.dumps(projected)
    assert review_id not in text and source["review_packet_hash"] not in text
    assert source["review_packet_id"] not in text and "label_template" not in text
    assert projected["outputs"][0]["output_id"] == "public-output-0001"
    assert projected["observed_peer_messages"][0]["reply_to"] == "public-output-0001"
    assert "public-output-0001" in projected["outputs"][0]["text"]


def test_forbidden_controller_field_in_delivered_evidence_fails_closed():
    source = packet()
    source["delivered_packet"] = json.dumps({"records": {"credentials": "private"}})
    with pytest.raises(ValueError, match="forbidden"):
        module.project_evidence(source, "benchmark-assignment")


def test_public_export_allowlist_html_escaping_and_no_source_mutation(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    evidence = source / "evidence"
    (evidence / "reviewer").mkdir(parents=True)
    p = packet()
    name = p["review_packet_id"] + ".json"
    atomic_json(evidence / "reviewer" / name, seal(p))
    atomic_json(evidence / "index.json", seal({"kind": "live_evidence_review_export", "phase_errors": {},
        "rows": [{"assignment_id": "benchmark-assignment", "review_packet": "reviewer/" + name,
                  "controller_record": "controller/private.json"}]}))
    summary = source / "summary.json"
    summary.write_text(json.dumps({"kind": "verified_peer_collection_factual_summary_v1", "phase_errors": {},
                                  "rows": [row()], "verification": {"live_ready": False, "pending": ["old-static-status"]}}), encoding="utf-8")
    before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
    output = tmp_path / "public"
    manifest = module.export_public_results(summary, evidence, output)
    assert manifest["assignment_count"] == 1
    assert not any("reviewer/" in item["path"] or "controller/" in item["path"] for item in manifest["files"])
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
    assert p["review_packet_id"] not in all_text and p["review_packet_hash"] not in all_text
    assert "private-attempt" not in all_text and "old-static-status" not in all_text
    page = (output / "collection-v1/evidence/assignment-0001.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert all(path.read_bytes() == content for path, content in before.items())
    with pytest.raises(ValueError, match="new directory"):
        module.export_public_results(summary, evidence, output)


def test_reviewer_input_path_cannot_escape_into_controller_or_filesystem(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    summary = source / "summary.json"
    summary.write_text(json.dumps({"kind": "verified_peer_collection_factual_summary_v1", "phase_errors": {}, "rows": [row()]}), encoding="utf-8")
    atomic_json(source / "index.json", seal({"kind": "live_evidence_review_export", "phase_errors": {},
        "rows": [{"assignment_id": "benchmark-assignment", "review_packet": "../controller/private.json"}]}))
    with pytest.raises(ValueError, match="masked reviewer packet"):
        module.export_public_results(summary, source, tmp_path / "public")
    assert not (tmp_path / "public").exists()
