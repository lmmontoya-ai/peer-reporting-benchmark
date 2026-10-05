"""The packaged contract and W07 allocation must agree with the approved document."""

from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting_v11.config import load_protocol, validate_protocol

COUNTS = {"collection": 936, "channel_failure": 72, "low_effort": 120, "calibration": 84, "smoke": 12}
ROOT = Path(__file__).resolve().parents[2]


def test_protocol_copy_is_byte_identical():
    assert (ROOT / "docs/v11/protocol.json").read_bytes() == (
        ROOT / "src/swarm_auth_bench/peer_reporting_v11/protocol.json").read_bytes()


def test_real_protocol_counts():
    result = validate_protocol(load_protocol())
    assert result["counts"] == COUNTS
    assert result["total_trials"] == 1224


@pytest.mark.parametrize("arm", list(COUNTS))
def test_declared_arm_count_mismatch_is_rejected(arm):
    protocol = load_protocol()
    protocol["arms"][arm]["trials"] += 1
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
