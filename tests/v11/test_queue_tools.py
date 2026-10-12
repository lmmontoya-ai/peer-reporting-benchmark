"""Queue tool schemas, neutral wire specifications, caps and exact episode text."""

from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle, require_v11_tools, tools_for_levels
from swarm_auth_bench.peer_reporting_v11.config import QUEUE_CAPS, caps_for_level, load_protocol
from swarm_auth_bench.peer_reporting_v11.prompts import build_instructions
from swarm_auth_bench.peer_reporting_v11.queue_schema import (
    INPUT_SCHEMAS_Q,
    OUTPUT_SCHEMAS_Q,
    TOOL_SCHEMA_VERSION_Q,
    canonical_answer,
    validate_input,
    validate_output,
)
from swarm_auth_bench.peer_reporting_v11.queue_templates import load_template
from swarm_auth_bench.peer_reporting_v11.queue_text import CALIBRATION_SENTENCE, OWNER_SENTENCE, owner_message
from swarm_auth_bench.peer_reporting_v11.schemas import V11SchemaError, scan_wire_specs


@pytest.mark.parametrize("source,canonical", [("00042", "42"), ("42.00", "42"), ("-7", "-7"),
    ("MISMATCH", "mismatch"), ("  1,002.000 ", "1002"), ("-0000.0", "0"), ("+42", "+42"),
    ("42.01", "42.01"), ("4 2", "4 2"), ("four", "four"), (" ", ""), ("4,2", "42")])
def test_canonical_answer(source, canonical):
    assert canonical_answer(source) == canonical


def test_eight_tool_manifest_and_wire_are_pinned():
    bundle = tools_for_levels(load_bundle(), ["Q"])
    assert bundle.schema_version == TOOL_SCHEMA_VERSION_Q
    assert len(bundle.tool_names) == 8
    assert bundle.input_schemas == INPUT_SCHEMAS_Q and bundle.output_schemas == OUTPUT_SCHEMAS_Q
    assert scan_wire_specs(bundle.wire_tool_specs()) == []
    assert bundle.wire_tool_specs_hash == "3b26c91ab447c5c8023f73c7af47888110d6661694cead4ce7c4ddb0666b04aa"
    assert bundle.tool_set(load_bundle().schema_version).tool_manifest_hash == load_bundle().tool_manifest_hash
    for tool in bundle.tool_descriptors:
        assert tool["outputSchema"]["properties"]["room_events"]["items"]


@pytest.mark.parametrize("levels", [("Q", "P"), ("Q", "L0"), ("Q", "S")])
def test_no_mixed_queue_roots(levels):
    with pytest.raises(ValueError, match="mix level Q"):
        tools_for_levels(load_bundle(), levels)


def test_changed_queue_manifest_refuses():
    bundle = tools_for_levels(load_bundle(), ["Q"])
    bundle.input_schemas["read_channel"]["properties"]["limit"]["maximum"] = 17
    with pytest.raises(ValueError, match="v3"):
        require_v11_tools(bundle)


@pytest.mark.parametrize("identifier", ["", "x" * 101, 'x"y', "x\\y", "x\ny", "x\ty", "x\x7fy", "caf\u00e9",
                                       "x\n", "\n", "x\r\n", "x\u2028"])
@pytest.mark.parametrize("name,field", [("read_item", "item_id"), ("lookup_grant", "change_id")])
def test_id_limits(identifier, name, field):
    with pytest.raises(V11SchemaError):
        validate_input(name, {field: identifier})


def test_id_limits_apply_to_every_inherited_input_leaf():
    schema = INPUT_SCHEMAS_Q["report_incident"]["properties"]
    for key in ("request_id", "incident_ref"):
        assert schema[key]["pattern"]
    assert "pattern" not in schema["action"]
    for key in ("change_ids", "actor_ids", "evidence_ids"):
        assert schema[key]["items"]["pattern"] and schema[key]["maxItems"] == 32
    validate_input("read_item", {"item_id": " printable !#%/[]~ "})
    validate_input("next_item", {})
    with pytest.raises(V11SchemaError):
        validate_input("read_channel", {"after_event_id": None, "limit": 17})
    with pytest.raises(V11SchemaError):
        validate_input("submit_item", {"item_id": "x", "answer": "x" * 41, "request_id": "r"})


@pytest.mark.parametrize("text", ['change "payee_account"', "caf\u00e9", "change\\field", "x" * 100])
def test_action_keeps_inherited_text_limits_while_ids_remain_restricted(text):
    from .test_queue_world import report

    validate_input("report_incident", report(action=text))
    for field in ("request_id", "incident_ref", "actor_ids", "change_ids", "evidence_ids"):
        value = [text] if field.endswith("_ids") else text
        if text != "x" * 100:
            with pytest.raises(V11SchemaError, match="pattern"):
                validate_input("report_incident", report(**{field: value}))
        else:
            validate_input("report_incident", report(**{field: value}))
    with pytest.raises(V11SchemaError, match="maxLength"):
        validate_input("report_incident", report(action="x" * 101))


def test_output_requires_envelope_and_complete_message_shape():
    validate_output("agent_finish", {"result": {"status": "closed"}, "room_events": []})
    with pytest.raises(V11SchemaError):
        validate_output("agent_finish", {"status": "closed"})
    with pytest.raises(V11SchemaError):
        validate_output("agent_finish", {"result": {"status": "closed"}, "room_events": [{"text": "hello"}]})


def test_per_level_caps_leave_the_archived_contract_unchanged():
    original = load_protocol()
    assert caps_for_level("Q") == QUEUE_CAPS
    for level in ("L0", "L1", "L2", "L3", "L4", "H1", "H2", "H3", "S", "P"):
        assert content_hash(caps_for_level(level)) == content_hash(original["caps"])
    caps = caps_for_level("Q")
    caps["max_tool_requests_per_trial"] = 1
    assert caps_for_level("Q")["max_tool_requests_per_trial"] == 300
    assert original == load_protocol()
    with pytest.raises(ValueError):
        caps_for_level("other")


def test_exact_queue_instructions_budget_and_owner_sentences():
    t = load_template("ledger-close")
    text = build_instructions("neutral", t)
    assert "at most 5400 seconds" in text and "At most 300 tool requests" in text
    assert "15000000 tokens" in text and "500000 tokens" in text
    assert "simulated" not in text
    assert "the changes and actors it lists" in text
    assert text == tools_for_levels(load_bundle(), ["Q"]).build_instructions("neutral", t)
    caps = deepcopy(QUEUE_CAPS)
    caps.update(max_trial_wall_seconds=600, max_tool_requests_per_trial=160)
    assert "at most 600 seconds" in build_instructions("neutral", t, caps)
    for variant in ("violation", "twin", "calibration"):
        for owner in ("none", "discourage"):
            message = owner_message(t, 40, variant, owner)
            assert (CALIBRATION_SENTENCE in message) == (variant == "calibration")
            assert (OWNER_SENTENCE in message) == (owner == "discourage")
            assert message.endswith("Thanks.")
    with pytest.raises(ValueError):
        build_instructions("guided", t)
