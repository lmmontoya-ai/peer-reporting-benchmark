"""Reporting tests with authored plans and explicitly synthetic observations only."""
import importlib.util
import json
import socket
from pathlib import Path

import pytest
from test_peer_reporting_live_review import archived  # noqa: F401

from swarm_auth_bench.peer_reporting.collection import build_collection
from swarm_auth_bench.peer_reporting.config import StudyConfig
from swarm_auth_bench.peer_reporting.live_review import inspect_live_collection

SPEC = importlib.util.spec_from_file_location(
    "peer_summary", Path(__file__).parents[1] / "scripts" / "summarize_peer_collection.py")
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


@pytest.fixture
def authored_plan(tmp_path):
    directory = tmp_path / "plan"
    build_collection(directory, StudyConfig())
    return directory


def test_all_assignments_remain_unknown_and_source_is_unchanged(authored_plan, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("summary contacted a provider")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = {str(path): path.read_bytes() for path in authored_plan.rglob("*") if path.is_file()}
    output = tmp_path / "summary"
    assert summary.main([str(authored_plan), "--output", str(output)]) == 0
    data = json.loads((output / "summary.json").read_text())
    assert data["planned_counts"] == {"collection": 216, "smoke": 9}
    assert len(data["rows"]) == 225
    assert all(row["report_attempt_count"] is None for row in data["rows"])
    assert all(row["task_outcome"] is None for row in data["rows"])
    assert all(row["endpoints"]["private_supported_shared_concern"]["value"] is None for row in data["rows"])
    assert {str(path): path.read_bytes() for path in authored_plan.rglob("*") if path.is_file()} == before
    assert len((output / "assignments.csv").read_text().splitlines()) == 226
    with pytest.raises(FileExistsError):
        summary.write_summary(data, output)


def test_synthetic_accepted_report_never_becomes_semantic_success(authored_plan, tmp_path):
    inspected = inspect_live_collection(authored_plan)
    row = next(row for row in inspected["rows"] if row["split"] == "collection" and row["K"] == 1)
    row["status"] = "archived"
    row["attempt"] = {"attempt_id": "synthetic-only", "model_execution_confirmed": True,
                      "eligible": True, "exposure_confirmed": True, "termination_kind": "natural_end",
                      "tool_requests": [{"tool": "functions.report_incident"}, {"tool": "report_incident"}]}
    pending = {"applicable": True, "value": None, "reason": "review_pending", "conditional_denominator": False}
    row["score"] = {"termination": {"valid_close": True}, "mechanical": {"accepted_report_count": 1},
                    "task": {"submission_count": 1, "outcome": True},
                    "review_pending_output_ids": ["synthetic-report"], "outputs": [{"review_status": "pending"}],
                    "endpoints": {name: dict(pending) for name in summary.ENDPOINTS}}
    data = summary.summarize(inspected)
    actual = next(item for item in data["rows"] if item["assignment_id"] == row["assignment_id"])
    assert actual["report_attempt_count"] == 2
    assert actual["accepted_report_count"] == 1
    assert actual["endpoints"]["private_supported_shared_concern"]["value"] is None
    assert actual["resource"] is None
    actual["evidence_error"] = "<script>alert('synthetic')</script>"
    summary.write_summary(data, tmp_path / "render")
    assert "<script>" not in (tmp_path / "render" / "index.html").read_text()


def test_controls_excluded_from_positive_incident_bounds(authored_plan):
    data = summary.summarize(inspect_live_collection(authored_plan))
    for cell in data["cells"]:
        endpoint = cell["endpoints"]["private_supported_shared_concern"]
        if cell["K"] in (0, None):
            assert endpoint["not_applicable"] == cell["planned"]
            assert endpoint["all_assigned"]["planned"] == 0
        else:
            assert endpoint["all_assigned"]["unknown"] == cell["planned"]
            assert endpoint["all_assigned"]["lower_bound"] == 0
            assert endpoint["all_assigned"]["upper_bound"] == 1


def test_source_output_overlap_rejected(authored_plan):
    with pytest.raises(SystemExit):
        summary.main([str(authored_plan), "--output", str(authored_plan / "summary")])


def test_verified_synthetic_runtime_archive_resources(archived):  # noqa: F811
    # The imported fixture uses an in-memory fake transport, never a live provider.
    inspected = inspect_live_collection(archived)
    resources = summary.verified_resources(archived, inspected)
    assert len(resources) == 9
    assert all(value["observed_total_tokens"] == 100 for value in resources.values())
    data = summary.summarize(inspected, resources)
    assert sum(row["resource"] is not None for row in data["rows"]) == 9
    assert all(row["resource"] is None for row in data["rows"] if row["split"] == "collection")
