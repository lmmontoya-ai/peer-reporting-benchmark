"""Study-derived labels cannot be replaced by resealed row or root labels."""

from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.lanes import lane_id
from swarm_auth_bench.peer_reporting_v11.pilot import CORE_ARMS, PILOT_ARMS
from swarm_auth_bench.peer_reporting_v11.pilot_decision import build_pilot_decision, validate_core_decision

from .live_fakes import Harness, run_phase
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from .test_pilot_decision import core_build, make_decision_inputs, misses


@pytest.fixture(scope="session")
def core_template(tmp_path_factory, wp6_study, wp6_inputs, request):
    patch = pytest.MonkeyPatch()
    request.addfinalizer(patch.undo)
    inputs = make_decision_inputs(tmp_path_factory.mktemp("binding-evidence"), wp6_study, patch)
    manifest, fixtures, _, _, write = inputs
    excluded = lane_id(manifest["protocol"]["models"][0], "xhigh")
    def changes(data, rows):
        targets = [row for row in rows if row["arm"] == "pressure_gate_xhigh"
                   and lane_id(row["model"], row["effort"]) == excluded][:2]
        for row in targets:
            misses(data[row["assignment_id"]])
    decision = build_pilot_decision(**write(changes))
    return manifest, fixtures, decision, core_build(inputs, wp6_inputs, decision)


@pytest.fixture
def core(tmp_path, core_template):
    manifest, fixtures, decision, built = core_template
    study = tmp_path / "study"
    study.mkdir()
    atomic_json(study / live.STUDY_MANIFEST, manifest)
    return study, manifest, fixtures, decision, built


def mutate(row, field, *, entry=False):
    key = "reasoning_effort" if entry and field == "effort" else field
    if field == "arm":
        row[key] = "pressure_pilot_" + (row["reasoning_effort"] if entry else row["effort"])
    elif field == "model":
        row[key] = "gpt-6-sol" if row[key] != "gpt-6-sol" else "gpt-6-luna"
    elif field == "effort":
        row[key] = "low" if row[key] == "xhigh" else "xhigh"
    else:
        row[key] = "changed"


@pytest.mark.parametrize("field", ["arm", "model", "effort"])
def test_build_refuses_assignment_substitution_before_writes(core, wp6_inputs, field):
    study, manifest, fixtures, decision, _ = core
    rows = deepcopy([row for row in manifest["assignments"] if row["arm"] in CORE_ARMS])
    for row in rows if field == "arm" else rows[:1]:
        mutate(row, field)
    before = {p.relative_to(study): p.read_bytes() for p in study.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="assignment .* differs from sealed study"):
        live.build_assignment_plan("calibration", rows, fixtures, wp6_inputs["caps_record"], revision="substituted",
            source={"study_manifest_hash": manifest["seal_hash"]}, gate_evidence={}, study_manifest=manifest,
            selected_arms=sorted(PILOT_ARMS if field == "arm" else CORE_ARMS),
            pilot_decision=None if field == "arm" else decision,
            pilot_decision_verifier=None if field == "arm" else validate_core_decision)
    assert {p.relative_to(study): p.read_bytes() for p in study.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("field", ["arm", "model", "effort", "instructions", "variant", "level", "block",
                                   "planned_order", "round", "fixture_id", "template_id", "prompt_condition"])
def test_prepare_refuses_substitution_before_root_or_registry_writes(core, field):
    study, _, _, _, original = core
    top, lanes, fixtures = deepcopy(original)
    mutate(next(iter(lanes.values()))["planned_order"][0], field, entry=True)
    if field == "arm":
        top["selected_arms"] = sorted(PILOT_ARMS)
        top.pop("pilot_decision")
        top.pop("pilot_decision_hash")
    root = study / "roots" / "refused"
    with pytest.raises(ValueError, match="sealed study|execution lane"):
        live.prepare_live_root(root, (top, lanes, fixtures), study_directory=study,
                               pilot_decision_verifier=validate_core_decision)
    assert not root.exists()
    assert not (study / live.STUDY_REGISTRY).exists()


def reseal_root(root, study, field):
    top = live.read_live_plan(root)
    lane = top["lanes"][0]
    path = root / lane["path"] / "phase-plan.json"
    lane_plan = read_sealed(path)
    mutate(lane_plan["planned_order"][0], field, entry=True)
    lane_plan = seal({k: v for k, v in lane_plan.items() if k != "seal_hash"})
    atomic_json(path, lane_plan)
    lane["plan_hash"] = lane_plan["seal_hash"]
    old = top["seal_hash"]
    if field == "arm":
        top["selected_arms"] = sorted(PILOT_ARMS)
        top.pop("pilot_decision")
        top.pop("pilot_decision_hash")
    top = seal({k: v for k, v in top.items() if k != "seal_hash"})
    atomic_json(root / live.LIVE_PLAN_FILE, top)
    for relative in (live.STUDY_REGISTRY, live.STUDY_REGISTRY + "/" + live.FINALIZED_DIRECTORY):
        path = study / relative / (old + ".json")
        record = read_sealed(path)
        record["plan_hash"] = top["seal_hash"]
        if "live_plan_hash" in record:
            record["live_plan_hash"] = top["seal_hash"]
        atomic_json(path.with_name(top["seal_hash"] + ".json"), seal({k: v for k, v in record.items() if k != "seal_hash"}))
        path.unlink()
    return top


@pytest.mark.parametrize("field", ["arm", "model", "effort"])
async def test_resealed_root_substitution_refused_by_verify_and_run(core, tmp_path, field):
    study, _, _, _, built = core
    root = study / "roots" / "core"
    live.prepare_live_root(root, built, study_directory=study, pilot_decision_verifier=validate_core_decision)
    top = reseal_root(root, study, field)
    with pytest.raises(ValueError, match="sealed study|execution lane"):
        live.verify_live_root(root, study_directory=study)
    before = {p.relative_to(root): content_hash(p.read_bytes().hex()) for p in root.rglob("*") if p.is_file()}
    harness = Harness(tmp_path / "homes", lambda *args: pytest.fail("substituted plan dispatched"))
    with pytest.raises(ValueError, match="sealed study|execution lane"):
        await run_phase(root, top, harness, study_directory=study, bundle=load_bundle())
    assert harness.created == []
    assert {p.relative_to(root): content_hash(p.read_bytes().hex()) for p in root.rglob("*") if p.is_file()} == before


def test_legitimate_five_lane_500_call_root_builds_prepares_and_verifies(core):
    study, _, _, _, built = core
    assert built[0]["maximum_live_calls"] == 500 and len(built[1]) == 5
    root = study / "roots" / "core"
    result = live.prepare_live_root(root, built, study_directory=study, pilot_decision_verifier=validate_core_decision)
    assert result["maximum_live_calls"] == 500
    assert live.verify_live_root(root, study_directory=study)["live_model_call_starts"] == 0
