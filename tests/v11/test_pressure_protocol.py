"""Level P allocation, instruction routing, plan labels and section 6 round properties."""

from collections import Counter, defaultdict
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11.collection import ORDER_VERSION, _verify_pressure_order
from swarm_auth_bench.peer_reporting_v11.config import (
    PRESSURE_ARMS,
    PRESSURE_CONSTANTS,
    PRESSURE_FIELDS,
    PSEL,
    load_protocol,
    pressure_fixture_cells,
    validate_protocol,
)
from swarm_auth_bench.peer_reporting_v11.live import _entry
from swarm_auth_bench.peer_reporting_v11.prompts import build_instructions


def test_pressure_contract_and_calibration_enumeration(wp6_study):
    _, manifest, result = wp6_study
    protocol = load_protocol()
    assert PSEL == protocol["pressure"]["psel"] == "P3"
    assert protocol["pressure"] == PRESSURE_CONSTANTS
    assert {arm: result["counts"][arm] for arm in PRESSURE_ARMS} == dict(zip(PRESSURE_ARMS, (12, 12, 66, 66, 300, 300)))
    assert result["total_trials"] == 2922 and result["fixtures"] == 405
    assert len({row["fixture_id"] for row in manifest["assignments"] if row["level"] == "P"}) == 126
    assert len({row["fixture_id"] for row in manifest["assignments"] if row["level"] == "P"
                and row["template_id"] == "firewall-change"}) == 63


def test_pressure_tables_match_specification():
    pilot = pressure_fixture_cells("pressure_pilot_xhigh")
    keys = ("variant", "prevalence_k", "visibility", "pressure")
    assert [tuple(cell[k] for k in keys) for cell in pilot] == [
        ("violation", 1, "private", "none"), ("violation", 1, "private", "neutral"),
        ("violation", 1, "visible", "neutral"), ("violation", 1, "visible", "P1"),
        ("violation", 1, "visible", "P2"), ("violation", 1, "visible", "P3"),
        ("violation", 1, "private", "P3"), ("violation", 12, "visible", "P3"),
        ("violation", 1, "visible", "P4"), ("twin", 0, "visible", "P3"), ("twin", 0, "visible", "neutral"),
    ]
    gate = pressure_fixture_cells("pressure_gate_xhigh")
    assert [cell["block"] for cell in gate] == [6, 7]
    assert all(tuple(cell[k] for k in keys) == ("violation", 1, "private", "neutral") for cell in gate)
    core = pressure_fixture_cells("pressure_core_xhigh")
    assert Counter(cell["block"] for cell in core) == {block: 10 for block in range(1, 6)}
    for block in range(1, 6):
        assert {tuple(cell[k] for k in keys) for cell in core if cell["block"] == block} == {
            *(("violation", k, visibility, pressure) for k in (1, 12)
              for visibility in ("private", "visible") for pressure in ("neutral", "P3")),
            ("twin", 0, "visible", "neutral"), ("twin", 0, "visible", "P3"),
        }


def test_core_pair_check_rejects_whole_groups_that_split_within_k_pairs(wp6_study, monkeypatch):
    from swarm_auth_bench.peer_reporting_v11 import collection

    _, manifest, _ = wp6_study
    arm = "pressure_core_xhigh"
    rows = [row for row in manifest["assignments"] if row["arm"] == arm]
    original = collection._pressure_group

    def regroup(row, selected_arm):
        group = original(row, selected_arm)
        if row["variant"] == "violation" and row["visibility"] == "private" and row["pressure"] == "neutral":
            return 1 - group
        return group

    # Swap one K=1/K=12 cell. Coverage, sizes, rotation and whole groups still hold.
    monkeypatch.setattr(collection, "_pressure_group", regroup)
    with pytest.raises(ValueError, match="within-K core pairs"):
        collection._interleave(rows, manifest["protocol"], "calibration")


@pytest.mark.parametrize("arm", PRESSURE_ARMS)
def test_groups_rotation_lane_order_and_core_pairs(arm, wp6_study):
    _, manifest, _ = wp6_study
    protocol = manifest["protocol"]
    rows = [row for row in manifest["assignments"] if row["arm"] == arm]
    cells = pressure_fixture_cells(arm)
    worlds = [(template, block) for template in protocol["templates"]["calibration"]
              for block in PRESSURE_CONSTANTS["blocks_per_arm"][arm]]
    gate, pilot = arm in PRESSURE_ARMS[:2], arm in PRESSURE_ARMS[2:4]
    sizes = (1,) if gate else (4, 4, 3) if pilot else (4, 4, 2)
    groups = defaultdict(list)
    assert set(Counter((row["fixture_id"], row["model"]) for row in rows).values()) == {1}
    for row in rows:
        w, m = worlds.index((row["template_id"], row["block"])), protocol["models"].index(row["model"])
        if gate:
            group = 0
        elif pilot:
            n = next(i + 1 for i, cell in enumerate(cells) if all(row[k] == cell[k] for k in
                     ("variant", "prevalence_k", "visibility", "pressure")))
            group = next(g for g, numbers in enumerate(((1, 2, 3, 4), (5, 6, 7, 9), (8, 10, 11))) if n in numbers)
        else:
            group = {1: 0, 12: 1, 0: 2}[row["prevalence_k"]]
        assert group == (row["round"] + m + w) % len(sizes)
        groups[w, m, group].append(row)
    assert len(groups) == len(worlds) * 3 * len(sizes)
    for (_, _, g), group in groups.items():
        assert len(group) == sizes[g]
        assert len({row["round"] for row in group}) == 1
    for round_index in range(len(sizes)):
        for m, model in enumerate(protocol["models"]):
            lane = [row for row in rows if row["model"] == model and row["round"] == round_index]
            keys = [content_hash([ORDER_VERSION, protocol["seeds"]["calibration"], arm,
                                  worlds.index((row["template_id"], row["block"])), m, row["fixture_id"]]) for row in lane]
            assert keys == sorted(keys)
    if arm in PRESSURE_ARMS[4:]:
        for world in worlds:
            for model in protocol["models"]:
                for k in (0, 1, 12):
                    paired = [row for row in rows if (row["template_id"], row["block"]) == world
                              and row["model"] == model and row["prevalence_k"] == k]
                    assert len(paired) == (2 if k == 0 else 4)
                    assert len({row["round"] for row in paired}) == 1


@pytest.mark.parametrize("arm", PRESSURE_ARMS)
def test_study_instructions_and_plan_entry_fields(arm, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    for row in manifest["assignments"]:
        if row["arm"] != arm:
            continue
        assert row["prompt_condition"] == "neutral" and row["world_mode"] == "normal"
        template = wp6_inputs["templates"][row["template_id"]]
        assert row["instructions"] == build_instructions("neutral", template, wp6_inputs["caps_record"]["trial"],
                                                         visibility=row["visibility"], held_submissions=row["pressure"] == "P4")
        # _entry is R6-gen's interface; v2 tool selection and live validation belong to R6-world.
        from swarm_auth_bench.peer_reporting.storage import read_sealed
        stored = read_sealed(wp6_study[0] / manifest["fixtures"][row["fixture_id"]]["path"])
        f = {k: v for k, v in stored.items() if k != "seal_hash"}
        entry = _entry(row, f, row["instructions"])
        assert {k: entry[k] for k in PRESSURE_FIELDS} == {k: row[k] for k in PRESSURE_FIELDS}
        assert all(entry[k] == f["parameters"][k] for k in PRESSURE_FIELDS)


@pytest.mark.parametrize("arm", PRESSURE_ARMS)
@pytest.mark.parametrize("tamper", ("missing", "duplicate", "round", "split_group", "lane_order"))
def test_pressure_order_verifier_rejects_mutations(arm, tamper, wp6_study):
    _, manifest, _ = wp6_study
    rows = deepcopy([row for row in manifest["assignments"] if row["arm"] == arm])
    if tamper == "missing":
        rows.pop()
    elif tamper == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif tamper == "round":
        rows[0]["round"] = 4
    elif tamper == "split_group":
        rows[0]["round"] = (rows[0]["round"] + 1) % 3
    else:
        positions = [i for i,r in enumerate(rows) if r["round"] == rows[0]["round"] and r["model"] == rows[0]["model"]]
        a,b = positions[:2]
        rows[a],rows[b] = rows[b],rows[a]
    with pytest.raises(ValueError, match="P "):
        _verify_pressure_order(rows, manifest["protocol"])


@pytest.mark.parametrize("arm", PRESSURE_ARMS)
@pytest.mark.parametrize("field,value", (("prompts", ["guided"]), ("effort", "medium"),
                                          ("world_mode", "report_store_unavailable"), ("fixtures_per_template", 1),
                                          ("split", "collection"), ("fixtures", "L1 violation")))
def test_pressure_arm_contract_rejects_mutations(arm, field, value):
    protocol = load_protocol()
    protocol["arms"][arm][field] = value
    with pytest.raises(ValueError, match=arm):
        validate_protocol(protocol)
