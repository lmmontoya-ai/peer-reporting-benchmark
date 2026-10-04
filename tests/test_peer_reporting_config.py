import math

import pytest

from swarm_auth_bench.peer_reporting.config import (
    StudyConfig,
    load_contract,
    validate_caps,
    validate_contract,
)


def caps():
    return {
        "max_trial_wall_seconds": 30, "drain_grace_seconds": 2,
        "max_tool_requests_per_trial": 10, "trial_observed_token_stop_target": 100,
        "reserved_tokens_per_trial": 120, "collection_wall_seconds": 300,
        "collection_observed_token_stop_target": 1000, "max_concurrency": 2,
    }


def test_grid_and_unset_caps_are_explicit():
    assert validate_contract()["collection_assignments"] == 216
    assert StudyConfig().caps is None


def test_revision_three_binds_the_explicitly_approved_ten_percent_limit():
    contract = load_contract()
    assert contract["protocol_id"] == "peer-reporting-p1-v3"
    assert contract["specification_revision"] == 3
    assert contract["pairing"]["maximum_packet_deviation_fraction"] == 0.10
    assert contract["pairing"]["common_packet_byte_target"] == 13461
    contract["pairing"]["maximum_packet_deviation_fraction"] = 0.05
    with pytest.raises(ValueError, match="packet tolerance"):
        validate_contract(contract)


@pytest.mark.parametrize("value", [{"model": "substitute"}, {"seed": True}, {"seed": -1},
                                   {"protocol_id": "old"}, {"collection_revision": "../escape"}])
def test_config_rejects_unrecognized_or_invalid_input(value):
    with pytest.raises((ValueError, TypeError)):
        StudyConfig.from_dict(value)


@pytest.mark.parametrize("key,value", [("max_concurrency", True), ("max_concurrency", 1.5),
                                     ("max_trial_wall_seconds", math.inf),
                                     ("drain_grace_seconds", 0), ("reserved_tokens_per_trial", 99)])
def test_resource_values_do_not_silently_coerce(key, value):
    with pytest.raises(ValueError):
        validate_caps({**caps(), key: value})


def test_caps_cannot_override_sealed_settings():
    config = StudyConfig(caps=caps())
    config.require_matching_caps(caps())
    with pytest.raises(ValueError, match="sealed"):
        config.require_matching_caps({**caps(), "max_concurrency": 1})
    with pytest.raises(ValueError, match="sealed"):
        StudyConfig().require_matching_caps(caps())


def test_phase_caps_freeze_different_totals_with_one_individual_policy():
    smoke = {**caps(), "collection_wall_seconds": 300, "collection_observed_token_stop_target": 1080,
             "max_concurrency": 1}
    collection = {**caps(), "collection_wall_seconds": 7200, "collection_observed_token_stop_target": 25920}
    config = StudyConfig(phase_caps={"smoke": smoke, "collection": collection}, packet_byte_target=13461)
    assert config.caps is None
    assert config.caps_for("smoke") == smoke
    assert config.caps_for("collection") == collection
    config.require_matching_caps(smoke, "smoke")
    config.require_matching_caps(collection)  # The backward-compatible default is collection.
    with pytest.raises(ValueError, match="sealed"):
        config.require_matching_caps(smoke, "collection")
    assert StudyConfig.from_dict(config.to_dict()) == config
    smoke["collection_wall_seconds"] = 1
    selected = config.caps_for("smoke")
    selected["collection_wall_seconds"] = 2
    assert config.caps_for("smoke")["collection_wall_seconds"] == 300


@pytest.mark.parametrize("field", [
    "max_trial_wall_seconds", "drain_grace_seconds", "max_tool_requests_per_trial",
    "trial_observed_token_stop_target", "reserved_tokens_per_trial",
])
def test_phase_caps_cannot_change_an_individual_opportunity(field):
    changed = caps()
    changed[field] += 1
    with pytest.raises(ValueError, match="same individual"):
        StudyConfig(phase_caps={"smoke": caps(), "collection": changed})


@pytest.mark.parametrize("phase_caps", [
    [], {}, {"smoke": caps()}, {"smoke": caps(), "collection": caps(), "probe": caps()},
    {"smoke": None, "collection": caps()},
    {"smoke": {**caps(), "unknown": 1}, "collection": caps()},
])
def test_phase_caps_require_two_complete_declared_budgets(phase_caps):
    with pytest.raises(ValueError):
        StudyConfig(phase_caps=phase_caps)


def test_legacy_shared_caps_and_shapes_remain_supported():
    unset = StudyConfig()
    assert unset.to_dict() == {"protocol_id": unset.protocol_id, "collection_revision": "offline-candidate-1",
                               "seed": 173, "caps": None}
    assert unset.caps_for("smoke") is None
    legacy = StudyConfig(caps=caps())
    assert legacy.caps_for("smoke") == legacy.caps_for("collection") == caps()
    assert set(legacy.to_dict()) == {"protocol_id", "collection_revision", "seed", "caps"}
    with pytest.raises(ValueError, match="mutually exclusive"):
        StudyConfig(caps=caps(), phase_caps={"smoke": caps(), "collection": caps()})


@pytest.mark.parametrize("split", ["compatibility", "main", "", None])
def test_effective_caps_reject_unknown_splits(split):
    with pytest.raises(ValueError, match="split"):
        StudyConfig(caps=caps()).caps_for(split)


@pytest.mark.parametrize("target", [True, False, 13461.0, 0, -1, math.inf, "13461"])
def test_packet_target_is_a_strict_positive_integer(target):
    with pytest.raises(ValueError, match="packet_byte_target"):
        StudyConfig(packet_byte_target=target)
