"""Offline N100 transport and archive checks; these never call a provider."""
import socket

from swarm_auth_bench.peer_reporting.collection import build_collection
from swarm_auth_bench.peer_reporting.config import StudyConfig
from swarm_auth_bench.peer_reporting.export import export_collection
from swarm_auth_bench.peer_reporting.fixtures import build_fixture
from swarm_auth_bench.peer_reporting.live import build_collection_phase_plan
from swarm_auth_bench.peer_reporting.runner import replay_attempt
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting.world import PeerWorld


def test_n100_pagination_exposure_and_private_report(tmp_path):
    fixture = build_fixture(100, 1)
    world = PeerWorld(fixture, tmp_path / "world", max_tool_requests=16)
    report = {
        "request_id": "private-r1", "incident_ref": None, "actor_ids": [],
        "action": "review", "action_stage": "unknown",
        "authorization_state": "unknown", "certainty": "unverified",
        "evidence_ids": [], "summary": "PRIVATE REPORT SENTINEL",
    }
    assert world.dispatch("report_incident", report)["status"] == "deferred"
    assert not world.confirm_exposure(fixture["packet"] + "changed")
    assert world.confirm_exposure(fixture["packet"])
    assert len(world.reports) == 1
    first = world.dispatch("read_channel", {"after_event_id": None, "limit": 64})
    second = world.dispatch("read_channel", {
        "after_event_id": first["next_after_event_id"], "limit": 64,
    })
    assert len(first["messages"]) == 64 and first["has_more"]
    assert len(second["messages"]) == 36 and not second["has_more"]
    messages = first["messages"] + second["messages"]
    assert len({m["event_id"] for m in messages}) == 100
    assert len({m["sender_id"] for m in messages}) == 100
    assert all("PRIVATE REPORT SENTINEL" not in m["text"] for m in messages)


def test_n100_build_replay_export_and_dynamic_phase_counts(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted network access")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    directory = tmp_path / "study"
    build_collection(directory, StudyConfig())
    manifest = read_sealed(directory / "collection-manifest.json")
    for split, count in (("smoke", 9), ("collection", 54)):
        phase = build_collection_phase_plan(directory, split, {}, {})
        assert phase["maximum_live_calls"] == count
        assert len(phase["planned_order"]) == count
    row = next(r for r in manifest["assignments"] if r["N"] == 100 and r["K"] == 1
               and r["split"] == "collection")
    result = replay_attempt(directory, row["assignment_id"])
    assert result["live_model_calls"] == 0
    assert result["accepted_reports"] == 1
    assert result["score"]["endpoints"]["private_supported_shared_concern"]["value"] is None
    exported = export_collection(directory, tmp_path / "viewer")
    assert exported["status_counts"] == {"unrun": 63}
    assert exported["offline_example_count"] == 1
    assert exported["offline_status_counts"] == {"verified": 1}
    assert exported["live_model_observations"] == 0
