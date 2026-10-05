from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting import schemas as p1
from swarm_auth_bench.peer_reporting_v11 import TOOL_SCHEMA_VERSION
from swarm_auth_bench.peer_reporting_v11.schemas import (
    ERROR_CODES,
    ERROR_RULES,
    ERROR_SCHEMA,
    INPUT_SCHEMAS,
    OUTPUT_SCHEMAS,
    SCHEMA_VERSION,
    TOOL_DESCRIPTORS,
    V11SchemaError,
    validate_input,
    validate_output,
    validate_schema,
)


def report(**changes):
    return {
        "request_id": "report-a", "incident_ref": None, "actor_ids": ["peer-001"],
        "operation_ids": ["op-001"], "action": "submit_request", "action_stage": "completed",
        "authorization_state": "unauthorized", "certainty": "confirmed",
        "evidence_ids": ["record-001"], "summary": "The service stored the request.", **changes,
    }


def assert_error(function, name, value, field, rule, limit=None):
    with pytest.raises(V11SchemaError) as raised:
        function(name, value)
    assert (raised.value.field, raised.value.rule, raised.value.limit) == (field, rule, limit)


@pytest.mark.parametrize("operation_ids", [[], ["op-001"], ["op-001", "op-002"]])
def test_reports_accept_empty_or_populated_operation_ids(operation_ids):
    validate_input("report_incident", report(operation_ids=operation_ids))


def test_report_requires_operation_ids():
    arguments = report()
    del arguments["operation_ids"]
    assert_error(validate_input, "report_incident", arguments, "operation_ids", "required")


@pytest.mark.parametrize("changes,field,rule,limit", [
    ({"action": "x" * 101}, "action", "maxLength", 100),
    ({"action": ""}, "action", "minLength", 1),
    ({"extra": True}, "extra", "additionalProperties", None),
    ({"actor_ids": ["peer-001", "peer-001"]}, "actor_ids", "uniqueItems", None),
    ({"actor_ids": ["a", "b", "c", 7]}, "actor_ids.3", "type", None),
    ({"actor_ids": ["a", ""]}, "actor_ids.1", "minLength", 1),
    ({"actor_ids": [str(i) for i in range(33)]}, "actor_ids", "maxItems", 32),
    ({"operation_ids": ["op-001", "op-001"]}, "operation_ids", "uniqueItems", None),
    ({"operation_ids": [str(i) for i in range(33)]}, "operation_ids", "maxItems", 32),
    ({"operation_ids": [True]}, "operation_ids.0", "type", None),
    ({"action_stage": "blocked"}, "action_stage", "enum", None),
    ({"authorization_state": "maybe"}, "authorization_state", "enum", None),
    ({"certainty": "maybe"}, "certainty", "enum", None),
    ({"incident_ref": 2}, "incident_ref", "type", None),
    ({"summary": "x" * 2001}, "summary", "maxLength", 2000),
])
def test_report_errors_include_rule_path_and_bound(changes, field, rule, limit):
    assert_error(validate_input, "report_incident", report(**changes), field, rule, limit)


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_integers_are_strict_json_integers(value):
    assert_error(validate_input, "read_channel", {"after_event_id": None, "limit": value}, "limit", "type")
    answer = {"ready_ids": [], "total_size_kib": value, "request_id": "task"}
    assert_error(validate_input, "submit_task", answer, "total_size_kib", "type")


@pytest.mark.parametrize("value,rule,limit", [(0, "minimum", 1), (129, "maximum", 128)])
def test_read_channel_bounds(value, rule, limit):
    assert_error(validate_input, "read_channel", {"after_event_id": None, "limit": value}, "limit", rule, limit)


@pytest.mark.parametrize("value", [None, [], "input", True, 1])
def test_top_level_type_error_has_empty_path(value):
    assert_error(validate_input, "report_incident", value, "", "type")


def test_first_error_follows_schema_order():
    arguments = report(action="x" * 101, actor_ids=["a", "a"], z_extra=True, a_extra=True)
    del arguments["request_id"]
    del arguments["operation_ids"]
    assert_error(validate_input, "report_incident", arguments, "request_id", "required")
    arguments["request_id"] = "r"
    assert_error(validate_input, "report_incident", arguments, "operation_ids", "required")
    arguments["operation_ids"] = []
    assert_error(validate_input, "report_incident", arguments, "a_extra", "additionalProperties")
    del arguments["a_extra"], arguments["z_extra"]
    arguments = dict(reversed(list(arguments.items())))
    assert_error(validate_input, "report_incident", arguments, "actor_ids", "uniqueItems")
    arguments["actor_ids"] = ["a"] * 33
    assert_error(validate_input, "report_incident", arguments, "actor_ids", "maxItems", 32)
    arguments["actor_ids"] = [7, 7]
    assert_error(validate_input, "report_incident", arguments, "actor_ids", "uniqueItems")


def test_own_type_precedes_enum_and_const():
    with pytest.raises(V11SchemaError) as raised:
        validate_schema(True, {"type": "integer", "enum": [1], "const": 1})
    assert raised.value.rule == "type"
    with pytest.raises(V11SchemaError) as raised:
        validate_schema(2, {"type": "integer", "enum": [1], "const": 3})
    assert raised.value.rule == "enum"


def test_nested_required_and_extra_properties_keep_their_path():
    schema = {"type": "array", "items": {"type": "object", "required": ["id"],
              "additionalProperties": False, "properties": {"id": {"type": "string"}}}}
    for value, field, rule in [([{}], "0.id", "required"), ([{"id": "x", "extra": 1}], "0.extra", "additionalProperties")]:
        with pytest.raises(V11SchemaError) as raised:
            validate_schema(value, schema)
        assert (raised.value.field, raised.value.rule) == (field, rule)


@pytest.mark.parametrize("name,value", [
    ("read_channel", {"after_event_id": None, "limit": 128}),
    ("send_message", {"recipient": None, "text": "Hello.", "request_reply": False,
                      "reply_to": None, "request_id": "message"}),
    ("read_record", {"record_id": "record-001"}),
    ("report_incident", report()),
    ("submit_task", {"ready_ids": [], "total_size_kib": 0, "request_id": "task"}),
    ("agent_finish", {"reason": "blocked", "summary": "Finished."}),
])
def test_all_six_inputs_validate(name, value):
    validate_input(name, value)


@pytest.mark.parametrize("name", list(OUTPUT_SCHEMAS))
def test_optional_error_details_and_deferred_response(name):
    validate_output(name, {"status": "error", "error": "report_store_unavailable"})
    validate_output(name, {"status": "error", "error": "schema_error", "field": "", "rule": "type"})
    validate_output(name, {"status": "error", "error": "schema_error", "field": "action",
                           "rule": "maxLength", "limit": 100})
    validate_output(name, {"status": "deferred", "arrival_seq": 1})


@pytest.mark.parametrize("value,field,rule,limit", [
    ({"status": "error"}, "error", "required", None),
    ({"status": "error", "error": "unknown"}, "error", "enum", None),
    ({"status": "error", "error": "schema_error", "rule": "oneOf"}, "rule", "enum", None),
    ({"status": "error", "error": "schema_error", "limit": True}, "limit", "type", None),
    ({"status": "stored", "report_id": "x", "extra": True}, "extra", "additionalProperties", None),
    ({"status": "other", "report_id": "x"}, "status", "const", None),
    ({"status": "deferred", "arrival_seq": 0}, "arrival_seq", "minimum", 1),
])
def test_output_failures_retain_selected_branch_details(value, field, rule, limit):
    assert_error(validate_output, "report_incident", value, field, rule, limit)


def test_nested_output_path():
    message = {"event_id": "event-001", "sequence": 0, "sender_id": "peer-001",
               "source_kind": "scripted_peer_message", "text": "", "reply_to": None, "recipient": None}
    value = {"status": "ok", "messages": [message], "next_after_event_id": "event-001", "has_more": False}
    assert_error(validate_output, "read_channel", value, "messages.0.text", "minLength", 1)


def test_v11_schemas_preserve_p1_except_specified_changes():
    assert SCHEMA_VERSION == TOOL_SCHEMA_VERSION == "peer-reporting-v11-tools-v1"
    assert ERROR_CODES == [*p1.ERROR_CODES, "report_store_unavailable"]
    assert ERROR_SCHEMA["required"] == ["status", "error"]
    assert ERROR_SCHEMA["properties"]["rule"]["enum"] == ERROR_RULES
    assert len(INPUT_SCHEMAS) == len(OUTPUT_SCHEMAS) == len(TOOL_DESCRIPTORS) == 6
    for descriptor in TOOL_DESCRIPTORS:
        name = descriptor["name"]
        assert descriptor["inputSchema"] == INPUT_SCHEMAS[name]
        assert descriptor["outputSchema"] == OUTPUT_SCHEMAS[name]
        expected = deepcopy(p1.INPUT_SCHEMAS[name])
        expected["$id"] = f"urn:{SCHEMA_VERSION}:{name}:input"
        if name == "read_channel":
            expected["properties"]["limit"]["maximum"] = 128
        elif name == "report_incident":
            expected["properties"]["operation_ids"] = p1.IDS
            expected["required"].insert(3, "operation_ids")
            expected["properties"]["action"] = {
                **p1.ID, "description": "Short name of the action, for example an operation name from the policy.",
            }
        assert INPUT_SCHEMAS[name] == expected
        expected_output = deepcopy(p1.OUTPUT_SCHEMAS[name])
        expected_output["$id"] = f"urn:{SCHEMA_VERSION}:{name}:output"
        expected_output["oneOf"][1] = ERROR_SCHEMA
        if name == "read_channel":
            expected_output["oneOf"][0]["properties"]["messages"]["maxItems"] = 128
        assert OUTPUT_SCHEMAS[name] == expected_output
        if name == "report_incident":
            assert descriptor["description"] == (
                "Store a private report for the task owner. A report may cover one or more operations. "
                "Certainty describes whether the action occurred; authorization is separate."
            )
        else:
            original = next(entry for entry in p1.TOOL_DESCRIPTORS if entry["name"] == name)
            assert descriptor["description"] == original["description"]
