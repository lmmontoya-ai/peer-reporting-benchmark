"""Independent N100 allocation expectations and deliberately resealed invalid plans."""
import json
from collections import Counter
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.collection import build_collection, verify_collection
from swarm_auth_bench.peer_reporting.config import StudyConfig, load_contract, validate_contract
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal


def candidate():
    common = {"max_trial_wall_seconds": 180, "drain_grace_seconds": 10, "max_tool_requests_per_trial": 16,
              "trial_observed_token_stop_target": 150000, "reserved_tokens_per_trial": 190000,
              "max_concurrency": 1}
    return {"protocol_id": "peer-reporting-n100-v1", "collection_revision": "test-n100-1", "seed": 173,
            "packet_byte_target": 31735, "phase_caps": {
                "smoke": {**common, "collection_wall_seconds": 1965,
                          "collection_observed_token_stop_target": 1710000},
                "collection": {**common, "collection_wall_seconds": 11190,
                               "collection_observed_token_stop_target": 10260000}}}


@pytest.fixture
def plan(tmp_path):
    directory = tmp_path / "plan"
    build_collection(directory, StudyConfig.from_dict(candidate()))
    return directory


def plain(value):
    return {key: item for key, item in value.items() if key != "seal_hash"}


def reseal_plan(directory, edit):
    manifest = plain(read_sealed(directory / "collection-manifest.json"))
    original_ids = [row["assignment_id"] for row in manifest["assignments"]]
    edit(manifest)
    sealed = seal(manifest)
    atomic_json(directory / "collection-manifest.json", sealed)
    index = plain(read_sealed(directory / "collection-index.json"))
    index["plan_hash"] = sealed["seal_hash"]
    for previous, row in zip(original_ids, manifest["assignments"]):
        if previous != row["assignment_id"]:
            index["assignments"][row["assignment_id"]] = index["assignments"].pop(previous)
    atomic_json(directory / "collection-index.json", seal(index))


def test_literal_matrix_separate_smoke_and_round_coverage(plan):
    data = read_sealed(plan / "collection-manifest.json")
    expected = Counter()
    for n, k in [(16, 1), (100, 0), (100, 1)]:
        for block in [1, 2]:
            for model in ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]:
                for prompt in ["none", "soft", "active"]:
                    expected[("collection", n, k, block, model, prompt)] += 1
    for model in ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]:
        for prompt in ["none", "soft", "active"]:
            expected[("smoke", 100, 1, 1, model, prompt)] += 1
    rows = data["assignments"]
    assert len(rows) == len({row["assignment_id"] for row in rows}) == 63
    assert Counter((row["split"], row["N"], row["K"], row["block"], row["model"], row["prompt_condition"])
                   for row in rows) == expected
    assert {row["variant"] for row in rows} == {"main"}
    collection = [row for row in rows if row["split"] == "collection"]
    assert sum(row["K"] == 1 for row in collection) == 36
    assert sum(row["K"] == 0 for row in collection) == 18
    smoke = [row for row in rows if row["split"] == "smoke"]
    assert {row["fixture_id"] for row in collection}.isdisjoint(row["fixture_id"] for row in smoke)
    assert len(data["fixtures"]) == 7
    for round_index in range(9):
        group = collection[round_index * 6:(round_index + 1) * 6]
        assert len({row["fixture_id"] for row in group}) == 6
        assert {row["round"] for row in group} == {round_index}
    assert Counter(item["comparison"] for item in data["comparison_audits"]) == {
        "fixed_K1_N16_N100": 2, "N100_K0_K1_false_alert_control": 2}
    assert verify_collection(plan)["counts"] == {"collection": 54, "smoke": 9}


@pytest.mark.parametrize("field,value", [("max_trial_wall_seconds", float("inf")),
                                        ("trial_observed_token_stop_target", float("nan")),
                                        ("reserved_tokens_per_trial", 190000.0),
                                        ("max_concurrency", True), ("max_trial_wall_seconds", 179),
                                        ("collection_wall_seconds", 11191)])
def test_nonfinite_wrong_types_and_cap_drift_rejected(field, value):
    config = candidate()
    config["phase_caps"]["collection"][field] = value
    with pytest.raises(ValueError):
        StudyConfig.from_dict(config)


def test_no_configurable_arbitrary_grid_or_legacy_protocol():
    assert StudyConfig().caps_for("smoke") is None
    config = candidate()
    config["protocol_id"] = "peer-reporting-p1-v3"
    with pytest.raises(ValueError, match="protocol"):
        StudyConfig.from_dict(config)
    config = candidate()
    config["cells"] = [[100, 50]]
    with pytest.raises(ValueError, match="unknown configuration fields"):
        StudyConfig.from_dict(config)
    config = candidate()
    config["caps"] = config.pop("phase_caps")["collection"]
    with pytest.raises(ValueError, match="separate frozen phase_caps"):
        StudyConfig.from_dict(config)


@pytest.mark.parametrize("mutation", ["count", "grid", "smoke", "posts", "slots", "diagnostics", "caps"])
def test_stale_or_changed_contract_rejected(mutation):
    contract = deepcopy(load_contract())
    if mutation == "count":
        contract["expected_counts"]["collection_assignments"] = 216
    elif mutation == "grid":
        contract["main_grid"][-1]["K"].append(50)
    elif mutation == "smoke":
        contract["smoke"]["N"] = 16
    elif mutation == "posts":
        contract["world"]["initial_peer_posts"] = 32
    elif mutation == "slots":
        contract["pairing"]["focal_post_slot_one_based_by_block"]["2"] = 16
    elif mutation == "caps":
        contract["resource_contract"]["candidate_phase_caps"]["smoke"]["reserved_tokens_per_trial"] = 95000
    else:
        contract["diagnostic_controls"] = [{"variant": "unverified_accusation"}]
    with pytest.raises(ValueError):
        validate_contract(contract)


@pytest.mark.parametrize("mutation", ["missing", "duplicated", "wrong_cell", "wrong_split", "order"])
def test_resealed_allocation_mutants_rejected(plan, mutation):
    def edit(manifest):
        rows = manifest["assignments"]
        if mutation == "missing":
            rows.pop()
        elif mutation == "duplicated":
            rows[-1] = deepcopy(rows[-2])
        elif mutation == "wrong_cell":
            rows[0]["K"] = 2
        elif mutation == "wrong_split":
            rows[-1]["split"] = "collection"
        else:
            rows[0], rows[1] = rows[1], rows[0]
    reseal_plan(plan, edit)
    with pytest.raises(ValueError):
        verify_collection(plan)


def test_resealed_literal_packet_binding_mutant_rejected(plan):
    def edit(manifest):
        row = manifest["assignments"][0]
        row["messages"][1]["content"] = "Forged replacement packet, not the frozen fixture."
        row["input_identity"]["instructions_and_roles_hash"] = content_hash(row["messages"])
        row["assignment_id"] = "pa-" + content_hash(row["input_identity"])
    reseal_plan(plan, edit)
    with pytest.raises(ValueError, match="input binding"):
        verify_collection(plan)


def test_resealed_comparison_label_rejected(plan):
    reseal_plan(plan, lambda data: data["comparison_audits"][0].update(comparison="fixed_fraction_0.25"))
    with pytest.raises(ValueError, match="comparison audits"):
        verify_collection(plan)


def test_plan_sources_do_not_claim_n100_live_findings(plan):
    data = read_sealed(plan / "collection-manifest.json")
    assert data["live_ready"] is False
    assert "human_review_sample_plan" in data["pending"]
    contract = data["contract"]
    assert contract["scoring"]["human_review_status"] == "pending_no_final_human_labels"
    assert contract["candidate_implementation"]["N100_benchmark_collection_calls"] == 0
    assert json.loads((plan / "collection-index.json").read_text())["assignments"]


def test_byte_target_and_postconstruction_cap_mutation_rejected(tmp_path):
    values = candidate()
    values["packet_byte_target"] = 13461
    with pytest.raises(ValueError, match="frozen N100 target"):
        StudyConfig.from_dict(values)
    config = StudyConfig.from_dict(candidate())
    config.phase_caps["collection"]["reserved_tokens_per_trial"] = 1
    with pytest.raises(ValueError):
        build_collection(tmp_path / "must-not-exist", config)
    assert not (tmp_path / "must-not-exist").exists()
