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
    assert all(fixture["parameters"]["level"] != "S" for fixture in retained)
    current = {fixture["fixture_id"]: fixture for fixture in build_split_fixtures(protocol, templates, split)
               if fixture["parameters"]["level"] != "S"}
    for fixture in retained:
        assert verify_fixture(fixture, templates[fixture["parameters"]["template_id"]]) == []
        updated = deepcopy(fixture)
        updated["provenance"]["specification_revision"] = 5
        assert canonical_json(updated) == canonical_json(current[fixture["fixture_id"]])
