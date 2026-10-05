"""Export of retained v1.1 lane evidence in the P1 live-review attempt shape. No model or provider call."""

import hashlib
import shutil
from pathlib import Path

import pytest
from live_fakes import fake_bundle, qualified_root, smoke_root

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review, inspect_live_root

P1_ATTEMPT_KEYS = {"adapter_version", "execution_kind", "assignment_id", "attempt_id", "primary", "attempt_number",
                   "fixture", "source_attempt_hash", "source_plan_hash", "eligible", "exposure_confirmed",
                   "termination_kind", "accepted_reports", "observer_outputs", "observed_peer_messages",
                   "task_submissions", "tool_requests", "tool_receipts", "model_execution_confirmed"}
V11_KEYS = {"model", "effort", "world_mode", "prompt_condition", "phase", "lane_id", "split", "arm", "template_id",
            "level", "variant", "near_miss_type", "planned_order", "report_attempts", "usage", "elapsed_seconds"}


@pytest.fixture(scope="module")
def smoked(tmp_path_factory):
    base = tmp_path_factory.mktemp("review")
    compat, _, _, _ = qualified_root(base)
    return {**smoke_root(base, compat), "compat": compat}


def file_hashes(directory):
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(directory).rglob("*")) if path.is_file() and path.suffix != ".lock"}


def test_export_keeps_the_p1_attempt_shape_and_adds_v11_labels(smoked, tmp_path):
    before = file_hashes(smoked["root"])
    scored, summaries = [], []

    def scorer(attempt):
        scored.append(attempt["assignment_id"])
        return {"assignment_id": attempt["assignment_id"], "world_mode": attempt["world_mode"]}

    def summarize(scores):
        summaries.append(len(scores))
        return {"trials": len(scores)}

    result = export_live_review(smoked["root"], tmp_path / "export", bundle=fake_bundle(), scorer=scorer,
                                summarize=summarize)
    assert result["attempts"] == 12 and result["status_counts"] == {"archived": 12}
    assert result["verified_model_observations"] == 12 and len(scored) == 12 and summaries == [12]
    assert file_hashes(smoked["root"]) == before  # the export never edits retained evidence
    index = read_sealed(tmp_path / "export" / "index.json")
    assert index["summary"] == {"trials": 12} and index["source_plan_hash"] == smoked["plan"]["seal_hash"]
    for row in index["rows"]:
        attempt = read_sealed(tmp_path / "export" / row["attempt_path"])
        attempt.pop("seal_hash")
        assert P1_ATTEMPT_KEYS | V11_KEYS == set(attempt)
        assert attempt["effort"] == ("low" if attempt["world_mode"] == "report_store_unavailable" else "xhigh")
        assert attempt["eligible"] and attempt["exposure_confirmed"] and attempt["termination_kind"] == "natural_end"
        assert attempt["usage"]["total_tokens"] == 2000 and attempt["fixture"]["truth"]["variant"] == attempt["variant"]
        assert row["score"] == {"assignment_id": attempt["assignment_id"], "world_mode": attempt["world_mode"]}
        (report,) = attempt["report_attempts"]
        assert report["arguments"]["operation_ids"] and report["admitted"] is True
        if attempt["world_mode"] == "report_store_unavailable":
            assert attempt["accepted_reports"] == [] and report["stored"] is False
            assert report["result"] == {"status": "error", "error": "report_store_unavailable"}
        else:
            assert len(attempt["accepted_reports"]) == 1 and report["stored"] is True
        kinds = {output["source_kind"] for output in attempt["observer_outputs"]}
        assert kinds == {"final_response", "finish_summary"}


def test_one_corrupt_attempt_is_quarantined_without_hiding_the_others(smoked, tmp_path):
    root = Path(shutil.copytree(smoked["root"], tmp_path / "smoke", ignore=shutil.ignore_patterns("*.lock")))
    lane = root / "lanes" / "gpt-6-sol-xhigh"
    attempt = next((lane / "attempts").iterdir()) / "attempt.json"
    payload = read_sealed(attempt)
    payload.pop("seal_hash")
    payload["prompt_condition"] = "discouraged" if payload["prompt_condition"] != "discouraged" else "guided"
    atomic_json(attempt, seal(payload))
    data = inspect_live_root(root, bundle=fake_bundle())
    assert data["status_counts"] == {"archived": 11, "quarantined_attempt": 1}
    quarantined = next(row for row in data["rows"] if row["status"] == "quarantined_attempt")
    assert quarantined["attempt"] is None and quarantined["lane_id"] == "gpt-6-sol-xhigh"


def test_compatibility_roots_are_not_exported_and_exports_stay_outside_the_root(smoked, tmp_path):
    with pytest.raises(ValueError, match="engineering checks"):
        export_live_review(smoked["compat"], tmp_path / "compat-export", bundle=fake_bundle())
    with pytest.raises(ValueError, match="outside"):
        export_live_review(smoked["root"], smoked["root"] / "export", bundle=fake_bundle())
