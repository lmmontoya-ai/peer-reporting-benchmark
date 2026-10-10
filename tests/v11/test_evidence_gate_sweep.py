"""Primary bindings also cover downstream root, repair, and packet gates."""

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live, live_review
from swarm_auth_bench.peer_reporting_v11.ledger_repair import repair_ledger
from swarm_auth_bench.peer_reporting_v11.pilot_decision import validate_core_decision
from swarm_auth_bench.peer_reporting_v11.review import write_review_packets
from swarm_auth_bench.peer_reporting_v11.review_plan import build_review_plan

from .review_root_helpers import register_review_root
from .test_ledger_repair import APPROVAL, LANE, file_hashes
from .test_ledger_repair import lane as lane
from .test_pilot_decision import decision_inputs as decision_inputs
from .test_review_population import core_review_inputs as core_review_inputs
from .test_review_population import export_root, export_rows
from .test_study_assignment_binding import core as core
from .test_study_assignment_binding import core_template as core_template
from .test_study_assignment_binding import reseal_root


@pytest.mark.parametrize("consumer", ["consumption", "prior_ledger", "export"])
def test_downstream_root_gates_refuse_resealed_assignment_labels(core, tmp_path, consumer):
    study, _, _, _, built = core
    root = study / "roots" / "core"
    live.prepare_live_root(root, built, study_directory=study, pilot_decision_verifier=validate_core_decision)
    top = reseal_root(root, study, "arm")
    output = tmp_path / "refused-export"
    with pytest.raises(ValueError, match="assignment arm differs from sealed study"):
        if consumer == "consumption":
            live.consumed_attempts_in_root(root, study_directory=study)
        elif consumer == "prior_ledger":
            live.prior_root_ledger([root], phase=top["phase"], source=top["source"], study_directory=study)
        else:
            live_review.export_live_review(root, output, study_directory=study)
    assert not output.exists()


def test_repair_gate_refuses_primary_assignment_substitution_before_writes(lane):
    top = live.read_live_plan(lane["root"])
    path = lane["directory"] / "phase-plan.json"
    plan = read_sealed(path)
    plan["planned_order"][0]["arm"] = "substituted"
    plan = seal({key: value for key, value in plan.items() if key != "seal_hash"})
    atomic_json(path, plan)
    top["lanes"][0]["plan_hash"] = plan["seal_hash"]
    atomic_json(lane["root"] / live.LIVE_PLAN_FILE, seal({key: value for key, value in top.items() if key != "seal_hash"}))
    before = file_hashes(lane["root"])
    with pytest.raises(ValueError, match="assignment arm differs from sealed study"):
        repair_ledger(lane["root"], study_directory=lane["study"], lane_id=LANE,
                      reason="Repair approved evidence.", approval_text=APPROVAL, binding_path=lane["binding"], evidence_directory=lane["evidence"])
    assert file_hashes(lane["root"]) == before


def test_pressure_export_cannot_use_an_earlier_level_review_plan(tmp_path, monkeypatch, core_review_inputs):
    study, manifest, assignments, attempts = core_review_inputs
    root, plan, _ = register_review_root(study, manifest, assignments, "combined")
    output = tmp_path / "export"
    export_root(monkeypatch, root, plan, study, output, export_rows(assignments, attempts))
    earlier_plan = build_review_plan(manifest)
    index = read_sealed(output / "index.json")
    index["review_plan_hash"] = earlier_plan["seal_hash"]
    atomic_json(output / "index.json", seal({key: value for key, value in index.items() if key != "seal_hash"}))
    packets = tmp_path / "packets"
    with pytest.raises(ValueError, match="level P export requires its pressure review selection"):
        write_review_packets(output, earlier_plan, packets, study_directory=study)
    assert not packets.exists()


@pytest.mark.parametrize("empty", [False, True])
def test_pressure_export_must_retain_the_whole_live_root(decision_inputs, empty):
    from pathlib import Path

    from swarm_auth_bench.peer_reporting_v11.pilot_decision import build_pilot_decision, export_reference
    from swarm_auth_bench.peer_reporting_v11.review_plan import pressure_review_selection

    inputs = decision_inputs[-1]()
    path = Path(inputs["gate_exports"][0]["path"])
    index = read_sealed(path)
    index["rows"] = [] if empty else [row for row in index["rows"] if row["score"]["arm"] == "pressure_gate_low"]
    index["pressure_review_selection"] = pressure_review_selection([row["score"] for row in index["rows"]])
    atomic_json(path, seal({key: value for key, value in index.items() if key != "seal_hash"}))
    reference = export_reference(path)
    with pytest.raises(ValueError, match="omits planned assignments from its registered live root"):
        build_pilot_decision(gate_exports=[reference], pilot_exports=[reference])


@pytest.mark.parametrize("field", ["eligibility", "task", "endpoint"])
def test_human_endpoints_refuse_resealed_structured_scores(tmp_path, field):
    from copy import deepcopy

    from swarm_auth_bench.peer_reporting_v11.review import human_endpoints

    from .review_helpers import bundle_for, fixture, replay, structured

    attempt = replay(tmp_path, fixture(), [])
    bundle = bundle_for(attempt)
    score = deepcopy(structured(attempt))
    if field == "eligibility":
        score["eligibility"]["eligible"] = not score["eligibility"]["eligible"]
    elif field == "task":
        score["task"]["outcome"] = not score["task"]["outcome"]
    else:
        endpoint = next(record for record in score["endpoints"].values() if "value" in record)
        endpoint["value"] = "fabricated"
    with pytest.raises(ValueError, match="structured score differs from the primary attempt"):
        human_endpoints(attempt, bundle["bindings"], {}, structured=score, allow_replay=True)


@pytest.mark.parametrize("consumer", ["consumption", "ledger"])
def test_standalone_ledger_gates_require_the_primary_study(core, consumer):
    study, _, _, _, built = core
    root = study / "roots" / "core"
    live.prepare_live_root(root, built, study_directory=study, pilot_decision_verifier=validate_core_decision)
    with pytest.raises(ValueError, match="assignment binding requires the registered study directory"):
        if consumer == "consumption":
            live.consumed_attempts_in_root(root)
        else:
            live.verify_consumed_ledger(root, live.read_live_plan(root), [])
