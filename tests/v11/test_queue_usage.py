"""Descriptive usage and duration checks through offline production runtime paths."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.queue_checks import smoke_episode
from swarm_auth_bench.peer_reporting_v11.queue_evidence import export_fields
from swarm_auth_bench.peer_reporting_v11.queue_protocol import caps_record
from swarm_auth_bench.peer_reporting_v11.queue_score import score_trial
from swarm_auth_bench.runtime import CodexRuntime

from .live_fakes import FakeTransport
from .queue_score_helpers import attempt
from .test_queue_fix1 import observe
from .test_queue_live import response
from .test_queue_live import sample as fixture_sample


class WireTransport(FakeTransport):
    # The probe is offline. JSON-RPC reading, writing, dispatch, recording and
    # observer execution all use the production runtime, without event injection.
    _send = CodexRuntime._send

    async def start(self):
        if self._proc is not None:
            return
        self.runtime_version = "codex-cli 0.158.0"
        self._home = self.fake_home
        fixture = fixture_sample(queue_length=4, block=0)
        self._proc = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).with_name("queue_usage_client.py")), json.dumps(fixture), self.script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._reader_task = asyncio.create_task(self._reader())
        await self._call("initialize", self._initialize_params())
        await self._send({"method": "initialized", "params": {}})


@pytest.mark.parametrize("case,expected_inputs", [
    ("present", [50, 1051, 123, 987]),
    ("absent", []),
    ("silent_retry", [50, 51, 987]),
    ("missing_usage", [None, None, None, None]),
    ("partial_usage", [50, None, 123, 987]),
])
async def test_descriptive_usage_comes_from_completion_order_through_the_production_wire(tmp_path, case, expected_inputs):
    caps = lane_caps(caps_record(), "smoke", 1)
    caps.update(max_trial_wall_seconds=10, drain_grace_seconds=2)
    result, fixture, _ = await observe(tmp_path, case, caps=caps, runtime_class=WireTransport)
    fields = export_fields(result, fixture)
    assert fields["eligibility_inputs"]["reached"]
    sample = {**attempt(), "fixture": fixture, "tool_requests": result["tool_requests"],
              "observer_outputs": result["observer_outputs"], "termination_kind": result["termination_kind"],
              "elapsed_seconds": result["elapsed_seconds"], **fields}
    usage = score_trial(sample, controller_events=result["events"])["usage"]
    calls = result["runtime_recording"]["model_calls"]
    completions = [event for event in result["raw_events"]
                   if (event.get("raw") or {}).get("method") == "rawResponse/completed"]
    assert [call["input_tokens"] for call in calls] == expected_inputs
    assert [call["response_id"] for call in calls] == [event["raw"]["params"]["responseId"] for event in completions]
    assert usage["per_call_usage"] == (calls or None)
    inputs = [tokens for tokens in expected_inputs if tokens is not None]
    assert usage["max_call_input_tokens"] == max(inputs, default=None)
    reason = "recorded_per_call_usage" if calls else "per_call_usage_unavailable"
    assert usage["per_call_usage_reason"] == reason
    assert usage["max_call_input_tokens_reason"] == (reason if inputs or not calls else "input_usage_unavailable")
    assert usage["generation_seconds"] == sample["generation_elapsed_seconds"]
    assert usage["wall_seconds"] >= usage["generation_seconds"]
    if case == "silent_retry":
        assert [call["response_id"] for call in calls] == ["response-1", "response-2", "response-4"]
        assert not any((event.get("raw") or {}).get("method") == "error" for event in result["raw_events"])
        focal = fields["eligibility_inputs"]["focal_served_event_seq"]
        retry_completion = next(event["event_seq"] for event in result["events"] if event["kind"] == "runtime_event"
            and ((event["event"].get("raw") or {}).get("params") or {}).get("responseId") == "response-4")
        assert focal < retry_completion
    if case == "absent":
        assert result["runtime_recording"]["context_estimates"]


async def test_score_and_smoke_use_generation_duration_when_cleanup_crosses_the_wall_cap(tmp_path):
    class SlowClose(FakeTransport):
        async def close(self):
            await asyncio.sleep(1.1)
            await super().close()

    caps = lane_caps(caps_record(), "smoke", 1)
    caps["max_trial_wall_seconds"] = 1
    result, fixture, _ = await observe(tmp_path, [response(),
        ("tool", "agent_finish", {"reason": "completed", "summary": "Record processing ended."}),
        ("sleep", 0.1), ("message", "Record processing completed.")], caps=caps, runtime_class=SlowClose)
    exported = export_fields(result, fixture)
    sample = {**attempt(), "fixture": fixture, "tool_requests": result["tool_requests"],
              "observer_outputs": result["observer_outputs"], "termination_kind": result["termination_kind"],
              "elapsed_seconds": result["elapsed_seconds"], **exported}
    usage = score_trial(sample, controller_events=result["events"])["usage"]
    checked = smoke_episode(result, caps)
    assert checked["passed"] and not checked["resources"]["caps_binding"]
    assert not usage["cap_ended"]
    assert usage["generation_seconds"] == checked["resources"]["wall_seconds"] == exported["generation_elapsed_seconds"]
    assert usage["wall_seconds"] == checked["resources"]["total_wall_seconds"]
    assert usage["generation_seconds"] < caps["max_trial_wall_seconds"] <= usage["wall_seconds"]
