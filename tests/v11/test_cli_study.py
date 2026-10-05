"""Offline CLI study commands use real modules and never call a provider."""

import json

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST


def test_build_and_verify_study_commands(tmp_path, wp6_inputs, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline CLI must never create a model runtime")

    monkeypatch.setattr(live, "reviewed_runtime_factory", forbidden)
    caps_path = tmp_path / "test-caps.json"
    atomic_json(caps_path, wp6_inputs["caps_record"])
    directory = tmp_path / "study"
    assert main(["build-study", str(directory), "--caps", str(caps_path)]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["total_trials"] == 1446 and built["live_model_calls"] == 0
    assert main(["verify-study", str(directory), "--caps", str(caps_path)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] and verified["seal_hash"] == built["seal_hash"]
    manifest = read_sealed(directory / STUDY_MANIFEST)
    manifest.pop("seal_hash")
    manifest["counts"]["smoke"] = 13
    atomic_json(directory / STUDY_MANIFEST, seal(manifest))
    assert main(["verify-study", str(directory), "--caps", str(caps_path)]) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["valid"] is False and rejected["errors"] and rejected["live_model_calls"] == 0
