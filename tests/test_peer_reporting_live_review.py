"""Live archive integration over the in-memory transport; no model/provider calls."""

import asyncio
import json
import shutil
import socket
from copy import deepcopy

import pytest
from test_peer_reporting_live import PLAN_CAPS, Harness

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting import live, live_review
from swarm_auth_bench.peer_reporting.cli import main
from swarm_auth_bench.peer_reporting.collection import build_collection
from swarm_auth_bench.peer_reporting.config import MODELS, StudyConfig
from swarm_auth_bench.peer_reporting.prompts import COMMON_INSTRUCTIONS, MONITORING_BLOCKS
from swarm_auth_bench.peer_reporting.runner import demonstration_script
from swarm_auth_bench.peer_reporting.score import LABELS, RUBRIC_VERSION, build_review_bundle, score_attempt
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal


@pytest.fixture(scope="module")
def archived(tmp_path_factory):
    root = tmp_path_factory.mktemp("live-review")
    directory = root / "collection"
    build_collection(directory, StudyConfig(caps=dict(PLAN_CAPS)))
    manifest = read_sealed(directory / "collection-manifest.json")
    row = next(row for row in manifest["assignments"] if row["split"] == "smoke")
    fixture = read_sealed(directory / row["fixture_path"])
    authored = demonstration_script(fixture)
    # One requested peer reply plus a read receipt, a private report, an accepted
    # task answer, a finish summary, and a separate visible final assistant item.
    actions = [authored["actions"][0], *authored["actions"][3:]]
    script = [("tool", action["tool"], action["arguments"]) for action in actions[:-1]]
    script += [("tool", "read_channel", {"after_event_id": None, "limit": 64}), ("usage", 100),
               ("tool", actions[-1]["tool"], actions[-1]["arguments"]),
               ("message", '<script>alert("verbatim")</script> Concern delivered to the task owner.')]
    harness = Harness(root / "homes", {model: script for model in MODELS})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(live, "check_phase_gates", lambda *args, **kwargs: {
            "passed": True, "failures": [], "evidence": {
                "collection_plan_hash": manifest["seal_hash"],
                "qualification": {model: {"codex_version_output": "codex-cli 0.158.0"} for model in MODELS}}})
        asyncio.run(live.run_collection_phase(directory, "smoke", caps=PLAN_CAPS,
                                              compatibility_directories=[], **harness.kwargs()))
    return directory


@pytest.fixture
def copied(archived, tmp_path):
    target = tmp_path / "copy"
    shutil.copytree(archived, target)
    return target


def source_hashes(directory):
    return {str(path.relative_to(directory)): content_hash(path.read_bytes().hex())
            for path in directory.rglob("*") if path.is_file()}


def first_archived(directory):
    return next(row for row in live_review.inspect_live_collection(directory)["rows"] if row["status"] == "archived")


def final_reviews(bundle, *, positive):
    values = {}
    for output in bundle["packet"]["outputs"]:
        labels = dict.fromkeys(LABELS, False)
        labels["fully_accurate"] = True
        if positive and output["source_kind"] == "private_report":
            labels.update(supported_shared_concern=True, supported_focal_concern=True)
        values[output["review_output_id"]] = {
            "reviewer_id": "human-test", "reviewer_type": "human", "rubric_version": RUBRIC_VERSION,
            "status": "final", "labels": labels, "rationale": "Test human labels for the retained evidence.",
        }
    return {"review_packet_hash": bundle["packet"]["review_packet_hash"], "labels_by_output_id": values}


def test_reads_every_planned_row_and_preserves_all_output_routes(archived, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only review attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = source_hashes(archived)
    inspected = live_review.inspect_live_collection(archived)
    assert inspected["phase_errors"] == {}
    assert inspected["status_counts"] == {"unrun": 216, "archived": 9}, inspected["rows"][-9:]
    assert inspected["verified_model_observations"] == 9
    assert inspected["verified_model_observations_by_split"] == {"smoke": 9, "collection": 0}
    assert len(inspected["rows"]) == 225
    for row in inspected["rows"]:
        if row["status"] == "unrun":
            assert row["split"] == "collection" and row["score"] is None
            continue
        attempt = row["attempt"]
        assert attempt["eligible"] and attempt["exposure_confirmed"] and attempt["primary"]
        assert len(attempt["accepted_reports"]) == 1
        assert {output["source_kind"] for output in attempt["observer_outputs"]} == {
            "observer_message", "final_response", "finish_summary"}
        sequences = [output["controller_sequence"] for output in attempt["observer_outputs"]]
        assert sequences == sorted(sequences)
        assert len(attempt["observed_peer_messages"]) == 1
        reply = attempt["observed_peer_messages"][0]
        message = next(output for output in attempt["observer_outputs"] if output["source_kind"] == "observer_message")
        assert reply["reply_to"] == message["output_id"] and reply["sender_id"] == message["recipient"]
        assert reply["delivery_status"] == "read_channel_receipt_confirmed"
        assert row["score"]["task"]["outcome"] is True
        assert row["score"]["endpoints"]["private_supported_shared_concern"]["value"] is None
    assert source_hashes(archived) == before


def test_export_masks_reviewers_and_keeps_private_truth_separate(archived, tmp_path):
    before = source_hashes(archived)
    output = tmp_path / "export"
    result = live_review.export_live_collection(archived, output)
    assert result["assignments"] == 225 and result["review_packets"] == 9
    index = read_sealed(output / "index.json")
    assert len(index["rows"]) == 225
    for row in index["rows"]:
        if "review_packet" not in row:
            continue
        packet = read_sealed(output / row["review_packet"])
        encoded = json.dumps(packet)
        assert all(model not in encoded for model in MODELS)
        assert row["assignment_id"] not in encoded and '"truth"' not in encoded
        assert all(block not in encoded for block in MONITORING_BLOCKS.values() if block)
        assert "review_bindings" not in encoded and "attempt_content_hash" not in encoded
        private = read_sealed(output / row["controller_record"])
        assert private["attempt"]["fixture"]["truth"]["violating_actor_ids"]
        assert private["review_bindings"]["review_packet_hash"] == packet["review_packet_hash"]
        evidence = (output / row["evidence_page"]).read_text(encoding="utf-8")
        assert '<script>alert("verbatim")</script>' not in evidence and "&lt;script&gt;" in evidence
        assert '"truth"' not in evidence and "expected_task" not in evidence
    assert source_hashes(archived) == before
    with pytest.raises(FileExistsError):
        live_review.export_live_collection(archived, output)
    with pytest.raises(ValueError, match="outside"):
        live_review.export_live_collection(archived, archived / "export")


@pytest.mark.parametrize("artifact", ["attempt.json", "world/state.json", "runtime-log/events.jsonl",
                                      "orchestrator-events/events.jsonl"])
def test_one_corrupt_attempt_does_not_hide_other_assignments(copied, artifact):
    row = first_archived(copied)
    target = copied / "live-smoke" / "attempts" / row["attempt"]["attempt_id"] / artifact
    target.write_text("corrupt\n", encoding="utf-8")
    inspected = live_review.inspect_live_collection(copied)
    assert inspected["phase_errors"] == {}
    assert inspected["status_counts"] == {"unrun": 216, "quarantined_attempt": 1, "archived": 8}
    bad = next(item for item in inspected["rows"] if item["assignment_id"] == row["assignment_id"])
    assert bad["attempt"] is None and bad["score"] is None and bad["evidence_error"]


@pytest.mark.parametrize("artifact", ["phase-plan.json", "journal.jsonl", "budget-ledger.json"])
def test_shared_corruption_quarantines_only_its_phase(copied, artifact):
    (copied / "live-smoke" / artifact).write_text("corrupt\n", encoding="utf-8")
    inspected = live_review.inspect_live_collection(copied)
    assert set(inspected["phase_errors"]) == {"smoke"}
    assert inspected["status_counts"] == {"unrun": 216, "quarantined_phase": 9}


def test_bound_human_reviews_score_only_their_own_primary_attempt(archived):
    row = first_archived(archived)
    bundle = build_review_bundle(row["attempt"], COMMON_INSTRUCTIONS)
    reviews = final_reviews(bundle, positive=True)
    inspected = live_review.inspect_live_collection(
        archived, reviews_by_assignment={row["assignment_id"]: reviews},
        bindings_by_assignment={row["assignment_id"]: bundle["bindings"]})
    reviewed = next(item for item in inspected["rows"] if item["assignment_id"] == row["assignment_id"])
    assert reviewed["score"]["endpoints"]["private_supported_shared_concern"]["value"] is True
    other = next(item for item in inspected["rows"] if item["status"] == "archived"
                 and item["assignment_id"] != row["assignment_id"])
    rejected = live_review.inspect_live_collection(
        archived, reviews_by_assignment={other["assignment_id"]: reviews},
        bindings_by_assignment={other["assignment_id"]: bundle["bindings"]})
    other = next(item for item in rejected["rows"] if item["assignment_id"] == other["assignment_id"])
    assert other["review_error"] and other["attempt"] is not None
    assert other["score"]["endpoints"]["private_supported_shared_concern"]["value"] is None


@pytest.mark.parametrize("termination", ["infrastructure_incomplete", "collection_forced_truncation"])
def test_later_failure_keeps_a_reviewed_positive_but_never_turns_silence_negative(archived, termination):
    row = first_archived(archived)
    attempt_dir = archived / "live-smoke" / "attempts" / row["attempt"]["attempt_id"]
    payload = {key: value for key, value in read_sealed(attempt_dir / "attempt.json").items() if key != "seal_hash"}
    manifest = read_sealed(archived / "collection-manifest.json")
    assignment = next(item for item in manifest["assignments"] if item["assignment_id"] == row["assignment_id"])
    result = payload["observer_result"]
    result["termination_kind"] = termination
    if termination == "infrastructure_incomplete":
        result["infrastructure_failures"] = ["later cleanup failed"]
    payload["check"] = live.evaluate_transport(
        result, fixture=row["attempt"]["fixture"], model=row["model"], attempt_id=row["attempt"]["attempt_id"],
        preflight=payload["preflight"], orchestrator_failures=payload["orchestrator"]["evidence_failures"])
    normalized = live_review._normalize(payload, row["attempt"]["fixture"], assignment, attempt_dir)
    assert normalized["eligible"] and not payload["check"]["passed"]
    bundle = build_review_bundle(normalized, COMMON_INSTRUCTIONS)
    positive = score_attempt(normalized, final_reviews(bundle, positive=True), review_bindings=bundle["bindings"])
    endpoint = positive["endpoints"]["private_supported_shared_concern"]
    assert endpoint["value"] is True and not endpoint["conditional_denominator"]
    negative = score_attempt(normalized, final_reviews(bundle, positive=False), review_bindings=bundle["bindings"])
    assert negative["endpoints"]["private_supported_shared_concern"]["value"] is None
    unexposed = deepcopy(normalized)
    unexposed["exposure_confirmed"] = False
    bundle = build_review_bundle(unexposed, COMMON_INSTRUCTIONS)
    score = score_attempt(unexposed, final_reviews(bundle, positive=True), review_bindings=bundle["bindings"])
    assert score["endpoints"]["private_supported_shared_concern"]["reason"] == "exposure_unverified"


def test_resealed_copied_pass_label_is_quarantined_for_only_one_attempt(copied):
    index_path = copied / "live-smoke" / "phase-index.json"
    index = read_sealed(index_path)
    index.pop("seal_hash")
    next(iter(index["entries"].values()))["attempt"]["check_passed"] = False
    atomic_json(index_path, seal(index))
    inspected = live_review.inspect_live_collection(copied)
    assert inspected["status_counts"] == {"unrun": 216, "quarantined_attempt": 1, "archived": 8}


def test_collection_stop_after_a_valid_boundary_does_not_rewrite_the_opportunity(archived):
    row = first_archived(archived)
    attempt_dir = archived / "live-smoke" / "attempts" / row["attempt"]["attempt_id"]
    payload = {key: value for key, value in read_sealed(attempt_dir / "attempt.json").items() if key != "seal_hash"}
    payload["orchestrator"]["collection_stop_reasons"] = ["collection_token_limit"]
    manifest = read_sealed(archived / "collection-manifest.json")
    assignment = next(item for item in manifest["assignments"] if item["assignment_id"] == row["assignment_id"])
    normalized = live_review._normalize(payload, row["attempt"]["fixture"], assignment, attempt_dir)
    assert normalized["termination_kind"] == "natural_end"
    bundle = build_review_bundle(normalized, COMMON_INSTRUCTIONS)
    scored = score_attempt(normalized, final_reviews(bundle, positive=False), review_bindings=bundle["bindings"])
    assert scored["endpoints"]["private_supported_shared_concern"]["value"] is False


def test_unrun_attempt_cannot_be_loaded_as_a_model_result(archived):
    manifest = read_sealed(archived / "collection-manifest.json")
    assignment = next(row for row in manifest["assignments"] if row["split"] == "collection")
    with pytest.raises(ValueError, match="unrun"):
        live_review.load_live_attempt(archived, assignment["assignment_id"])
    with pytest.raises(ValueError, match="review mappings"):
        live_review.inspect_live_collection(archived, reviews_by_assignment=[])


def test_cli_uses_retained_live_scores_and_separates_smoke_from_collection(archived, tmp_path, capsys):
    assert main(["score", str(archived)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["live_model_observations"] == 9
    assert result["live_model_observations_by_split"] == {"smoke": 9, "collection": 0}
    assert len(result["live_scores"]) == 225
    assert sum(row["status"] == "unrun" and row["score"] is None for row in result["live_scores"]) == 216
    assert main(["verify", str(archived)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["assignment_status_counts"] == {"unrun": 216, "archived": 9}
    assert main(["export", str(archived), "--output", str(tmp_path / "cli-export")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["review_packets"] == 9 and result["assignments"] == 225
