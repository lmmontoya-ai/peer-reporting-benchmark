"""Level S allocation, paired rounds, and a real offline 72-entry pilot root."""

import json
from collections import Counter, defaultdict
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.config import (
    CALIBRATION_EXTENSION_ARMS,
    HARD_PROBE_ARMS,
    SOCIAL_ARMS,
    SOCIAL_CONSTANTS,
    SOCIAL_FIELDS,
    load_protocol,
    social_fixture_cells,
    validate_protocol,
)
from swarm_auth_bench.peer_reporting_v11.lanes import lane_id, lane_order


def test_social_protocol_constants_counts_and_closed_arms(wp6_study):
    protocol = load_protocol()
    assert protocol["specification_revision"] == 5
    assert protocol["social"] == SOCIAL_CONSTANTS
    report = validate_protocol(protocol)
    expected = dict(zip(SOCIAL_ARMS, (36, 36, 204, 204, 36)))
    assert {arm: report["counts"][arm] for arm in SOCIAL_ARMS} == expected
    assert report["total_trials"] == 1962
    assert set(protocol["closed_arms"]) == {"calibration", *CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS}
    assert {f"S{index}" for index in range(1, 7)} <= set(protocol["hypotheses"])
    _, manifest, result = wp6_study
    assert Counter(row["arm"] for row in manifest["assignments"]) == report["counts"]
    assert result["total_trials"] == 1962 and result["fixtures"] == 245
    assert manifest["live_model_calls"] == 0


@pytest.mark.parametrize("field", list(SOCIAL_CONSTANTS))
def test_protocol_social_constants_are_validated_against_code(field):
    protocol = load_protocol()
    protocol["social"][field] = None
    with pytest.raises(ValueError, match="social constants"):
        validate_protocol(protocol)


def test_social_constant_validation_distinguishes_boolean_from_integer():
    protocol = load_protocol()
    protocol["social"]["blocks_per_arm"]["social_grid_xhigh"] = [True, 2]
    with pytest.raises(ValueError, match="social constants"):
        validate_protocol(protocol)


@pytest.mark.parametrize("arm", SOCIAL_ARMS)
@pytest.mark.parametrize("field,value", [
    ("split", "collection"), ("prompts", ["guided"]), ("prompts", ["neutral", "guided"]),
    ("effort", "medium"), ("world_mode", "report_store_unavailable"),
    ("fixtures_per_template", 5), ("fixtures", "L1 violation"), ("trials", 0),
])
def test_social_arm_contract_rejects_mutations(arm, field, value):
    protocol = load_protocol()
    protocol["arms"][arm][field] = value
    with pytest.raises(ValueError, match=arm):
        validate_protocol(protocol)


@pytest.mark.parametrize("arm", SOCIAL_ARMS)
def test_social_arms_select_exact_cells_and_group_by_posts(arm, wp6_study):
    _, manifest, _ = wp6_study
    rows = [row for row in manifest["assignments"] if row["arm"] == arm]
    definition = manifest["protocol"]["arms"][arm]
    groups = defaultdict(list)
    for row in rows:
        assert row["prompt_condition"] == "neutral"
        assert row["world_mode"] == "normal" and row["effort"] == definition["effort"]
        groups[row["template_id"], row["model"]].append(row)
    for group in groups.values():
        actual = [{key: row[key] for key in ("level", "variant", *SOCIAL_FIELDS)} for row in group]
        assert sorted(actual, key=str) == sorted(social_fixture_cells(arm), key=str)
    paired = defaultdict(list)
    for row in rows:
        paired[row["template_id"], row["difficulty"], row["block"], row["post_condition"], row["model"]].append(row)
    for group in paired.values():
        assert len({row["round"] for row in group}) == 1
        orders = sorted(row["planned_order"] for row in group)
        assert orders == list(range(orders[0], orders[-1] + 1))
        assert sum(row["variant"] == "twin" for row in group) == 1


def test_pilot_grouping_requires_six_three_three_rows_per_model_per_round(wp6_study):
    # v11 section 9's <=2 row spread is infeasible with v12 section 4's four
    # indivisible groups of three rows. Keep the explicit revision 5 grouping.
    _, manifest, _ = wp6_study
    for arm in ("social_pilot_xhigh", "social_pilot_low", "social_anchor_xhigh"):
        for round_index in (0, 3, 6):
            counts = Counter(row["model"] for row in manifest["assignments"]
                             if row["arm"] == arm and row["round"] == round_index)
            assert sorted(counts.values()) == [3, 3, 6]


def test_cli_builds_offline_pilot_root_with_all_four_fields_unchanged(tmp_path, wp6_study, wp6_inputs,
                                                                  monkeypatch, capsys):
    # Only compatibility evidence is authored here. Real study verification,
    # fixture verification, arm selection, entry validation, and root sealing run.
    # This creates no runtime and authorizes no model call.
    monkeypatch.setattr(live, "compatibility_evidence", lambda directories, *, bundle: (
        {lane_id(model, effort): {"test_only": True} for model, effort in lane_order()}, []))
    study, manifest, _ = wp6_study
    caps = tmp_path / "caps.json"
    atomic_json(caps, wp6_inputs["caps_record"])
    root = study / "roots" / "social-pilot-offline-test"
    assert main(["build", str(root), "--phase", "calibration", "--caps", str(caps), "--study", str(study),
                 "--revision", "social-pilot-offline-test", "--arm", "social_pilot_xhigh",
                 "--arm", "social_pilot_low"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["maximum_live_calls"] == 72
    top = live.read_live_plan(root)
    assert top["maximum_live_calls"] == 72
    assert top["selected_arms"] == ["social_pilot_low", "social_pilot_xhigh"]
    entries = [entry for lane in top["lanes"] for entry in
               read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"]]
    assert len(entries) == 72
    by_id = {row["assignment_id"]: row for row in manifest["assignments"]}
    for entry in entries:
        row = by_id[entry["entry_id"]]
        assert {key: entry[key] for key in SOCIAL_FIELDS} == {key: row[key] for key in SOCIAL_FIELDS}
        fixture = live.read_root_fixture(root, top, entry["fixture_id"])
        assert all(entry[key] == fixture["parameters"][key] for key in SOCIAL_FIELDS)
    assert not list(root.glob("lanes/*/attempts/*/attempt.json"))


@pytest.mark.parametrize("arm", HARD_PROBE_ARMS)
def test_consumed_hard_probe_arms_cannot_plan_again(arm, wp6_study, wp6_inputs, monkeypatch):
    monkeypatch.setattr(live, "verify_sealed_study", lambda directory, caps: {"valid": True})
    with pytest.raises(ValueError, match="closed and never run again"):
        live.build_phase_plan("calibration", wp6_inputs["caps_record"], revision="closed-probe",
                              study_directory=wp6_study[0], arms=[arm])


@pytest.mark.parametrize("field", SOCIAL_FIELDS)
def test_live_plan_rejects_social_row_field_mismatches(field, wp6_study, wp6_inputs):
    rows, fixtures, _ = live.load_study(wp6_study[0], "calibration")
    row = deepcopy(next(row for row in rows if row["level"] == "S"))
    row[field] = "changed"
    with pytest.raises(ValueError, match="social row labels"):
        live.validate_assignment_rows("calibration", [row], fixtures, wp6_inputs["caps_record"], load_bundle())
