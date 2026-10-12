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


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("state", ["exited", "replaced", "running", "exit_during_check"])
async def test_second_close_after_timeout_sends_no_signal_and_clears_resources(tmp_path, monkeypatch,
                                                                            platform, state):
    signals, stops = [], []
    owners = {90101: "original"}

    class Process:
        pid = 90101
        code = None
        check_exit = False

        @property
        def returncode(self):
            observed = self.code
            if self.check_exit:
                # Exit and PID reuse before Python publishes the observed exit.
                self.check_exit = False
                self.code = 0
                owners[self.pid] = "replacement"
            return observed

        def kill(self):
            signals.append("kill")

        def terminate(self):
            signals.append("terminate")

        def send_signal(self, sig):
            signals.append(sig)

        async def wait(self):
            pytest.fail("second close waited for the process")

    runtime = core.CodexRuntime(codex_executable="not-executed")
    runtime._temporary_home = tempfile.TemporaryDirectory(prefix="work-", dir=tmp_path)
    runtime._home = home = Path(runtime._temporary_home.name)
    runtime._proc = proc = Process()
    runtime._reader_task = reader = asyncio.create_task(asyncio.Event().wait())
    background = asyncio.create_task(asyncio.Event().wait())
    runtime._background.add(background)
    runtime._peer_dispatches = {asyncio.create_task(asyncio.Event().wait())}
    dispatches = list(runtime._peer_dispatches)
    runtime._sessions["session"] = object()
    pending = asyncio.get_running_loop().create_future()
    runtime._pending[1] = pending

    async def stop(stopped):
        stops.append(stopped)
        await asyncio.Event().wait()

    runtime._stop_process = stop
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runtime.close(), 0.02)
    assert runtime._proc is proc and home.exists()
    if state == "exited":
        proc.code = 0
    elif state == "replaced":
        owners[proc.pid] = "replacement"
    elif state == "exit_during_check":
        proc.check_exit = True
    monkeypatch.setattr(core, "os", SimpleNamespace(name=platform,
        kill=lambda *args: signals.append(("kill", args)),
        killpg=lambda *args: signals.append(("killpg", args))))
    monkeypatch.setattr(core, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9,
        pidfd_send_signal=lambda *args: signals.append(("pidfd", args))))
    monkeypatch.setattr(cleanup, "process_start", lambda pid: pytest.fail("second close read a PID identity"))

    if state == "exited":
        await runtime.close()
        assert runtime._proc is None and not runtime._close_started
    else:
        with pytest.raises(asyncio.TimeoutError) as caught:
            await runtime.close()
        assert str(caught.value) == "" and runtime._proc is proc and runtime._close_started
    assert signals == [] and stops == [proc]
    assert not home.exists() and runtime._home is runtime._temporary_home is runtime._reader_task is None
    assert runtime._sessions == runtime._pending == {} and runtime._background == set()
    assert reader.cancelled() and background.cancelled() and pending.cancelled()
    assert runtime._peer_dispatches == set() and all(task.cancelled() for task in dispatches)
    assert owners[proc.pid] == ("replacement" if state in {"replaced", "exit_during_check"} else "original")
    # A later observation of exit can finish an incomplete close without signals.
    proc.code = 0
    await runtime.close()
    assert runtime._proc is None and not runtime._close_started and signals == [] and stops == [proc]


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("outcome", ["success", "spawn_failure", "marker_failure"])
async def test_launch_opens_no_extra_descriptor_or_reaper_even_on_failure(tmp_path, monkeypatch,
                                                                        platform, outcome):
    order, descriptors, waits = [], [], []
    proc = SimpleNamespace(pid=90101, returncode=None)

    async def wait():
        waits.append(True)
        return proc.returncode

    async def launch(*args, **kwargs):
        order.append("launch")
        if outcome == "spawn_failure":
            raise OSError("launch failed")
        return proc

    def mark_home(home, **process):
        assert home == tmp_path
        order.append("mark")
        if process:
            assert process == {"process_pid": proc.pid}
            if outcome == "marker_failure":
                raise OSError("marker failed")

    def descriptor(*args):
        descriptors.append(args)
        return 42

    proc.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(core, "os", SimpleNamespace(name=platform, environ={}, pidfd_open=descriptor,
                                                   open=descriptor, close=descriptor))
    monkeypatch.setattr(core, "mark_home", mark_home)
    runtime = core.CodexRuntime(codex_executable="not-executed")
    if outcome == "success":
        assert await runtime._launch(home=tmp_path) is proc
    else:
        with pytest.raises(OSError, match="failed"):
            await runtime._launch(home=tmp_path)
    await asyncio.sleep(0)
    assert order == (["mark", "launch"] if outcome == "spawn_failure" else ["mark", "launch", "mark"])
    assert descriptors == waits == [] and runtime._background == set()
    assert "_runtime_pidfd" not in vars(proc) and "_runtime_start" not in vars(proc)


@pytest.mark.parametrize("exited", [True, False])
async def test_second_close_records_process_exit_or_incomplete_cleanup(tmp_path, monkeypatch, exited):
    monkeypatch.setattr(cleanup, "CLEANUP_BUDGET_SECONDS", 0.1)
    signals = []
    proc = SimpleNamespace(pid=90101, returncode=None,
                           kill=lambda: signals.append("kill"), terminate=lambda: signals.append("terminate"),
                           send_signal=lambda sig: signals.append(sig))

    class RetryClose(FakeTransport):
        passes = 0

        async def _stop_process(self, stopped):
            assert stopped is proc
            try:
                await asyncio.Event().wait()
            finally:
                if exited:
                    proc.returncode = 0

        async def close(self):
            self.passes += 1
            if self.passes == 1:
                self._proc = proc
            await super().close()

    result, runtime, _ = await observe(tmp_path, runtime_class=RetryClose)
    assert runtime.passes == 2 and signals == [] and result["runtime_closed"] is exited
    assert runtime._sessions == runtime._pending == {} and runtime._background == set()
    assert runtime._home is runtime._temporary_home is runtime._reader_task is None
    if exited:
        assert runtime._proc is None and not result["infrastructure_failures"]
        assert result["termination_kind"] == "natural_end"
    else:
        assert runtime._proc is proc and runtime._close_started
        assert result["termination_kind"] == "infrastructure_incomplete"
        assert "runtime cleanup incomplete: TimeoutError: " in result["infrastructure_failures"]
        proc.returncode = 0
        await core.CodexRuntime.close(runtime)
    assert signals == []


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


@pytest.mark.parametrize("key", ["password", "token", "key", "secret"])
@pytest.mark.parametrize("delimiter", [",", ";", "}", "]"])
@pytest.mark.parametrize("location", ["instructions", "environment"])
def test_bare_credential_delimiters_are_fully_redacted_before_hashing(key, delimiter, location):
    def capture(prefix, suffix):
        text = key + "=" + prefix + delimiter + suffix + " retained-text"
        if location == "instructions":
            return {"instructions": text}
        return {"input": [{"role": "user", "content": [{"type": "input_text",
            "text": "<environment_context>\n" + text + "\n</environment_context>"}]}]}

    recording = probe_record(capture("consistency-prefix", "consistency-suffix"))
    assert "consistency-" not in json.dumps(recording)
    assert key + "=[redacted] retained-text" in json.dumps(recording)
    assert recording == probe_record(capture("different-prefix", "different-suffix"))


@pytest.mark.parametrize("location", ["instructions", "environment"])
def test_adjacent_serialized_quoted_values_keep_structure_after_redaction(location):
    def capture(value):
        text = '{"password": "' + value + '", "retained": ["value"]}'
        if location == "instructions":
            return {"instructions": text}
        return {"input": [{"content": [{"text": "<environment_context>\n" + text
                                      + "\n</environment_context>"}]}]}

    recording = probe_record(capture("consistency-prefix,consistency-suffix"))
    text = (recording["request_shape"]["instructions"] if location == "instructions" else
            recording["environment_frame"][0]["content"][0]["text"].splitlines()[1])
    assert json.loads(text) == {"password": "[redacted]", "retained": ["value"]}
    assert recording == probe_record(capture("different-prefix;different-suffix"))


@pytest.mark.parametrize("text", ["Authorization: Basic Y29uc2lzdGVuY3k6dmFsdWU=",
                                 '\"Authorization\": \"Basic Y29uc2lzdGVuY3k6dmFsdWU=\"'])
def test_authorization_scheme_value_is_redacted(text):
    assert "Y29uc2lzdGVuY3k6dmFsdWU=" not in json.dumps(probe_record({"instructions": text}))
