"""The v1.1 observer adapter over a scripted in-memory app-server. No Codex process or provider is started."""

from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting import schemas as p1_schemas
from swarm_auth_bench.peer_reporting.live_runtime import PeerCodexRuntime
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11.bundle import require_v11_tools
from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.live_runtime import (
    V11PeerRuntime,
    run_live_observer,
    validate_preflight,
)
from swarm_auth_bench.runtime import RuntimeProtocolError

from .live_fakes import (
    FakeTransport,
    FakeV11World,
    build_fixture,
    caps_record,
    fake_bundle,
    fake_version,
    load_template,
    qualifier_steps,
    report_steps,
)


def fixture(level="L1", variant="violation"):
    return build_fixture(load_template("token-issuance"), split="smoke", seed=7, level=level, variant=variant)


async def observe(tmp_path, *, model="gpt-6-sol", effort="low", mode="normal", steps=None, sample=None):
    bundle = fake_bundle()
    sample = sample or fixture()
    home = tmp_path / "home"
    home.mkdir()
    runtime = FakeTransport(model, effort, steps if steps is not None else qualifier_steps(sample), home)
    caps = lane_caps(caps_record(), "smoke", 1)
    preflight = await v11_live.manifest_preflight(runtime, model, caps, reasoning_effort=effort, bundle=bundle,
                                                  version_reader=fake_version)
    FakeV11World.created.clear()
    result = await run_live_observer(sample, "Exact observer instructions.", directory=tmp_path / "attempt",
                                     attempt_id="attempt-1", requested_model=model, reasoning_effort=effort,
                                     world_mode=mode, bundle=bundle, caps=caps, qualification=preflight,
                                     runtime=runtime)
    return result, runtime, sample, preflight


def test_v11_runtime_accepts_low_and_xhigh_while_p1_still_pins_xhigh():
    for effort in ("low", "xhigh"):
        runtime = V11PeerRuntime(model="gpt-6-astra", reasoning_effort=effort)
        assert runtime.reasoning_effort == effort and runtime.metadata["reasoning_effort"] == effort
        assert runtime.metadata["peer_catalog_override"]["requested_model"] == "gpt-6-astra"
    for effort in ("medium", "high", None):
        with pytest.raises(ValueError):
            V11PeerRuntime(model="gpt-6-astra", reasoning_effort=effort)
    with pytest.raises(RuntimeProtocolError):
        V11PeerRuntime(model="gpt-5", reasoning_effort="low")
    with pytest.raises(RuntimeProtocolError, match="xhigh"):
        PeerCodexRuntime(model="gpt-6-astra", reasoning_effort="low")


def test_tool_manifest_binding_refuses_the_p1_tools():
    assert require_v11_tools(fake_bundle())
    p1 = fake_bundle(schema_version=p1_schemas.SCHEMA_VERSION, tool_descriptors=deepcopy(p1_schemas.TOOL_DESCRIPTORS),
                     input_schemas=deepcopy(p1_schemas.INPUT_SCHEMAS), output_schemas=deepcopy(p1_schemas.OUTPUT_SCHEMAS))
    with pytest.raises(ValueError, match="operation_ids") as refused:
        require_v11_tools(p1)
    assert "read_channel limit maximum is not 128" in str(refused.value)
    assert "tool schema version" in str(refused.value)
    five = fake_bundle(tool_descriptors=fake_bundle().tool_descriptors[:5])
    with pytest.raises(ValueError, match="six"):
        require_v11_tools(five)
    assert fake_bundle().tool_manifest_hash != p1.tool_manifest_hash


async def test_low_effort_reaches_the_turn_start_and_the_world_mode_reaches_the_world(tmp_path):
    result, runtime, sample, preflight = await observe(tmp_path, effort="low", mode="normal")
    assert runtime.turn_efforts == ["low"] and runtime.probe_calls == 1
    assert FakeV11World.created == [{"trial_id": "attempt-1", "world_mode": "normal", "max_tool_requests": 32}]
    assert result["reasoning_effort"] == "low" and result["world_mode"] == "normal"
    assert result["tool_manifest_hash"] == fake_bundle().tool_manifest_hash
    assert preflight["reasoning_effort"] == "low" and preflight["tool_schema_version"] == "peer-reporting-v11-tools-v1"
    assert result["termination_kind"] == "natural_end" and result["usage"]["total_tokens"] == 4321
    assert result["exposure_confirmed"] and result["world_checkpoint"] is not None
    report = [request for request in result["world_state"]["requests"] if request["name"] == "report_incident"]
    assert report[0]["arguments"]["operation_ids"] == ["qualifier-operation-1"]
    assert [item["report"]["operation_ids"] for item in result["world_state"]["reports"]] == [["qualifier-operation-1"]]
    assert result["events"][0]["kind"] == "observer_admitted" and result["events"][0]["reasoning_effort"] == "low"
    assert result["events"][0]["world_mode"] == "normal"


async def test_unavailable_report_store_records_the_attempt_and_stores_nothing(tmp_path):
    sample = fixture()
    result, runtime, _, _ = await observe(tmp_path, effort="xhigh", mode="report_store_unavailable",
                                          steps=report_steps(sample), sample=sample)
    assert runtime.turn_efforts == ["xhigh"]
    assert FakeV11World.created[0]["world_mode"] == "report_store_unavailable"
    assert result["world_mode"] == "report_store_unavailable" and result["world_state"]["reports"] == []
    attempts = [request for request in result["tool_requests"] if request["tool"] == "report_incident"]
    assert attempts[0]["result"] == {"status": "error", "error": "report_store_unavailable"}
    assert attempts[0]["arguments"]["operation_ids"] == [sample["truth"]["focal_operation_id"]]
    assert result["termination_kind"] == "natural_end"


async def test_preflight_binds_effort_and_v11_tools(tmp_path):
    bundle = fake_bundle()
    caps = lane_caps(caps_record(), "smoke", 1)
    home = tmp_path / "home"
    home.mkdir()
    runtime = FakeTransport("gpt-6-luna", "low", [], home)
    with pytest.raises(v11_live.PreflightError, match="effort"):
        await v11_live.manifest_preflight(runtime, "gpt-6-luna", caps, reasoning_effort="xhigh", bundle=bundle,
                                          version_reader=fake_version)
    record = await v11_live.manifest_preflight(runtime, "gpt-6-luna", caps, reasoning_effort="low", bundle=bundle,
                                               version_reader=fake_version)
    assert validate_preflight("gpt-6-luna", caps, record, runtime, reasoning_effort="low", bundle=bundle)
    with pytest.raises(ValueError):
        validate_preflight("gpt-6-luna", caps, record, runtime, reasoning_effort="xhigh", bundle=bundle)
    stale = {**record, "tool_manifest_hash": "0" * 64}
    with pytest.raises(ValueError):
        validate_preflight("gpt-6-luna", caps, stale, runtime, reasoning_effort="low", bundle=bundle)
    with pytest.raises(ValueError):
        await run_live_observer(fixture(), "Instructions.", directory=tmp_path / "attempt", attempt_id="attempt-1",
                                requested_model="gpt-6-luna", reasoning_effort="low", world_mode="degraded",
                                bundle=bundle, caps=caps, qualification=record, runtime=runtime)
    assert runtime.methods == []  # no session was started for any refusal


async def test_turn_start_with_another_effort_is_refused(tmp_path):
    bundle = fake_bundle()
    sample = fixture()
    caps = lane_caps(caps_record(), "smoke", 1)
    home = tmp_path / "home"
    home.mkdir()
    runtime = FakeTransport("gpt-6-sol", "low", qualifier_steps(sample), home)
    preflight = await v11_live.manifest_preflight(runtime, "gpt-6-sol", caps, reasoning_effort="low", bundle=bundle,
                                                  version_reader=fake_version)
    original = runtime._turn_params
    runtime._turn_params = lambda thread, prompt, effort=None: original(thread, prompt, "xhigh")
    result = await run_live_observer(sample, "Instructions.", directory=tmp_path / "attempt", attempt_id="attempt-1",
                                     requested_model="gpt-6-sol", reasoning_effort="low", world_mode="normal",
                                     bundle=bundle, caps=caps, qualification=preflight, runtime=runtime)
    assert "turn/start" not in runtime.methods and runtime.turn_efforts == []
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert any("reasoning effort differs" in failure for failure in result["infrastructure_failures"])
