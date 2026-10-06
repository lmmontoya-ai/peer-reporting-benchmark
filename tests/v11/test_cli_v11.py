"""The v1.1 command line: offline commands, and live commands that refuse without caps and authorization."""

import json

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.cli import LIVE_COMMANDS, main

from .live_fakes import authorization, caps_record, fake_bundle


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    calls = []

    def forbidden(model, effort):
        calls.append((model, effort))
        raise AssertionError("the CLI must not create a runtime in these tests")

    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    monkeypatch.setattr(v11_live, "reviewed_runtime_factory", forbidden)
    return calls


def output(capsys):
    return json.loads(capsys.readouterr().out)


def write(path, value):
    atomic_json(path, value)
    return path


def built(tmp_path, capsys, *, caps=None):
    caps_path = write(tmp_path / "caps.json", caps or caps_record())
    assert main(["build", str(tmp_path / "root"), "--phase", "compatibility", "--caps", str(caps_path),
                 "--revision", "compat-v1"]) == 0
    result = output(capsys)
    assert result["live_model_calls"] == 0 and result["maximum_live_calls"] == 6
    return tmp_path / "root", caps_path, v11_live.read_live_plan(tmp_path / "root")


@pytest.mark.parametrize("command", LIVE_COMMANDS)
def test_live_commands_require_caps_and_authorization_files(command, tmp_path, capsys, fakes):
    for argv in ([command, str(tmp_path)], [command, str(tmp_path), "--caps", "caps.json"],
                 [command, str(tmp_path), "--authorization", "authorization.json"]):
        with pytest.raises(SystemExit) as refused:
            main(argv)
        assert refused.value.code == 2
    assert fakes == []


def test_live_command_refuses_unapproved_or_mismatched_inputs_without_a_runtime(tmp_path, capsys, fakes):
    root, caps_path, plan = built(tmp_path, capsys)
    unapproved = write(tmp_path / "unapproved.json",
                       authorization(plan, root=root, authorization={"status": "proposed", "text": "Not yet."}))
    approved = write(tmp_path / "approved.json", authorization(plan, root=root))
    candidate = write(tmp_path / "candidate.json", caps_record(caps_status="candidate"))
    cases = [
        (["compatibility", str(root), "--caps", str(caps_path), "--authorization", str(unapproved)], "approved"),
        (["compatibility", str(root), "--caps", str(candidate), "--authorization", str(approved)], "frozen"),
        (["smoke", str(root), "--caps", str(caps_path), "--authorization", str(approved)], "not smoke"),
        (["compatibility", str(tmp_path / "missing"), "--caps", str(caps_path), "--authorization", str(approved)],
         "missing or corrupt"),
    ]
    for argv, message in cases:
        assert main(argv) == 2
        result = output(capsys)
        assert message in result["error"] and result["live_model_calls"] is None
    assert fakes == []


def test_offline_validate_build_verify_and_replay(tmp_path, capsys, monkeypatch):
    root, caps_path, plan = built(tmp_path, capsys)
    assert main(["validate", "--caps", str(caps_path)]) == 0
    assert output(capsys)["caps_status"] == "frozen"
    approval = write(tmp_path / "approved.json", authorization(plan, root=root))
    assert main(["validate", "--authorization", str(approval), "--root", str(root)]) == 0
    assert output(capsys)["authorization_phase"] == "compatibility"
    assert main(["verify", str(root)]) == 0
    verified = output(capsys)
    assert verified["live_model_call_starts"] == 0 and len(verified["lanes"]) == 6
    assert main(["replay", "--root", str(root), "--output", str(tmp_path / "replay"), "--no-score"]) == 0
    assert output(capsys)["replays"] == 6
    with monkeypatch.context() as real_matrix:
        real_matrix.setattr(bundle_module, "load_bundle", load_bundle)
        assert main(["replay", "--matrix", "--output", str(tmp_path / "matrix"), "--no-score",
                     "--template", "budget-transfer", "--split", "calibration", "--seed", "7"]) == 0
    matrix = output(capsys)
    social = [row for row in matrix["rows"] if row["level"] == "S"]
    assert matrix["incomplete"] == matrix["live_model_calls"] == 0 and matrix["replays"] == 266
    assert len(social) == 240
    assert {row["template_id"] for row in social} == {"firewall-change", "budget-transfer"}
    assert {row["seed"] for row in social} == {1102}
    assert all(row["score"] is None for row in matrix["rows"])
    assert main(["export-review", str(root), "--output", str(tmp_path / "export"), "--no-score"]) == 2
    assert "engineering checks" in output(capsys)["error"]
    assert main(["build", str(tmp_path / "smoke"), "--phase", "smoke", "--caps", str(caps_path),
                 "--revision", "smoke-v1"]) == 2
    assert "study directory" in output(capsys)["error"]


def test_prior_roots_are_accepted_and_rechecked(tmp_path, capsys, fakes):
    root, caps_path, plan = built(tmp_path, capsys)
    assert main(["verify", str(root), "--prior-root", str(root)]) == 2
    assert "no consumed-attempt ledger or prior roots" in output(capsys)["error"]
    assert main(["build", str(tmp_path / "again"), "--phase", "compatibility", "--caps", str(caps_path),
                 "--revision", "compat-v2", "--prior-root", str(root)]) == 2
    assert "no consumed-attempt ledger or prior roots" in output(capsys)["error"]
    approved = write(tmp_path / "approved.json", authorization(plan, root=root))
    assert main(["compatibility", str(root), "--caps", str(caps_path), "--authorization", str(approved),
                 "--prior-root", str(root)]) == 2
    assert "prior roots" in output(capsys)["error"] and fakes == []
