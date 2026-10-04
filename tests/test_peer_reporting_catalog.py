import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from swarm_auth_bench.model_catalog import luna_catalog
from swarm_auth_bench.peer_reporting import catalog
from swarm_auth_bench.runtime import RuntimeProtocolError


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
def test_reviewed_catalog_is_exact_and_pinned(model):
    raw, metadata = catalog.reviewed_catalog(model)
    data = json.loads(raw)
    assert len(data["models"]) == 1 and data["models"][0]["slug"] == model
    assert metadata["catalog_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not metadata["served_snapshot_pinned"]
    assert data["models"][0]["tool_mode"] == "direct"
    assert data["models"][0]["experimental_supported_tools"] == []
    assert data["models"][0]["multi_agent_version"] == "disabled"
    assert data["models"][0]["use_responses_lite"] is False
    if model == "gpt-6-luna":
        assert raw == luna_catalog()[0]


def test_unknown_model_and_effort_cannot_fall_back():
    with pytest.raises(RuntimeProtocolError, match="unreviewed"):
        catalog.ReviewedPeerRuntime(model="gpt-6")
    with pytest.raises(RuntimeProtocolError, match="xhigh"):
        catalog.ReviewedPeerRuntime(model="gpt-6-sol", reasoning_effort="high")


def test_changed_catalog_fails_before_process_launch(monkeypatch, tmp_path):
    raw, _ = catalog.reviewed_catalog("gpt-6-sol")
    (tmp_path / "gpt-6-sol-direct.json").write_bytes(raw + b" ")
    monkeypatch.setattr(catalog, "DATA", tmp_path)
    with pytest.raises(RuntimeProtocolError, match="reviewed hash"):
        catalog.ReviewedPeerRuntime(model="gpt-6-sol")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
async def test_probe_and_authenticated_launch_use_same_exact_catalog(monkeypatch, tmp_path, model):
    calls = []

    async def capture(*command, **kwargs):
        calls.append((command, kwargs))
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    runtime = catalog.ReviewedPeerRuntime(model=model)
    for name in ("probe", "live"):
        directory = tmp_path / name
        directory.mkdir()
        await runtime._launch(home=directory, overrides=(('model_provider="swarm_probe"',)
                                                        if name == "probe" else ()))
    for command, kwargs in calls:
        settings = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "-c"]
        paths = [json.loads(s.split("=", 1)[1]) for s in settings if s.startswith("model_catalog_json=")]
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == catalog.reviewed_catalog(model)[0]
        assert kwargs["cwd"] == Path(paths[0]).parent
    assert runtime.metadata["requested_model"] == model
    assert runtime.metadata["peer_catalog_override"]["requested_model"] == model


@pytest.mark.asyncio
async def test_no_caller_catalog_override(tmp_path):
    runtime = catalog.ReviewedPeerRuntime(model="gpt-6-astra")
    with pytest.raises(RuntimeProtocolError, match="cannot override"):
        await runtime._launch(home=tmp_path, overrides=('model_catalog_json="unreviewed.json"',))
