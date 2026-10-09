"""Core build regressions through python -m, using sealed offline gate evidence."""

import json
import shutil
import subprocess
import sys
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.pilot_decision import build_pilot_decision
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

from .live_fakes import Harness, qualifier_steps, run_phase
from .test_pilot_decision import decision_inputs as decision_inputs
from .test_pilot_decision import misses


def build_cli(root, study, caps, *, decision=None, compatibility=None):
    argv = [sys.executable, "-m", "swarm_auth_bench.peer_reporting_v11", "build", str(root),
            "--phase", "calibration", "--arm", "pressure_core_xhigh", "--revision", "core-cli",
            "--study", str(study), "--caps", str(caps)]
    if decision is not None:
        argv += ["--pilot-decision", str(decision)]
    if compatibility is not None:
        argv += ["--compatibility", str(compatibility)]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.stderr == "", result.stderr
    return result.returncode, json.loads(result.stdout)


async def test_core_build_cli_prepares_valid_sealed_decision(tmp_path, decision_inputs, wp6_inputs, wp6_study):
    record = build_pilot_decision(**decision_inputs[-1]())
    decision = tmp_path / "decision.json"
    atomic_json(decision, record)
    caps = tmp_path / "caps.json"
    atomic_json(caps, wp6_inputs["caps_record"])
    study = tmp_path / "study"
    shutil.copytree(wp6_study[0], study)
    bundle = load_bundle()
    compatibility = tmp_path / "compatibility"
    built = live.build_compatibility_plan(wp6_inputs["caps_record"], revision="core-cli-compat", bundle=bundle,
                                          tool_schema_version=TOOL_SCHEMA_VERSION_P)
    live.prepare_live_root(compatibility, built, bundle=bundle)
    plan = live.read_live_plan(compatibility)
    fixture = next(iter(built[2].values()))
    harness = Harness(tmp_path / "homes", lambda model, effort: qualifier_steps(fixture))
    assert (await run_phase(compatibility, plan, harness, bundle=bundle))["status"] == "complete"
    root = study / "roots" / "core"
    status, result = build_cli(root, study, caps, decision=decision, compatibility=compatibility)
    assert status == 0, result
    assert result["live_model_calls"] == 0 and result["maximum_live_calls"] == 300
    assert result["selected_arms"] == ["pressure_core_xhigh"]
    sealed = read_sealed(root / live.LIVE_PLAN_FILE)
    assert sealed["pilot_decision"] == record and sealed["pilot_decision_hash"] == record["seal_hash"]
    report = live.verify_live_root(root, bundle=bundle, study_directory=study)
    assert sealed["maximum_live_calls"] == 300 and report["live_model_call_starts"] == 0
    assert live.root_registration(study, sealed, directory=root, require_finalized=True)["state"] == "finalized"


@pytest.mark.parametrize("case,message", [("missing", "requires a pilot decision"),
                                         ("stop", "stop pilot decision"),
                                         ("tampered", "differs from recomputed")])
def test_core_build_cli_controls_decision_refusals(tmp_path, decision_inputs, wp6_inputs, wp6_study, case, message):
    caps = tmp_path / "caps.json"
    atomic_json(caps, wp6_inputs["caps_record"])
    decision = None
    if case != "missing":
        def failed_gate(data, rows):
            for row in rows:
                misses(data[row["assignment_id"]])
        record = build_pilot_decision(**decision_inputs[-1](failed_gate if case == "stop" else None))
        if case == "tampered":
            record = deepcopy(record)
            record["pooled_gate"]["hits"] -= 1
            record = seal({key: value for key, value in record.items() if key != "seal_hash"})
        decision = tmp_path / "decision.json"
        atomic_json(decision, record)
    study = wp6_study[0]
    root = study / "roots" / ("refused-cli-" + case)
    registrations = list((study / live.STUDY_REGISTRY).glob("*.json"))
    status, result = build_cli(root, study, caps, decision=decision)
    assert status == 2 and message in result["error"]
    assert result["live_model_calls"] == 0
    assert not root.exists()
    assert list((study / live.STUDY_REGISTRY).glob("*.json")) == registrations
