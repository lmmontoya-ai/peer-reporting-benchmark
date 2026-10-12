"""Offline budget command, smoke failures, primary recordings, and root isolation."""

import asyncio
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11 import cli, live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle, tools_for_levels
from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.live_review import report_attempts
from swarm_auth_bench.peer_reporting_v11.live_runtime import run_live_observer
from swarm_auth_bench.peer_reporting_v11.queue_budget import B, budget_results, check_budget, check_capture
from swarm_auth_bench.peer_reporting_v11.queue_checks import smoke_episode
from swarm_auth_bench.peer_reporting_v11.queue_evidence import eligibility_inputs, verify_result
from swarm_auth_bench.peer_reporting_v11.queue_protocol import QUEUE_OPTIONS, caps_record
from swarm_auth_bench.peer_reporting_v11.queue_templates import load_template
from swarm_auth_bench.peer_reporting_v11.queue_text import build_instructions
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_Q
from swarm_auth_bench.runtime import RuntimeProtocolError

from .live_fakes import FakeTransport, fake_version
from .test_queue_live import response, sample, steps


@pytest.mark.parametrize("alter", [False, True])
async def test_budget_command_with_fake_client_path(monkeypatch, alter):
    executable = str(Path(__file__).with_name("queue_budget_client.py"))
    original = asyncio.create_subprocess_exec

    async def launch(program, *arguments, **kwargs):
        assert program == executable
        if alter and "env" in kwargs:
            kwargs["env"]["QUEUE_BUDGET_ALTER"] = "1"
        return await original(sys.executable, executable, *arguments, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    if alter:
        with pytest.raises(RuntimeProtocolError, match="changed"):
            await check_budget(executable, timeout=20)
    else:
        result = await check_budget(executable, timeout=20)
        assert result["passed"] and len(result["results"]) == 2
        assert all(sample["bytes"] == B for sample in result["results"])


def test_budget_samples_and_missing_or_altered_capture():
    for text in budget_results():
        assert len(text.encode("utf-8")) == B
        check_capture({"input": [{"type": "function_call_output", "call_id": "budget-0", "output": text}]}, text, "budget-0")
        with pytest.raises(RuntimeProtocolError, match="changed"):
            check_capture({"input": [{"type": "function_call_output", "call_id": "budget-0", "output": text[:-1]}]}, text, "budget-0")
    with pytest.raises(RuntimeProtocolError, match="unique"):
        check_capture({"input": []}, "x", "budget-0")


@pytest.fixture
async def recorded(tmp_path):
    fixture = sample(queue_length=4, block=0)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    home = tmp_path / "home"
    home.mkdir()
    runtime = FakeTransport("gpt-6-sol", "low", steps(fixture), home, options=QUEUE_OPTIONS)
    caps = lane_caps(caps_record(), "smoke", 1)
    preflight = await live.manifest_preflight(runtime, runtime.model, caps, reasoning_effort="low", bundle=bundle,
                                             version_reader=fake_version)
    result = await run_live_observer(fixture, build_instructions(load_template("ledger-close")), directory=tmp_path / "attempt",
        attempt_id="smoke-check", requested_model=runtime.model, reasoning_effort="low", world_mode="normal",
        bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    payload = {"observer_result": result, "fixture_hash": content_hash(fixture),
               **{key: result[key] for key in ("reasoning_items", "runtime_recording") if key in result}}
    return result, payload, tmp_path / "attempt", bundle


@pytest.mark.parametrize("check", ["room_events", "reasoning_reconciled", "cleanup_without_compaction",
                                   "per_call_recording", "output_budget"])
async def test_each_smoke_check_passes_and_fails(recorded, check):
    result = deepcopy(recorded[0])
    assert smoke_episode(result, caps_record()["trial"])["passed"]
    if check == "room_events":
        result["tool_requests"][0]["result"].pop("room_events")
    elif check == "reasoning_reconciled":
        result.pop("reasoning_items")
    elif check == "cleanup_without_compaction":
        result["runtime_recording"]["compaction_events"] = [{"raw_event_index": 0}]
    elif check == "per_call_recording":
        result["runtime_recording"]["model_calls"][0]["output_tokens"] = None
    else:
        result["tool_requests"][0]["result"]["result"]["text"] = "x" * B
    checked = smoke_episode(result, caps_record()["trial"])
    assert not checked["passed"] and not checked["checks"][check]


@pytest.mark.parametrize("key", ["tool_requests", "tool_receipts", "observer_outputs", "reasoning_items",
                                 "runtime_recording", "usage", "termination_kind"])
async def test_altered_derived_records_are_refused(recorded, key):
    _, payload, directory, bundle = recorded
    verify_result(payload, directory, bundle)
    changed = deepcopy(payload)
    if key == "termination_kind":
        changed["observer_result"][key] = "per_trial_limit"
    elif key == "usage":
        changed["observer_result"][key]["total_tokens"] += 1
    elif key == "runtime_recording":
        changed["observer_result"][key]["model_calls"][0]["input_tokens"] += 1
    else:
        changed["observer_result"][key] = []
    with pytest.raises(ValueError, match="primary evidence"):
        verify_result(changed, directory, bundle)


def test_mixed_level_root_is_refused():
    with pytest.raises(ValueError, match="mix level Q"):
        tools_for_levels(load_bundle(), ["Q", "P"])


async def test_world_fixture_binding_is_independent_of_the_owner_text(recorded):
    _, payload, directory, bundle = recorded
    changed = deepcopy(payload)
    changed["fixture_hash"] = "0" * 64
    with pytest.raises(ValueError, match="world fixture"):
        verify_result(changed, directory, bundle)


async def test_export_report_storage_flag_matches_the_queue_world(recorded):
    result = recorded[0]
    attempts = report_attempts(result["tool_requests"])
    assert len(attempts) == len(result["world_state"]["reports"]) == 1
    assert attempts[0]["stored"]
    assert attempts[0]["result"]["result"]["report_id"] == result["world_state"]["reports"][0]["report_id"]


async def test_token_stop_after_finish_is_exported_as_cap_ended(tmp_path):
    fixture = sample(queue_length=4, block=0)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    home = tmp_path / "home"
    home.mkdir()
    script = [("tool", "agent_finish", {"reason": "completed", "summary": "Record processing ended."}),
              response(99, input_tokens=15000000), ("sleep", 1)]
    runtime = FakeTransport("gpt-6-sol", "low", script, home, options=QUEUE_OPTIONS)
    caps = lane_caps(caps_record(), "compatibility", 1)
    preflight = await live.manifest_preflight(runtime, runtime.model, caps, reasoning_effort="low", bundle=bundle,
                                             version_reader=fake_version)
    result = await run_live_observer(fixture, build_instructions(load_template("ledger-close")), directory=tmp_path / "attempt",
        attempt_id="finish-token-check", requested_model=runtime.model, reasoning_effort="low", world_mode="normal",
        bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    assert not result["infrastructure_failures"]
    assert eligibility_inputs(result, fixture)["cap_ended"]
    assert "turn/interrupt" in runtime.methods


def test_offline_cli_commands_are_available():
    args = cli._parser().parse_args(["queue-budget-check", "--client", "fake-client"])
    assert args.client == "fake-client"
    assert cli._parser().parse_args(["build-study", "study", "--level", "Q", "--caps", "caps.json"]).level == "Q"


def test_budget_cli_runs_with_fake_client_path(monkeypatch):
    executable = str(Path(__file__).with_name("queue_budget_client.py"))
    original = asyncio.create_subprocess_exec

    async def launch(program, *arguments, **kwargs):
        assert program == executable
        return await original(sys.executable, executable, *arguments, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    assert cli.main(["queue-budget-check", "--client", executable]) == 0


async def test_queue_turn_start_refuses_summary_mismatch(tmp_path):
    fixture = sample(queue_length=4, block=0)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    home = tmp_path / "home"
    home.mkdir()
    runtime = FakeTransport("gpt-6-sol", "low", steps(fixture), home, options=QUEUE_OPTIONS)
    caps = lane_caps(caps_record(), "compatibility", 1)
    preflight = await live.manifest_preflight(runtime, runtime.model, caps, reasoning_effort="low", bundle=bundle,
                                             version_reader=fake_version)
    original = runtime._turn_params

    def altered_turn(*args):
        return {**original(*args), "summary": None}

    runtime._turn_params = altered_turn
    result = await run_live_observer(fixture, build_instructions(load_template("ledger-close")), directory=tmp_path / "attempt",
        attempt_id="summary-check", requested_model=runtime.model, reasoning_effort="low", world_mode="normal",
        bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert "turn/start" not in runtime.methods
