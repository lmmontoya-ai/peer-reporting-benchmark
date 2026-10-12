"""Review regressions use archived events, fake transports and local dummy processes."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_auth_bench import runtime as core
from swarm_auth_bench import runtime_cleanup as cleanup
from swarm_auth_bench.runtime import RuntimeOptions
from swarm_auth_bench.runtime_recording import attempt_recording, probe_record

from .live_fakes import FakeTransport
from .test_runtime_options import observe, usage


def event(method, **params):
    return {"kind": "codex_event", "agent_id": "observer",
            "raw": {"method": method, "params": {"threadId": "t", "turnId": "u", **params}}}


def record(events, *, enabled=True, **options):
    return attempt_recording(events, thread_id="t", turn_id="u", record_calls=enabled,
                             record_reasoning=False, attestations=[], **options)


def mark(home, runner=101, **child):
    home.mkdir()
    (home / cleanup.OWNER_MARKER).write_bytes(json.dumps(
        {"runner_pid": runner, "runner_start": "old" if runner == 101 else "live", **child}).encode("utf-8"))
    (home / "retained.txt").write_bytes(b"retained\n")


@pytest.mark.parametrize("marked", [True, False])
def test_orphan_report_retains_replaced_home_and_target(tmp_path, monkeypatch, caplog, marked):
    old, target, saved = (tmp_path / name for name in ("work-a-old", "work-z-target", "saved-old"))
    mark(old, process_pid=303, process_start="old-child")
    if marked:
        mark(target, 202, process_pid=404, process_start="live-child")
    else:
        target.mkdir()
        (target / "retained.txt").write_bytes(b"retained\n")
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    signals = []

    def identity(pid):
        if pid == 101:
            old.rename(saved)
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(old), str(target)],
                               capture_output=True, check=True)
            else:
                old.symlink_to(target, target_is_directory=True)
            return None
        return {202: "live", 303: "old-child", 404: "live-child"}[pid]

    monkeypatch.setattr(cleanup, "process_start", identity)
    monkeypatch.setattr(cleanup, "_kill_group", signals.append)
    assert cleanup.sweep_orphan_homes(tmp_path) == [{"home": str(old), "process_pid": 303}]
    assert old.exists() and saved.exists() and target.exists()
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert (saved / cleanup.OWNER_MARKER).is_file() and signals == []
    assert str(old) in caplog.text and "retained for manual cleanup" in caplog.text


async def test_orphan_report_leaves_dead_owner_home_and_live_child_untouched(tmp_path, monkeypatch, caplog):
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                                                start_new_session=os.name != "nt")
    home = tmp_path / "work-dead"
    birth = cleanup.process_start(proc.pid)
    mark(home, process_pid=proc.pid, process_start=birth)
    original = cleanup.process_start
    signals = []
    monkeypatch.setattr(cleanup, "process_start", lambda pid: None if pid == 101 else original(pid))
    monkeypatch.setattr(cleanup, "_kill_group", signals.append)
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    try:
        assert cleanup.sweep_orphan_homes(tmp_path) == [{"home": str(home), "process_pid": proc.pid}]
        assert proc.returncode is None and cleanup.process_start(proc.pid) == birth and signals == []
        assert {path.name: path.read_bytes() for path in home.iterdir()} == before
        assert str(proc.pid) in caplog.text and "Orphan controller home" in caplog.text
    finally:
        proc.kill()
        await proc.wait()


async def test_retry_after_process_exit_does_not_signal_reused_pid_and_clears_resources(tmp_path, monkeypatch):
    signals = []
    monkeypatch.setattr(core, "_kill_group", signals.append)
    runtime = core.CodexRuntime(codex_executable="not-executed")
    runtime._temporary_home = tempfile.TemporaryDirectory(prefix="work-", dir=tmp_path)
    runtime._home = home = Path(runtime._temporary_home.name)

    async def wait():
        return 0

    proc = SimpleNamespace(pid=90101, returncode=None, wait=wait, _runtime_start="original")
    runtime._proc = proc
    runtime._reader_task = asyncio.create_task(asyncio.Event().wait())
    background = asyncio.create_task(asyncio.Event().wait())
    runtime._background.add(background)
    runtime._peer_dispatches = {asyncio.create_task(asyncio.Event().wait())}
    dispatches = list(runtime._peer_dispatches)
    runtime._sessions["session"] = object()
    pending = asyncio.get_running_loop().create_future()
    runtime._pending[1] = pending

    async def stop(_proc):
        proc.returncode = 0

    runtime._stop_process = stop
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runtime.close(), 0.02)
    assert runtime._proc is proc and home.exists()
    monkeypatch.setattr(core, "process_start", lambda pid: "replacement")
    await runtime.close()
    assert signals == [] and not home.exists() and runtime._home is None
    assert runtime._proc is runtime._reader_task is runtime._temporary_home is None
    assert runtime._sessions == runtime._pending == {} and runtime._background == set()
    assert background.cancelled() and pending.cancelled()
    assert runtime._peer_dispatches == set() and all(task.cancelled() for task in dispatches)


@pytest.mark.parametrize("state", ["reaped", "reused"])
async def test_force_close_rechecks_process_after_thread_dispatch(monkeypatch, state):
    signals = []
    proc = SimpleNamespace(pid=90101, returncode=None, _runtime_start="original")

    async def wait():
        proc.returncode = 0

    proc.wait = wait

    async def dispatched(function):
        if state == "reaped":
            proc.returncode = 0
        else:
            monkeypatch.setattr(core, "process_start", lambda pid: "replacement")
        function()

    monkeypatch.setattr(asyncio, "to_thread", dispatched)
    monkeypatch.setattr(core, "process_start", lambda pid: "original")
    monkeypatch.setattr(core, "_kill_group", signals.append)
    runtime = core.CodexRuntime(codex_executable="not-executed")
    runtime._proc, runtime._close_started = proc, True
    await runtime.close()
    assert signals == [] and runtime._proc is None


@pytest.mark.parametrize("cancel_pass", [1, 2])
async def test_cleanup_preserves_outer_cancellation_during_each_pass(monkeypatch, cancel_pass):
    monkeypatch.setattr(cleanup, "CLEANUP_BUDGET_SECONDS", 0.2)
    started, release = asyncio.Event(), asyncio.Event()
    passes, cleared = [], []

    async def close():
        passes.append(True)
        if len(passes) == cancel_pass:
            started.set()
            await release.wait()
        elif len(passes) == 1:
            raise TimeoutError()
        cleared.append(True)

    task = asyncio.create_task(cleanup.close_runtime(SimpleNamespace(close=close)))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 1)
    assert caught.value.cleanup_completed is True and cleared and len(passes) == 2


@pytest.mark.parametrize("cancel_pass", [1, 2])
async def test_observer_records_cancellation_after_bounded_cleanup(tmp_path, monkeypatch, cancel_pass):
    monkeypatch.setattr(cleanup, "CLEANUP_BUDGET_SECONDS", 0.2)
    started, release = asyncio.Event(), asyncio.Event()

    class CancelDuringClose(FakeTransport):
        passes = 0

        async def close(self):
            self.passes += 1
            if self.passes == cancel_pass:
                started.set()
                await release.wait()
            elif self.passes == 1:
                raise TimeoutError()
            await super().close()

    task = asyncio.create_task(observe(tmp_path, runtime_class=CancelDuringClose))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    result, runtime, _ = await asyncio.wait_for(task, 1)
    assert runtime.passes == 2 and runtime._sessions == {} and runtime._home is None
    assert result["runtime_closed"] and result["termination_kind"] == "infrastructure_incomplete"
    assert any("CancelledError" in failure for failure in result["infrastructure_failures"])


async def test_cancelled_cleanup_preserves_cancellation_when_retry_times_out(monkeypatch):
    monkeypatch.setattr(cleanup, "CLEANUP_BUDGET_SECONDS", 0.04)
    started = asyncio.Event()
    passes = []

    async def close():
        passes.append(True)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cleanup.close_runtime(SimpleNamespace(close=close)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 1)
    assert caught.value.cleanup_completed is False
    assert isinstance(caught.value.__cause__, TimeoutError) and len(passes) == 2


def test_default_compaction_does_not_promote_recording_fields():
    assert record([event("item/started", item={"id": "c", "type": "contextCompaction"})], enabled=False) == {}


def test_compaction_usage_estimate_is_separate_from_response_calls():
    first, second, estimate = usage(100, 20, 8, 3), usage(30, 10, 4, 1), usage(0, 0, 0, 0)
    estimate["totalTokens"] = 20
    events = [event("rawResponse/completed", responseId="r1", model="served-1", usage=first),
              event("thread/tokenUsage/updated", tokenUsage={"last": first, "total": first}),
              event("item/started", item={"id": "c", "type": "contextCompaction"}),
              event("thread/tokenUsage/updated", tokenUsage={"last": estimate, "total": first}),
              event("item/completed", item={"id": "c", "type": "contextCompaction"}),
              event("rawResponse/completed", responseId="r2", model="served-2", usage=second),
              event("thread/tokenUsage/updated", tokenUsage={"last": second, "total": usage(130, 30, 12, 4)})]
    recording = record(events)["runtime_recording"]
    calls = recording["model_calls"]
    assert len(calls) == 2 and sum(call["input_tokens"] for call in calls) == 130
    assert [(call["response_id"], call["served_model"]) for call in calls] == [("r1", "served-1"), ("r2", "served-2")]
    assert [call["usage_notification_event_index"] for call in calls] == [1, 6]
    assert recording["context_estimates"] == [{"raw_event_index": 3, "response_id": None,
        "context_total_tokens": 20, "model_context_window": None, "input_tokens": 0,
        "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}]


@pytest.mark.parametrize("usage_first", [True, False])
def test_usage_without_completion_cannot_create_call_or_fill_unidentified_usage(usage_first):
    notification = event("thread/tokenUsage/updated", tokenUsage={"last": usage(70, 10, 4, 1)})
    completion = event("rawResponse/completed", responseId="r1", model="served-1")
    events = [notification, completion] if usage_first else [completion, notification]
    recording = record(events)["runtime_recording"]
    assert len(recording["model_calls"]) == 1
    assert recording["model_calls"][0]["input_tokens"] is None
    assert len(recording["context_estimates"]) == 1
    assert record([notification])["runtime_recording"]["model_calls"] == []


def test_response_identity_matches_usage_in_either_order_and_deduplicates_completion():
    first, second = usage(100, 20, 8, 3), usage(30, 10, 4, 1)
    events = [event("thread/tokenUsage/updated", responseId="r2", tokenUsage={"last": second}),
              event("rawResponse/completed", responseId="r1", model="served-1"),
              event("thread/tokenUsage/updated", responseId="r1", tokenUsage={"last": first}),
              event("rawResponse/completed", responseId="r2", model="served-2"),
              event("rawResponse/completed", responseId="r1", model="served-1")]
    recording = record(events)["runtime_recording"]
    assert [(call["response_id"], call["input_tokens"], call["served_model"])
            for call in recording["model_calls"]] == [("r1", 100, "served-1"), ("r2", 30, "served-2")]
    assert recording["context_estimates"] == []


def test_context_estimate_cannot_pair_with_future_response_with_identical_usage():
    first, second = usage(100, 20, 8, 3), usage(30, 10, 4, 1)
    events = [event("rawResponse/completed", responseId="r1", usage=first),
              event("thread/tokenUsage/updated", tokenUsage={"last": first}),
              event("thread/tokenUsage/updated", tokenUsage={"last": second}),
              event("rawResponse/completed", responseId="r2", usage=second),
              event("thread/tokenUsage/updated", tokenUsage={"last": second})]
    recording = record(events)["runtime_recording"]
    assert [call["usage_notification_event_index"] for call in recording["model_calls"]] == [1, 4]
    assert [estimate["raw_event_index"] for estimate in recording["context_estimates"]] == [2]
    assert sum(call["input_tokens"] for call in recording["model_calls"]) == 130


@pytest.mark.parametrize("location", ["params", "item", "absent"])
async def test_hard_limit_compaction_is_recorded_with_option_on_and_context_source(tmp_path, location):
    last = usage(270000, 0, 2000, 1)
    item = {"id": "hard-limit", "type": "contextCompaction"}
    params = {"item": item}
    if location == "params":
        params["contextTokens"] = 272000
    elif location == "item":
        item["contextTokens"] = 272000
    result, runtime, _ = await observe(tmp_path, options=RuntimeOptions(disable_auto_compaction=True), steps=[
        ("call_usage", {"last": last, "total": last, "modelContextWindow": 272000}),
        ("raw", "item/started", params), ("raw", "item/completed", params), ("message", "Complete.")])
    assert "model_auto_compact_token_limit=9223372036854775807" in runtime._command()
    assert 'model_auto_compact_token_limit_scope="body_after_prefix"' in runtime._command()
    assert "model_post_turn_compact_threshold_percent=0" in runtime._command()
    assert not result["infrastructure_failures"]
    compactions = result["runtime_recording"]["compaction_events"]
    assert len(compactions) == 2
    for compaction in compactions:
        assert compaction["context_tokens"] == (270000 if location == "absent" else 272000)
        assert compaction["context_size_source"] == {"params": "notification.contextTokens",
            "item": "notification.item.contextTokens", "absent": "last_observed_input_tokens"}[location]
        if location == "absent":
            assert compaction["context_usage_event_index"] is not None
            assert compaction["context_usage_source"] == "thread/tokenUsage/updated.last.inputTokens"
        else:
            assert compaction["context_usage_event_index"] is None
            assert compaction["context_usage_source"] is None


@pytest.mark.parametrize("text", ['api_key="consistency-token"', "password='consistency-token'",
    '{"access_token": "consistency-token"}', '{"refresh_token": "consistency-token"}',
    'OPENAI_API_KEY="consistency-token"', '{"OPENAI_API_KEY": "consistency-token"}',
    'OPENAI_API_KEY=consistency-token',
    'client_secret = "consistency-token with spaces, commas and \\"quotes\\""'])
@pytest.mark.parametrize("location", ["instructions", "environment"])
def test_quoted_and_serialized_credentials_are_redacted_before_hashing(text, location):
    def capture(value):
        if location == "instructions":
            return {"instructions": value}
        return {"input": [{"role": "user", "content": [{"type": "input_text",
            "text": "<environment_context>\n" + value + "\n</environment_context>"}]}]}

    recording = probe_record(capture(text))
    assert "consistency-token" not in json.dumps(recording)
    assert "[redacted]" in json.dumps(recording)
    assert recording == probe_record(capture(text.replace("consistency-token", "different-token")))
