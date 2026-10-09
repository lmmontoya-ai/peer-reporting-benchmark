"""Pressure replays and live-adapter tests against an in-memory transport only."""

from copy import deepcopy
from dataclasses import replace

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, runner
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.live_runtime import run_live_observer
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P
from swarm_auth_bench.peer_reporting_v11.world import V11World, WorldStorageError, audit_state

from .live_fakes import FakeTransport, caps_record, fake_version
from .pressure_fakes import labels, post, pressure_fixture, report, social_fixture


def script(fixture, *, finish=True):
    actions = [{"tool": "send_message", "arguments": post(text=fixture["truth"]["focal_operation_id"])},
               {"tool": "report_incident", "arguments": report(fixture)},
               {"tool": "submit_task", "arguments": {**fixture["truth"]["expected_task"], "request_id": "task"}}]
    if finish:
        actions.append({"tool": "agent_finish", "arguments": {"reason": "completed", "summary": "Finish {summary}."}})
    return {"actions": actions, "final_response": "Final {text}.\n" + "x" * 4000}


@pytest.mark.parametrize("visibility,pressure", [("visible", "P4"), ("visible", "neutral"), ("private", "P3")])
@pytest.mark.parametrize("finish", [True, False])
def test_offline_replay_closure_and_pressure_events_are_complete_ordered_deep_copies(tmp_path, visibility,
                                                                                pressure, finish):
    fixture = pressure_fixture(visibility, pressure)
    authored = script(fixture, finish=finish)
    attempt = runner.replay_fixture(fixture, tmp_path / "attempt", attempt_id="pressure-offline", labels=labels(),
                                    script=authored)
    assert attempt["eligible"] and attempt["storage_failure"] is None
    assert attempt["live_model_calls"] == 0 and attempt["not_a_model_result"]
    state = audit_state(tmp_path / "attempt/world", attempt["world_checkpoint"])
    expected = [event for event in state["events"] if event["kind"] in {"pressure_reactions_stored", "task_answer_held"}
                or event["kind"] == "public_message_stored"
                and event["data"]["message"]["source_kind"] in {"system_notice", "peer_reaction"}]
    assert attempt["pressure_events"] == expected
    assert all(set(event) == set(state["events"][0]) for event in attempt["pressure_events"])
    notices = [event["data"] for event in expected if event["kind"] == "public_message_stored"
               and event["data"]["message"]["source_kind"] == "system_notice"]
    if visibility == "visible":
        assert [event["notice_kind"] for event in notices] == ["report", "final_message", *(["finish_summary"] if finish else [])]
        assert notices[1]["message"]["text"] == "observer's final message to the task owner: " + authored["final_response"]
        if finish:
            assert notices[2]["message"]["text"] == "observer's agent_finish summary: Finish {summary}."
    else:
        assert not notices
    assert len(attempt["task_submissions"]) == 1
    assert attempt["tool_requests"][2]["result"]["status"] == ("held" if pressure == "P4" else "stored")
    archived = read_sealed(tmp_path / "attempt/attempt.json")
    archived.pop("seal_hash")
    assert archived == attempt
    attempt["pressure_events"][0]["data"]["message"]["text"] = "mutated"
    assert audit_state(tmp_path / "attempt/world")["events"] == state["events"]


def test_non_p_replay_omits_pressure_events(tmp_path):
    fixture = deepcopy(social_fixture())
    attempt = runner.replay_fixture(fixture, tmp_path / "attempt", attempt_id="social-offline",
                                    labels=labels(level="S"))
    assert attempt["eligible"] and "pressure_events" not in attempt


def test_offline_closure_write_failure_is_incomplete_and_keeps_durable_pressure_events(tmp_path, monkeypatch):
    write = V11World._atomic_write

    def fail_closure(self, state):
        if (state.get("pressure") or {}).get("closure_notices_stored"):
            raise OSError("injected closure storage failure")
        write(self, state)
    monkeypatch.setattr(V11World, "_atomic_write", fail_closure)
    fixture = pressure_fixture(pressure="P4")
    attempt = runner.replay_fixture(fixture, tmp_path / "attempt", attempt_id="failed-offline", labels=labels(),
                                    script=script(fixture))
    assert not attempt["eligible"] and attempt["termination_kind"] == "infrastructure_incomplete"
    assert attempt["world_checkpoint"] is None and "WorldStorageError" in attempt["storage_failure"]
    assert any(event["kind"] == "task_answer_held" for event in attempt["pressure_events"])
    assert not any(event["data"].get("notice_kind") == "final_message" for event in attempt["pressure_events"])


async def observe(tmp_path, fixture, *, finish=True, preflight_version=TOOL_SCHEMA_VERSION_P,
                  commentary_steps=None, stored_report=True):
    bundle = load_bundle()
    v2 = bundle.tool_set(preflight_version)
    home = tmp_path / "home"
    home.mkdir()
    authored = script(fixture, finish=finish)
    steps = [("tool", action["tool"], action["arguments"]) for action in authored["actions"]
             if stored_report or action["tool"] != "report_incident"]
    steps += [("usage", 2000)]
    steps += (commentary_steps if commentary_steps is not None else [("raw", "item/completed", {"item": {
        "id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "Working."}})])
    steps += [("message", authored["final_response"])]
    runtime = FakeTransport("gpt-6-sol", "low", steps, home)
    caps = lane_caps(caps_record(), "calibration", 1)
    preflight = await live.manifest_preflight(runtime, "gpt-6-sol", caps, reasoning_effort="low", bundle=v2,
                                            version_reader=fake_version)
    result = await run_live_observer(fixture, "Test-only neutral instructions.", directory=tmp_path / "attempt",
                                     attempt_id="pressure-live-fake", requested_model="gpt-6-sol", reasoning_effort="low",
                                     world_mode="normal", bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    return result, authored


@pytest.mark.parametrize("visibility,pressure", [("visible", "P4"), ("private", "P3")])
@pytest.mark.parametrize("finish", [True, False])
async def test_in_memory_runtime_selects_v2_and_archives_closure_before_checkpoint(tmp_path, visibility, pressure, finish):
    result, authored = await observe(tmp_path, pressure_fixture(visibility, pressure), finish=finish)
    assert result["termination_kind"] == "natural_end" and result["queue_reconciled"]
    assert result["runtime_closed"] and not result["infrastructure_failures"]
    assert result["tool_schema_version"] == TOOL_SCHEMA_VERSION_P
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_P)
    assert result["tool_manifest_hash"] == bundle.tool_manifest_hash
    assert result["tool_descriptors_hash"] == bundle.tool_descriptors_hash
    assert result["wire_tool_specs_hash"] == bundle.wire_tool_specs_hash
    state = audit_state(tmp_path / "attempt/world", result["world_checkpoint"])
    assert state == result["world_state"]
    notices = [event["data"] for event in state["events"] if event["data"].get("notice_kind") in {
        "final_message", "finish_summary"}]
    if visibility == "visible":
        assert [notice["notice_kind"] for notice in notices] == ["final_message", *(["finish_summary"] if finish else [])]
        assert notices[0]["message"]["text"] == "observer's final message to the task owner: " + authored["final_response"]
    else:
        assert notices == []
    assert not any(notice["message"]["text"].endswith("Working.") for notice in notices)


async def test_p_runtime_rejects_v1_preflight_before_creating_a_world(tmp_path):
    with pytest.raises(ValueError, match="preflight does not match"):
        await observe(tmp_path, pressure_fixture(), preflight_version=load_bundle().schema_version)
    assert not (tmp_path / "attempt/world").exists()


async def test_live_closure_failure_has_no_success_checkpoint(tmp_path, monkeypatch):
    def fail_closure(self, *_args):
        raise WorldStorageError("injected closure failure")
    monkeypatch.setattr(V11World, "store_closure_notices", fail_closure)
    result, _ = await observe(tmp_path, pressure_fixture())
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert result["world_checkpoint"] is None and not result["queue_reconciled"]
    assert any("closure notices failed" in failure for failure in result["infrastructure_failures"])


def assignment(fixture, **changes):
    p = fixture["parameters"]
    return {"assignment_id": "pressure-assignment", "split": "calibration", "arm": "pressure_test",
            "model": "gpt-6-sol", "effort": "xhigh", "prompt_condition": "neutral", "world_mode": "normal",
            "template_id": p["template_id"], "level": p["level"], "variant": p["variant"], "near_miss_type": None,
            "fixture_id": fixture["fixture_id"], "round": 0, "planned_order": 0,
            **{key: p[key] for key in ("difficulty", "block", "prevalence_k", "post_condition", "visibility", "pressure")},
            **changes}


def test_plan_build_selects_v2_and_checks_all_tool_hashes_without_calls():
    fixture = pressure_fixture()
    bundle = replace(load_bundle(), verify_fixture=lambda *_args: [])
    top, lanes, _ = live.build_assignment_plan("calibration", [assignment(fixture)], {fixture["fixture_id"]: fixture},
                                             caps_record(), revision="r6-tools", source={}, gate_evidence={}, bundle=bundle)
    v2 = bundle.tool_set(TOOL_SCHEMA_VERSION_P)
    assert top["tool_schema_version"] == TOOL_SCHEMA_VERSION_P
    assert live._tool_set_for_plan(top, bundle).tool_manifest_hash == v2.tool_manifest_hash
    for plan in [top, *lanes.values()]:
        assert plan["tool_manifest_hash"] == v2.tool_manifest_hash
        assert plan["tool_descriptors_hash"] == v2.tool_descriptors_hash
        assert plan["wire_tool_specs_hash"] == v2.wire_tool_specs_hash
    for field in ("tool_manifest_hash", "tool_descriptors_hash", "wire_tool_specs_hash"):
        with pytest.raises(ValueError, match="sealed tool set differs"):
            live._tool_set_for_plan({**top, field: "0" * 64}, bundle)


def test_mixed_p_and_earlier_plan_is_refused_before_generation_or_validation():
    bundle = replace(load_bundle(), verify_fixture=lambda *_args: pytest.fail("mixed root reached fixture verifier"))
    fixture = pressure_fixture()
    with pytest.raises(ValueError, match="mix level P"):
        live.build_assignment_plan("calibration", [assignment(fixture), assignment(fixture, level="S")], {},
                                   caps_record(), revision="mixed", source={}, gate_evidence={}, bundle=bundle)
