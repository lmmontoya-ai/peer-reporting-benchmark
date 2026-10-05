from copy import deepcopy
from dataclasses import replace

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.schemas import scan_wire_specs


def test_wire_tools_are_neutral_and_keep_the_archived_manifest_unchanged():
    bundle = load_bundle()
    archived = bundle.tool_manifest()
    specs = bundle.wire_tool_specs()
    assert scan_wire_specs(specs) == []
    assert len(specs) == 6
    assert bundle.wire_tool_specs_hash == content_hash(specs)
    assert bundle.tool_manifest() == archived
    for descriptor, spec in zip(bundle.tool_descriptors, specs):
        expected = deepcopy(descriptor["inputSchema"])
        schema_id = expected.pop("$id")
        assert schema_id == f"urn:{bundle.schema_version}:{descriptor['name']}:input"
        assert spec == {"type": "function", "name": descriptor["name"],
                        "description": descriptor["description"], "inputSchema": expected}
        assert descriptor["outputSchema"]["$id"] == f"urn:{bundle.schema_version}:{descriptor['name']}:output"
    specs[0]["inputSchema"]["properties"]["limit"]["maximum"] = 1
    assert bundle.tool_manifest() == archived


def test_wire_tools_remove_nested_schema_ids_without_mutating_descriptors():
    bundle = load_bundle()
    descriptors = deepcopy(bundle.tool_descriptors)
    descriptors[0]["inputSchema"]["properties"]["limit"]["$id"] = "urn:nested"
    modified = replace(bundle, tool_descriptors=descriptors)
    assert scan_wire_specs(modified.wire_tool_specs()) == []
    assert descriptors[0]["inputSchema"]["properties"]["limit"]["$id"] == "urn:nested"


@pytest.mark.parametrize("location", ["name", "description", "inputSchema"])
def test_bundle_fails_closed_on_construct_names_in_wire_content(location):
    bundle = load_bundle()
    descriptors = deepcopy(bundle.tool_descriptors)
    if location == "inputSchema":
        descriptors[0][location]["properties"]["limit"]["description"] = "An EXPERIMENT setting."
    else:
        descriptors[0][location] += " EXPERIMENT"
    modified = replace(bundle, tool_descriptors=descriptors)
    for produce in (modified.wire_tool_specs, lambda: modified.wire_tool_specs_hash):
        with pytest.raises(ValueError, match="forbidden wire label: experiment"):
            produce()
