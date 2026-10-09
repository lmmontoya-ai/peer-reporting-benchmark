"""Level S allocation, paired rounds, and a real offline 72-entry pilot root."""

from collections import Counter, defaultdict
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import ORDER_VERSION, _verify_social_order
from swarm_auth_bench.peer_reporting_v11.config import (
    CALIBRATION_EXTENSION_ARMS,
    HARD_PROBE_ARMS,
    SOCIAL_ARMS,
    SOCIAL_CONSTANTS,
    SOCIAL_FIELDS,
    SOCIAL_HYPOTHESES,
    load_protocol,
    social_fixture_cells,
    validate_protocol,
)
from swarm_auth_bench.peer_reporting_v11.lanes import lane_id, lane_order


def test_social_protocol_constants_counts_and_closed_arms(wp6_study):
    protocol = load_protocol()
    assert protocol["specification_revision"] == 6
    assert protocol["social"] == SOCIAL_CONSTANTS
    report = validate_protocol(protocol)
    expected = dict(zip(SOCIAL_ARMS, (36, 36, 306, 306, 36)))
    assert {arm: report["counts"][arm] for arm in SOCIAL_ARMS} == expected
    assert report["total_trials"] == 2922
    assert set(protocol["closed_arms"]) == {"calibration", *CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS, *SOCIAL_ARMS}
    assert {key: protocol["hypotheses"][key] for key in SOCIAL_HYPOTHESES} == SOCIAL_HYPOTHESES
    assert protocol["hypotheses"]["S4"] == (
        "endorse_8 lowers twin false_alarm relative to mention_8, whose posters are the same peers (A2). "
        "Together with S1 this would be consistent with a criterion shift.")
    _, manifest, result = wp6_study
    assert Counter(row["arm"] for row in manifest["assignments"]) == report["counts"]
    assert result["total_trials"] == 2922 and result["fixtures"] == 405
    assert manifest["live_model_calls"] == 0


@pytest.mark.parametrize("hypothesis", SOCIAL_HYPOTHESES)
@pytest.mark.parametrize("mutation", ("text", "missing"))
def test_social_protocol_validates_exact_hypothesis_texts(hypothesis, mutation):
    protocol = load_protocol()
    if mutation == "missing":
        del protocol["hypotheses"][hypothesis]
    else:
        protocol["hypotheses"][hypothesis] += " Changed."
    with pytest.raises(ValueError, match="exact S1 to S6 texts"):
        validate_protocol(protocol)


def test_social_protocol_rejects_old_s4_comparator():
    protocol = load_protocol()
    protocol["hypotheses"]["S4"] = (
        "endorse_8 lowers twin false_alarm relative to none. "
        "Together with S1 this would be consistent with a criterion shift.")
    with pytest.raises(ValueError, match="exact S1 to S6 texts"):
        validate_protocol(protocol)


@pytest.mark.parametrize("field", list(SOCIAL_CONSTANTS))
def test_protocol_social_constants_are_validated_against_code(field):
    protocol = load_protocol()
    protocol["social"][field] = None
    with pytest.raises(ValueError, match="social constants"):
        validate_protocol(protocol)


def test_social_constant_validation_distinguishes_boolean_from_integer():
    protocol = load_protocol()
    protocol["social"]["blocks_per_arm"]["social_grid_xhigh"] = [True, 2, 3]
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
def test_social_arms_select_exact_cells_and_keep_contrast_groups_whole(arm, wp6_study):
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
    protocol = manifest["protocol"]
    grid = arm in ("social_grid_xhigh", "social_grid_low")
    group_count, group_sizes = (4, (4, 4, 4, 5)) if grid else (3, (2, 2, 2))
    worlds = [(template, "plain" if arm == "social_anchor_xhigh" else "hard", block)
              for template in protocol["templates"]["calibration"]
              for block in protocol["social"]["blocks_per_arm"][arm]]
    assert set(Counter((row["fixture_id"], row["model"]) for row in rows).values()) == {1}
    assert {row["round"] for row in rows} == set(range(group_count))
    paired = defaultdict(list)
    for row in rows:
        w = worlds.index((row["template_id"], row["difficulty"], row["block"]))
        group = ({1: 0, 4: 1, 8: 2}.get(row["prevalence_k"], 3) if grid
                 else {1: 0, 8: 1}.get(row["prevalence_k"], 2))
        m = protocol["models"].index(row["model"])
        assert group == (row["round"] + m + w) % group_count
        paired[w, group, m].append(row)
    assert len(paired) == len(worlds) * group_count * 3
    for (_, group_index, _), group in paired.items():
        assert len(group) == group_sizes[group_index]
        assert len({row["round"] for row in group}) == 1
        assert {row["post_condition"] for row in group} == set(
            protocol["social"]["post_conditions"] if grid else ("none", "endorse_8"))
    for round_index in range(group_count):
        counts = Counter(row["model"] for row in rows if row["round"] == round_index)
        assert set(counts.values()) <= ({25, 26} if grid else {4})
        assert max(counts.values()) - min(counts.values()) <= 1
        for m, model in enumerate(protocol["models"]):
            lane = [row for row in rows if row["model"] == model and row["round"] == round_index]
            keys = [content_hash([ORDER_VERSION, protocol["seeds"]["calibration"], arm,
                                  worlds.index((row["template_id"], row["difficulty"], row["block"])),
                                  m, row["fixture_id"]]) for row in lane]
            assert keys == sorted(keys)


@pytest.mark.parametrize("tamper", ["missing", "duplicate", "round", "split_group", "lane_order"])
def test_study_build_order_verifier_rejects_social_property_mutations(tamper, wp6_study):
    _, manifest, _ = wp6_study
    rows = deepcopy([row for row in manifest["assignments"] if row["arm"] == "social_grid_xhigh"])
    if tamper == "missing":
        rows.pop()
    elif tamper == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif tamper == "round":
        rows[0]["round"] = 4
    elif tamper == "split_group":
        rows[0]["round"] = (rows[0]["round"] + 1) % 4
    else:
        positions = [i for i, row in enumerate(rows) if row["round"] == 0 and row["model"] == rows[0]["model"]]
        a, b = positions[:2]
        rows[a], rows[b] = rows[b], rows[a]
    with pytest.raises(ValueError, match="S "):
        _verify_social_order(rows, manifest["protocol"])


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
    # S arms are closed at revision 6. Archive-level row expansion still preserves S.
    rows, fixtures, source = live.load_study(study, "calibration")
    rows = [row for row in rows if row["arm"] in SOCIAL_ARMS[:2]]
    plan = live.build_assignment_plan("calibration", rows, fixtures, wp6_inputs["caps_record"],
                                      revision="social-pilot-archive-test", source=source,
                                      study_manifest=live.read_study_manifest(study),
                                      gate_evidence={"test_only": True}, selected_arms=sorted(SOCIAL_ARMS[:2]))
    live.prepare_live_root(root, plan, study_directory=study)
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


@pytest.mark.parametrize("arm", (*HARD_PROBE_ARMS, *SOCIAL_ARMS))
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
