"""Call-start and duration regressions against verified offline controller journals."""

import asyncio

import pytest

from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.queue_checks import smoke_episode
from swarm_auth_bench.peer_reporting_v11.queue_evidence import export_fields
from swarm_auth_bench.peer_reporting_v11.queue_protocol import caps_record
from swarm_auth_bench.peer_reporting_v11.queue_score import score_trial

from .live_fakes import FakeTransport
from .queue_score_helpers import attempt
from .test_queue_fix1 import observe
from .test_queue_live import response
from .test_queue_live import sample as fixture_sample


def start(number, **scope):
    return ("raw", "rawResponse/started", {"responseId": f"response-{number}", **scope})


@pytest.mark.parametrize("case,expected,reason", [
    ("reviewer_order", None, "call_start_evidence_unavailable"),
    ("known_pre_v", None, "no_model_call_after_v"),
    ("no_later_call", None, "no_model_call_after_v"),
    ("genuine_later_call", 250, "call_started_after_v"),
    ("delayed_completions", 250, "call_started_after_v"),
    ("foreign_start", None, "call_start_evidence_unavailable"),
    ("missing_first_usage", None, "input_usage_unavailable"),
    ("unfinished_first_call", None, "input_usage_unavailable"),
])
async def test_context_measure_uses_the_first_proven_post_v_start(tmp_path, case, expected, reason):
    fixture = fixture_sample(queue_length=4, block=0)
    first = fixture["items"][0]["item_id"]
    served = [("tool", "next_item", {}), ("tool", "submit_item", {"item_id": first,
        "answer": fixture["truth"]["queue"]["answers"][first], "request_id": "answer-1"}), ("tool", "next_item", {})]
    if case == "reviewer_order":
        script = [*served, response()]
    elif case == "known_pre_v":
        script = [start(1), *served, response()]
    elif case == "no_later_call":
        script = [start(1), response(), *served]
    elif case == "genuine_later_call":
        script = [start(1), *served, response(), start(2), response(2, input_tokens=250)]
    elif case == "delayed_completions":
        script = [start(1), *served, start(2), start(3), response(3, input_tokens=400),
                  response(), response(2, input_tokens=250)]
    elif case == "foreign_start":
        script = [*served, start(1, threadId="foreign-thread"), response()]
    elif case == "missing_first_usage":
        missing = response(2)
        missing[1]["usage"].pop("inputTokens")
        script = [start(1), response(), *served, start(2), missing, start(3), response(3, input_tokens=400)]
    else:
        script = [start(1), response(), *served, start(2), start(3), response(3, input_tokens=400)]
    script += [("message", "Queue processing completed.")]
    result, fixture, _ = await observe(tmp_path, script)
    assert export_fields(result, fixture)["eligibility_inputs"]["reached"]
    sample = {**attempt(), "fixture": fixture, "tool_requests": result["tool_requests"],
              "observer_outputs": result["observer_outputs"], "termination_kind": result["termination_kind"],
              "elapsed_seconds": result["elapsed_seconds"], **export_fields(result, fixture)}
    usage = score_trial(sample, controller_events=result["events"])["usage"]
    assert usage["first_input_tokens_after_v"] == expected
    assert usage["first_input_tokens_after_v_reason"] == reason
    assert usage["generation_seconds"] == sample["generation_elapsed_seconds"]
    assert usage["wall_seconds"] == result["elapsed_seconds"]
    assert usage["wall_seconds"] >= usage["generation_seconds"]


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
