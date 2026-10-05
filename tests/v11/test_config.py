"""The packaged contract and W07 allocation must agree with the approved document."""

from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting_v11.config import (
    CALIBRATION_EXTENSION_ARMS,
    load_protocol,
    validate_protocol,
)
from swarm_auth_bench.peer_reporting_v11.incidents import load_template

COUNTS = {"collection": 936, "channel_failure": 72, "low_effort": 120, "calibration": 84,
          "calibration_extension_xhigh": 84, "calibration_extension_low": 42, "smoke": 12}
ROOT = Path(__file__).resolve().parents[2]


def test_protocol_copy_is_byte_identical():
    assert (ROOT / "docs/v11/protocol.json").read_bytes() == (
        ROOT / "src/swarm_auth_bench/peer_reporting_v11/protocol.json").read_bytes()


def test_real_protocol_counts():
    result = validate_protocol(load_protocol())
    assert result["counts"] == COUNTS
    assert result["total_trials"] == 1350


@pytest.mark.parametrize("arm", list(COUNTS))
def test_declared_arm_count_mismatch_is_rejected(arm):
    protocol = load_protocol()
    protocol["arms"][arm]["trials"] += 1
    with pytest.raises(ValueError, match=arm):
        validate_protocol(protocol)


@pytest.mark.parametrize("arm", CALIBRATION_EXTENSION_ARMS)
@pytest.mark.parametrize("key,value", [
    ("split", "collection"), ("prompts", ["unknown"]), ("prompts", ["neutral", "neutral"]),
    ("effort", "medium"), ("fixtures_per_template", 6), ("fixtures", "L1 violation"),
    ("world_mode", "report_store_unavailable"),
])
def test_invalid_calibration_extension_definition_is_rejected(arm, key, value):
    protocol = load_protocol()
    protocol["arms"][arm][key] = value
    with pytest.raises(ValueError, match=arm):
        validate_protocol(protocol)


@pytest.mark.parametrize("change", ["total", "templates", "fixtures", "definition", "smoke_cell"])
def test_counts_are_computed_from_definitions(change):
    protocol = deepcopy(load_protocol())
    if change == "total":
        protocol["total_trials"] += 1
    elif change == "templates":
        protocol["templates"]["collection"].pop()
    elif change == "fixtures":
        protocol["arms"]["calibration"]["fixtures_per_template"] = 13
    elif change == "definition":
        protocol["arms"]["low_effort"]["fixtures"] = "L1 violation"
    else:
        protocol["arms"]["smoke"]["cells"].pop()
    with pytest.raises(ValueError):
        validate_protocol(protocol)


@pytest.mark.parametrize("value", [0, 7, -1, True, 1.5, "3", None])
def test_protocol_rejects_invalid_distractor_counts(value):
    protocol = load_protocol()
    protocol["generator_parameters"]["distractor_grants"] = value
    with pytest.raises(ValueError, match="distractor_grants"):
        validate_protocol(protocol)


@pytest.mark.parametrize("value", [[0, 6], [1, 7]])
def test_protocol_rejects_a_distractor_range_outside_the_contract(value):
    protocol = load_protocol()
    protocol["generator_parameters"]["distractor_grants_allowed_range"] = value
    with pytest.raises(ValueError, match="distractor_grants_allowed_range"):
        validate_protocol(protocol)


@pytest.mark.parametrize("value", [[], ["wrong_digest", "wrong_digest"], ["unknown"],
                                  ["wrong_digest", "unknown"], "wrong_digest", [True], None])
def test_protocol_rejects_invalid_near_miss_types(value):
    protocol = load_protocol()
    protocol["near_miss_types"] = value
    with pytest.raises(ValueError, match="near_miss_types"):
        validate_protocol(protocol)


@pytest.mark.parametrize("companion,pressure", [
    (0, [1, 5]), (1, [1, 5]), (5, [1, 5]), (3, [0, 5]), (3, [1, 1]),
    (32, [1, 5]), (-32, [1, 5]), (3, [1, 32]), (3, [-32, 5]),
    (True, [1, 5]), (3.5, [1, 5]), (3, [True, 5]), (3, [1]), (3, [1, 2, 5]), (3, "1,5"),
])
def test_protocol_rejects_colliding_outside_or_malformed_slot_offsets(companion, pressure):
    protocol = load_protocol()
    protocol["generator_parameters"].update(companion_slot_offset=companion, pressure_slot_offsets=pressure)
    with pytest.raises(ValueError, match="slot offsets"):
        validate_protocol(protocol)


@pytest.mark.parametrize("companion,pressure", [
    (-2, [1, 5]), (-1, [1, 5]), (3, [5, 1]), (3, [-1, 2]),
])
def test_protocol_rejects_reordered_pinned_slot_offsets(companion, pressure):
    protocol = load_protocol()
    protocol["generator_parameters"].update(companion_slot_offset=companion, pressure_slot_offsets=pressure)
    with pytest.raises(ValueError, match="slot offsets require"):
        validate_protocol(protocol)


@pytest.mark.parametrize("direction", ["before", "after"])
def test_protocol_checks_slot_bounds_for_every_template(direction):
    protocol = load_protocol()
    slots = [load_template(template_id)["focal_slot"] for ids in protocol["templates"].values()
             for template_id in ids]
    # These offsets fit some templates but fail at the earliest or latest one.
    valid_slot = max(slots) if direction == "before" else min(slots)
    offset = -min(slots) if direction == "before" else 33 - max(slots)
    assert 1 <= valid_slot + offset <= 32
    protocol["generator_parameters"]["companion_slot_offset"] = offset
    with pytest.raises(ValueError, match="outside 1..32"):
        validate_protocol(protocol)


@pytest.mark.parametrize("count", [1, 2])
def test_small_near_miss_subset_requires_updated_calibration_counts(count):
    protocol = load_protocol()
    protocol["near_miss_types"] = protocol["near_miss_types"][:count]
    with pytest.raises(ValueError, match="calibration"):
        validate_protocol(protocol)
    calibration = protocol["arms"]["calibration"]
    calibration["fixtures_per_template"] = 8 + 2 * count
    calibration["trials"] = 2 * calibration["fixtures_per_template"] * 3
    protocol["total_trials"] = sum(COUNTS.values()) - COUNTS["calibration"] + calibration["trials"]
    assert validate_protocol(protocol)["counts"]["calibration"] == calibration["trials"]
