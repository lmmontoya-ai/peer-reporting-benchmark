"""Hashes captured before any revision 5 implementation changes."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.incidents import load_all_templates
from swarm_auth_bench.peer_reporting_v11.ladder import build_split_fixtures, verify_fixture


@pytest.mark.parametrize("split", ["collection", "calibration", "smoke"])
def test_revision_four_golden_hashes_and_revision_five_existing_levels(split):
    golden = json.loads((Path(__file__).parent / "data/rev4-fixture-hashes.json").read_text(encoding="utf-8"))
    protocol, templates = load_protocol(), load_all_templates()
    retained = build_split_fixtures(protocol, templates, split, specification_revision=4)
    assert {fixture["fixture_id"]: content_hash(fixture) for fixture in retained} == golden[split]
    assert all(fixture["parameters"]["level"] not in ("S", "P") for fixture in retained)
    current = {fixture["fixture_id"]: fixture for fixture in build_split_fixtures(protocol, templates, split)
               if fixture["parameters"]["level"] not in ("S", "P")}
    for fixture in retained:
        assert verify_fixture(fixture, templates[fixture["parameters"]["template_id"]]) == []
        updated = deepcopy(fixture)
        updated["provenance"]["specification_revision"] = 6
        assert canonical_json(updated) == canonical_json(current[fixture["fixture_id"]])


@pytest.mark.parametrize("split,expected_hash", [
    ("collection", "83a4277ac13a7cb8c9adeefdca4ea2e30b56f6c44591f20975eee97ca5fc83de"),
    ("calibration", "eb911a74ce3d9ce74a1ad36ffb2933b5bf6cd1fc915335fe93b75fe919f5efc6"),
    ("smoke", "d885dac24cf251c7d424520ab2c604280d8eeb1d12de68358ca8fc4f65803fd7"),
])
def test_a1_keeps_every_non_social_assignment_and_its_relative_order(split, expected_hash, wp6_study):
    # Captured before A1 from 047fa4c, including extension and hard-probe arms.
    # Absolute planned positions change when S rounds interleave in calibration.
    _, manifest, _ = wp6_study
    rows = [{key: value for key, value in row.items() if key != "planned_order"}
            for row in manifest["assignments"] if row["split"] == split and row["level"] not in ("S", "P")]
    assert content_hash(rows) == expected_hash
