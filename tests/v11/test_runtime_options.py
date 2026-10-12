"""WP1 uses only the fake transport and local dummy processes, never a provider."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_auth_bench import runtime_cleanup
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, phase
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.closure import closure_outputs, record_delivery
from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
from swarm_auth_bench.peer_reporting_v11.live_runtime import (
    _Controller,
    run_live_observer,
    validate_preflight,
)
from swarm_auth_bench.peer_reporting_v11.review import build_review_bundle
from swarm_auth_bench.peer_reporting_v11.score import score_trial
from swarm_auth_bench.runtime import CodexRuntime, RuntimeOptions, RuntimeProtocolError
from swarm_auth_bench.runtime_recording import probe_record

from .live_fakes import (
    FakeTransport,
    Harness,
    caps_record,
    compat_fixture,
    compat_root,
    fake_bundle,
    fake_version,
    qualifier_steps,
    run_phase,
)
from .pressure_fakes import labels, pressure_fixture
from .runtime_recording_bytes import default_recording_bytes
from .test_live_runtime_v11 import fixture

# Generated from the original runtime and fake transport at 0630195, under
# CPython 3.11.15 and 3.12.12, with runtime_recording_bytes.py. Both captures fix
# clocks, inline thread completions and the thread-request cwd. Python 3.12
# interleaves tool item notifications before tool_requested, changing the bytes
# and controller checkpoint hash; compare only within the same Python minor.
# To regenerate, create a separate checkout of 0630195 outside this repository:
#   git worktree add --detach <baseline-checkout> 0630195
#   uv run --offline --extra dev python tests/v11/runtime_recording_bytes.py --checkout <baseline-checkout>
# For 3.11, set UV_PROJECT_ENVIRONMENT=.venv311 and add
#   --python C:\Users\luism\miniconda3\python.exe
# to uv run. Remove the scratch checkout with git worktree remove afterwards.
PRE_WP1_RECORDING_SHA256 = {
    (3, 11): "e6b2d15f591d03eb4926ff12ed8694eddf9c07b51eeeba2c1f55bbc54d30caee",
    (3, 12): "2ddcf3ad83341083c2fa24db497fd4cd40cabe568bc9e8855050032f08875c7c",
}


async def observe(tmp_path, *, options=RuntimeOptions(), steps=None, sample=None, runtime_class=FakeTransport,
                  caps=None, bundle=None):
    sample, bundle = sample or fixture(), bundle or fake_bundle()
    home = tmp_path / "home"
    home.mkdir(parents=True)
    runtime = runtime_class("gpt-6-sol", "low", steps or qualifier_steps(sample), home, options=options)
    caps = caps or lane_caps(caps_record(), "smoke", 1)
    qualification = await live.manifest_preflight(runtime, runtime.model, caps, reasoning_effort="low",
                                                 bundle=bundle, version_reader=fake_version)
    result = await run_live_observer(sample, "Exact instructions.", directory=tmp_path / "attempt",
                                     attempt_id="attempt-1", requested_model=runtime.model, reasoning_effort="low",
                                     world_mode="normal", bundle=bundle, caps=caps, qualification=qualification,
                                     runtime=runtime)
    return result, runtime, qualification


@pytest.mark.parametrize("changes", [{"reasoning_summary": "auto"}, {"reasoning_summary": "concise"},
                                    {"record_calls": 1}, {"home_prefix": "../work-"}])
def test_options_reject_unsupported_settings(changes):
    with pytest.raises(ValueError):
        RuntimeOptions(**changes)


async def test_summary_requested_attested_pinned_and_archived_with_different_terminal_copy(tmp_path):
    options = RuntimeOptions(reasoning_summary="detailed", record_calls=True)
    result, runtime, qualification = await observe(tmp_path, options=options, steps=[
        ("reasoning", ["Standalone summary."], {"summary": ["Terminal summary."], "content": ["different"]}),
        ("usage", 20), ("message", "Final answer.")])
    assert not result["infrastructure_failures"] and result["termination_kind"] == "natural_end"
    assert qualification["reasoning_summary"] == "detailed"
    assert qualification["runtime_options"] == asdict(options)
    assert runtime.probe_requests[0]["reasoning"]["summary"] == "detailed"
    turn = next(request["params"] for request in runtime.requests if request["method"] == "turn/start")
    assert turn["summary"] == "detailed"
    assert [entry["item"]["summary"] for entry in result["reasoning_items"]] == [
        ["Standalone summary."], ["Terminal summary."]]
    reconciliation = next(event for event in result["events"] if event["kind"] == "turn_notifications_reconciled")
    assert reconciliation["unresolved"] == reconciliation["contradicted"] == []
    assert [output["text"] for output in result["observer_outputs"]] == ["Final answer."]


@pytest.mark.parametrize("terminal", [{"summary": ["Different summary."]}, {"id": "unseen-reasoning"}])
async def test_default_reasoning_copies_keep_the_original_reconciliation_rule(tmp_path, terminal):
    result, _, _ = await observe(tmp_path, steps=[("reasoning", ["Standalone summary."], terminal),
                                                 ("usage", 20), ("message", "Final answer.")])
    assert result["termination_kind"] == "infrastructure_incomplete"
    reconciliation = next(event for event in result["events"] if event["kind"] == "turn_notifications_reconciled")
    assert reconciliation["unresolved"] and "reasoning_items" not in result


@pytest.mark.parametrize("summary", [None, "auto", "concise"])
def test_probe_refuses_summary_mismatch(summary):
    runtime = CodexRuntime(options=RuntimeOptions(reasoning_summary="detailed"))
    with pytest.raises(RuntimeProtocolError, match="reasoning summary"):
        runtime._attest_manifest({"model": runtime.model, "tools": [], "reasoning": {"summary": summary}}, [])


def test_probe_recording_refuses_missing_environment_evidence():
    runtime = CodexRuntime(options=RuntimeOptions(record_calls=True))
    with pytest.raises(RuntimeProtocolError, match="environment frame is unavailable"):
        runtime._attest_manifest({"model": runtime.model, "tools": []}, [])


async def test_manifest_loopback_capture_uses_summary_and_neutral_actual_probe_home():
    class LoopbackProbe(CodexRuntime):
        async def _launch(self, *, home, env_extra=None, overrides=()):
            self.probe_home = home
            self.url = next(value for value in overrides if "base_url=" in value)
            return object()

        async def _send(self, message, proc=None):
            pass

        async def _stop_process(self, proc):
            pass

        async def _probe_call(self, proc, request_id, method, params):
            if method == "thread/start":
                self.thread_params = params
                return {"thread": {"id": "probe-thread"}}
            if method == "turn/start":
                assert params["summary"] == "detailed"
                port = int(self.url.split("127.0.0.1:", 1)[1].split("/", 1)[0])
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                request = {"model": self.model, "tools": [], "reasoning": {"summary": params["summary"]},
                           "instructions": self.thread_params["baseInstructions"], "input": [{"role": "user",
                           "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>"
                                        + self.thread_params["cwd"] + "</cwd>\n</environment_context>"}]}]}
                body = json.dumps(request).encode("utf-8")
                writer.write(b"POST /v1/responses HTTP/1.1\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\n\r\n" + body)
                await writer.drain()
                await reader.read()
                writer.close()
                await writer.wait_closed()
            return {}

    runtime = LoopbackProbe(codex_executable="not-executed", options=RuntimeOptions(
        reasoning_summary="detailed", home_prefix="work-", record_calls=True))
    attestation = await runtime._probe_manifest([])
    assert runtime.probe_home.name.startswith("work-") and not runtime.probe_home.exists()
    assert attestation["reasoning_summary"] == "detailed"
    assert attestation["request_recording"]["request_shape"]["reasoning"]["summary"] == "detailed"
    assert str(runtime.probe_home) in json.dumps(attestation["request_recording"]["environment_frame"]).replace(
        "\\\\", "\\")


async def test_preflight_refuses_summary_and_options_mismatch(tmp_path):
    _, runtime, qualification = await observe(tmp_path, options=RuntimeOptions(reasoning_summary="detailed"))
    runtime.options = RuntimeOptions()
    with pytest.raises(ValueError, match="runtime options differ"):
        validate_preflight(runtime.model, lane_caps(caps_record(), "smoke", 1), qualification, runtime,
                           reasoning_effort="low", bundle=fake_bundle())
    runtime.options = RuntimeOptions(reasoning_summary="detailed")
    qualification["reasoning_summary"] = "concise"
    with pytest.raises(ValueError, match="preflight does not match"):
        validate_preflight(runtime.model, lane_caps(caps_record(), "smoke", 1), qualification, runtime,
                           reasoning_effort="low", bundle=fake_bundle())


@pytest.mark.parametrize("summary", [None, "concise"])
async def test_turn_start_refuses_summary_mismatch(tmp_path, summary):
    class DifferentSummary(FakeTransport):
        def _turn_params(self, *args, **kwargs):
            return {**super()._turn_params(*args, **kwargs), "summary": summary}

    result, runtime, _ = await observe(tmp_path, options=RuntimeOptions(reasoning_summary="detailed"),
                                       runtime_class=DifferentSummary)
    assert "turn/start" not in runtime.methods
    assert result["termination_kind"] == "infrastructure_incomplete"
    assert any("reasoning summary differs" in failure for failure in result["infrastructure_failures"])


def usage(input_tokens, cached, output, reasoning):
    return {"inputTokens": input_tokens, "cachedInputTokens": cached, "outputTokens": output,
            "reasoningOutputTokens": reasoning, "totalTokens": input_tokens + output}


@pytest.mark.parametrize("raw_first", [True, False])
async def test_per_call_usage_served_model_compaction_and_probe_hashes(tmp_path, raw_first):
    first, second = usage(100, 20, 8, 3), usage(140, 95, 15, 6)
    pairs = []
    for number, last in enumerate((first, second), 1):
        notification = ("call_usage", {"last": last, "total": usage(9000 + number, 0, 0, 0),
                                       "modelContextWindow": 272000})
        raw = ("response", {"responseId": f"response-{number}", "usage": last,
                            "model": f"served-model-{number}"})
        pairs += [raw, notification] if raw_first else [notification, raw]
    result, runtime, qualification = await observe(tmp_path, options=RuntimeOptions(record_calls=True),
                                                    steps=[*pairs, ("compaction",), ("message", "Complete.")])
    recording = result["runtime_recording"]
    assert not result["infrastructure_failures"]
    assert [{key: call[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")}
            for call in recording["model_calls"]] == [
        {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 8, "reasoning_tokens": 3},
        {"input_tokens": 140, "cached_input_tokens": 95, "output_tokens": 15, "reasoning_tokens": 6}]
    assert [call["served_model"] for call in recording["model_calls"]] == ["served-model-1", "served-model-2"]
    assert [event["context_tokens"] for event in recording["compaction_events"]] == [140, 140]
    assert all(event["context_size_source"] == "last_observed_input_tokens"
               for event in recording["compaction_events"])
    assert all(event["model_context_window"] == 272000 for event in recording["compaction_events"])
    probe = qualification["manifest_attestation"]["request_recording"]
    assert recording["manifest_probes"] == [probe]
    assert probe == probe_record(runtime.probe_requests[0])
    for key in ("request_shape", "environment_frame"):
        expected = hashlib.sha256(json.dumps(probe[key], sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True).encode("utf-8")).hexdigest()
        assert probe[key + "_sha256"] == expected


async def test_compactions_recorded_without_per_call_option(tmp_path):
    last = usage(35, 0, 4, 1)
    result, _, _ = await observe(tmp_path, options=RuntimeOptions(disable_auto_compaction=True),
                                steps=[("call_usage", {"last": last, "total": last}),
                                                 ("compaction",), ("message", "Complete.")])
    assert "model_calls" not in result["runtime_recording"]
    assert result["runtime_recording"]["compaction_events"][0]["context_tokens"] == 35


async def test_neutral_home_and_probe_environment_frame(tmp_path):
    result, runtime, _ = await observe(tmp_path, options=RuntimeOptions(home_prefix="work-", record_calls=True))
    thread = next(request["params"] for request in runtime.requests if request["method"] == "thread/start")
    assert Path(thread["cwd"]).name.startswith("work-")
    probe = result["runtime_recording"]["manifest_probes"][0]
    assert "work-probe" in json.dumps(probe["environment_frame"])
    assert "swarm-codex" not in json.dumps(probe["environment_frame"])


def test_probe_recording_removes_secrets_and_retains_only_requested_shape():
    capture = {"instructions": "api_key=secret-value Bearer hidden-token sk-abc123", "authorization": "hidden",
               "reasoning": {"effort": "low", "summary": "detailed", "secret": "hidden"},
               "text": {"verbosity": "low", "format": {"secret": "hidden"}},
               "input": [{"role": "user", "content": [{"text": "<environment_context>secret=hidden</environment_context>"}]}]}
    record = probe_record(capture)
    assert "hidden" not in json.dumps(record) and "secret-value" not in json.dumps(record)
    assert "sk-abc123" not in json.dumps(record) and "authorization" not in record["request_shape"]
    assert record["request_shape"]["text"] == {"verbosity": "low"}


def test_compaction_option_uses_pinned_client_fields_without_changing_defaults():
    # rust-v0.158.0 core/src/session/context_window.rs: BodyAfterPrefix reads
    # model_auto_compact_token_limit directly; the full context guard remains.
    # protocol/src/openai_models.rs: total scope clamps to 90% of the window.
    default = CodexRuntime(codex_executable="not-executed")
    configured = CodexRuntime(codex_executable="not-executed",
                              options=RuntimeOptions(disable_auto_compaction=True))
    assert not any("compact" in argument for argument in default._command())
    command = configured._command()
    assert "model_auto_compact_token_limit=9223372036854775807" in command
    assert 'model_auto_compact_token_limit_scope="body_after_prefix"' in command
    assert "model_post_turn_compact_threshold_percent=0" in command


async def test_default_requests_preflight_and_attempt_have_no_added_option_fields(tmp_path):
    result, runtime, qualification = await observe(tmp_path)
    assert runtime._turn_params("thread", "packet") == {
        "threadId": "thread", "input": [{"type": "text", "text": "packet"}], "environments": [],
        "sandboxPolicy": {"type": "readOnly"}, "approvalPolicy": "never", "effort": "low"}
    assert qualification["manifest_attestation"] == {
        "model": runtime.model, "tools": fake_bundle().attested_tools, "verified": True}
    assert "runtime_options" not in qualification and "reasoning_summary" not in qualification
    assert "runtime_options" not in result["runtime_metadata"]
    assert "reasoning_items" not in result and "runtime_recording" not in result
    assert runtime.options.home_prefix == "swarm-codex-controller-"
    assert runtime.options.manifest_prefix == "swarm-codex-manifest-"


async def test_default_recording_bytes_match_pre_wp1_across_all_levels(tmp_path, monkeypatch):
    python_minor = sys.version_info[:2]
    baseline = PRE_WP1_RECORDING_SHA256.get(python_minor)
    if baseline is None:
        pytest.skip(f"No pre-WP1 recording baseline for Python {python_minor[0]}.{python_minor[1]}; "
                    "baselines were generated only for Python 3.11 and 3.12")
    encoded = await default_recording_bytes(tmp_path, monkeypatch)
    assert hashlib.sha256(encoded).hexdigest() == baseline


async def test_full_drain_still_has_its_own_cleanup_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cleanup, "CLEANUP_BUDGET_SECONDS", 0.2)
    original = _Controller.drain_runtime_events
    drain_count = 0

    async def slow_drain(controller):
        nonlocal drain_count
        drain_count += 1
        if drain_count == 1:
            await asyncio.sleep(0.05)
        await original(controller)

    class SlowClose(FakeTransport):
        async def close(self):
            await asyncio.sleep(0.025)
            await super().close()

    monkeypatch.setattr(_Controller, "drain_runtime_events", slow_drain)
    caps = lane_caps(caps_record(), "smoke", 1) | {"drain_grace_seconds": 0.03}
    result, _, _ = await observe(tmp_path, runtime_class=SlowClose, caps=caps)
    assert result["runtime_closed"] and result["drain_elapsed_seconds"] >= 0.05
    assert not any("runtime cleanup incomplete" in failure for failure in result["infrastructure_failures"])


async def test_cancelled_close_leaves_process_running_cancels_tasks_removes_home_and_clears_sessions(tmp_path,
                                                                                                  monkeypatch):
    runtime = CodexRuntime(codex_executable="not-executed")
    runtime._temporary_home = tempfile.TemporaryDirectory(prefix="work-", dir=tmp_path)
    runtime._home = Path(runtime._temporary_home.name)
    home = runtime._home
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                                                start_new_session=os.name != "nt")
    runtime._proc = proc
    runtime._reader_task = asyncio.create_task(asyncio.Event().wait())
    background = asyncio.create_task(asyncio.Event().wait())
    runtime._background.add(background)
    runtime._sessions["session"] = object()

    async def hang(_proc):
        await asyncio.Event().wait()

    runtime._stop_process = hang
    signals = []
    original_kill = proc.kill
    monkeypatch.setattr(proc, "kill", lambda: signals.append("kill"))
    monkeypatch.setattr(proc, "terminate", lambda: signals.append("terminate"))
    monkeypatch.setattr(proc, "send_signal", lambda sig: signals.append(sig))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(runtime.close(), 0.02)
        assert runtime._proc is proc and home.exists()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(runtime.close(), 5)
        assert proc.returncode is None and not home.exists() and signals == []
        assert background.cancelled() and runtime._sessions == {} and runtime._proc is proc
        assert runtime._home is runtime._temporary_home is runtime._reader_task is None
        original_kill()
        await proc.wait()
        await runtime.close()
        assert runtime._proc is None and signals == []
    finally:
        if proc.returncode is None:
            original_kill()
            await proc.wait()


async def test_first_close_hang_is_retried_within_cleanup_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cleanup, "CLEANUP_BUDGET_SECONDS", 0.1)

    class RetryClose(FakeTransport):
        passes = 0

        async def close(self):
            self.passes += 1
            if self.passes == 1:
                await asyncio.Event().wait()
            await super().close()

    result, runtime, _ = await observe(tmp_path, runtime_class=RetryClose)
    assert runtime.passes == 2 and result["runtime_closed"]
    assert not result["infrastructure_failures"]


@pytest.mark.parametrize("failure", ["hang", "process_running"])
async def test_both_close_passes_failing_preserve_failure_text_and_root_hold(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(runtime_cleanup, "CLEANUP_BUDGET_SECONDS", 0.06)
    root, plan = compat_root(tmp_path / "root")
    sample = compat_fixture(root, plan)
    harness = Harness(tmp_path / "homes", lambda *_: qualifier_steps(sample))
    factory = harness.factory
    attempts = []

    def failing_factory(*args):
        runtime = factory(*args)
        original_close = runtime.close

        async def stop(_proc):
            await asyncio.Event().wait()

        if failure == "process_running":
            runtime._proc = SimpleNamespace(pid=90101, returncode=None)
            runtime._stop_process = stop

        async def hang():
            attempts.append(runtime)
            if failure == "hang":
                await asyncio.Event().wait()
            await original_close()

        runtime.close = hang
        return runtime

    harness.factory = failing_factory
    status = await run_phase(root, plan, harness)
    assert status["holds"] and attempts
    for lane in plan["lanes"]:
        records = list((root / lane["path"] / "attempts").glob("*/attempt.json"))
        for path in records:
            result = read_sealed(path)["observer_result"]
            assert result["termination_kind"] == "infrastructure_incomplete" and not result["runtime_closed"]
            assert "runtime cleanup incomplete: TimeoutError: " in result["infrastructure_failures"]
    assert all(attempts.count(runtime) == 2 for runtime in harness.created)
    if failure == "process_running":
        for runtime in harness.created:
            assert runtime._sessions == {} and runtime._home is None and runtime._close_started
            assert runtime._proc.returncode is None
            runtime._proc.returncode = 0
            await runtime.close()


@pytest.mark.parametrize("prefix", ["swarm-codex-controller-", "work-"])
def test_sweep_reports_dead_owner_and_matching_process_preserves_all_homes(tmp_path, monkeypatch, caplog,
                                                                                prefix):
    dead, alive, unmarked = (tmp_path / (prefix + name) for name in ("dead", "alive", "unmarked"))
    for home in (dead, alive, unmarked):
        home.mkdir()
    (dead / runtime_cleanup.OWNER_MARKER).write_bytes(json.dumps({
        "runner_pid": 101, "runner_start": "old", "process_pid": 303, "process_start": "child"}).encode())
    (alive / runtime_cleanup.OWNER_MARKER).write_bytes(json.dumps({
        "runner_pid": 202, "runner_start": "live"}).encode())
    killed = []
    monkeypatch.setattr(runtime_cleanup, "process_start", lambda pid: {101: None, 202: "live", 303: "child"}[pid])
    monkeypatch.setattr(runtime_cleanup, "_kill_group", killed.append)
    orphans = runtime_cleanup.sweep_orphan_homes(tmp_path)
    assert killed == [] and orphans == [{"home": str(dead.resolve()), "process_pid": 303}]
    assert dead.exists() and alive.exists() and unmarked.exists()
    assert "Orphan controller home" in caplog.text and "retained for manual cleanup" in caplog.text


@pytest.mark.parametrize("birth", ["unknown", "reused"])
def test_sweep_retains_unknown_or_reused_child_identity(tmp_path, monkeypatch, birth):
    home = tmp_path / "work-retained"
    home.mkdir()
    (home / runtime_cleanup.OWNER_MARKER).write_bytes(json.dumps({
        "runner_pid": 101, "runner_start": "old", "process_pid": 303, "process_start": "child"}).encode())
    monkeypatch.setattr(runtime_cleanup, "process_start", lambda pid: None if pid == 101 else birth)
    assert runtime_cleanup.sweep_orphan_homes(tmp_path) == [{"home": str(home), "process_pid": None}]
    assert home.exists()


def test_owner_marker_pins_real_runner_birth(tmp_path):
    runtime_cleanup.mark_home(tmp_path)
    marker = json.loads((tmp_path / runtime_cleanup.OWNER_MARKER).read_bytes())
    assert marker == {"runner_pid": os.getpid(), "runner_start": runtime_cleanup.process_start(os.getpid())}
    assert marker["runner_start"] not in (None, "unknown")


async def test_opt_in_primary_attempt_archive_contains_recordings(tmp_path):
    root, plan = compat_root(tmp_path / "root")
    sample = compat_fixture(root, plan)
    harness = Harness(tmp_path / "homes", lambda *_: [("reasoning", ["Recorded summary."]),
                                                      *qualifier_steps(sample)])
    factory = harness.factory

    def configured_factory(*args):
        runtime = factory(*args)
        runtime.options = RuntimeOptions(reasoning_summary="detailed", record_calls=True)
        return runtime

    harness.factory = configured_factory
    status = await run_phase(root, plan, harness)
    assert not status["holds"]
    archives = list(root.glob("lanes/*/attempts/*/attempt.json"))
    assert len(archives) == len(harness.created) > 0
    for path in archives:
        payload = read_sealed(path)
        assert payload["reasoning_items"] == payload["observer_result"]["reasoning_items"]
        assert payload["runtime_recording"] == payload["observer_result"]["runtime_recording"]
    assert "runtime_cleanup.py" in phase.implementation_hashes()
    assert "runtime_recording.py" in phase.implementation_hashes()


@pytest.mark.parametrize("stored_report", [True, False])
async def test_reasoning_summary_mutation_cannot_change_scores_closure_packets_or_room_notices(tmp_path,
                                                                                           stored_report):
    sample = pressure_fixture(visibility="visible", pressure="neutral")
    bundle = load_bundle().tool_set("peer-reporting-v11-tools-v2")
    focal = sample["truth"]["focal_operation_id"]
    actor = next(operation["actor_id"] for operation in sample["truth"]["operations"]
                 if operation["operation_id"] == focal)
    from .pressure_fakes import report

    steps = [("reasoning", ["Primary summary."])]
    if stored_report:
        steps.append(("tool", "report_incident", report(sample)))
    steps += [("usage", 2000), ("message", "Task complete.")]
    result, _, _ = await observe(tmp_path, sample=sample, bundle=bundle, steps=steps,
                                 options=RuntimeOptions(reasoning_summary="detailed"))
    assert not result["infrastructure_failures"]
    attempt = {**labels(), "attempt_id": result["attempt_id"], "assignment_id": "assignment-1",
               "fixture": sample, "instructions": "Exact instructions.", "primary": True,
               "eligible": True, "exposure_confirmed": True, "execution_kind": "live_model",
               "model_execution_confirmed": True, "termination_kind": result["termination_kind"],
               "tool_requests": result["tool_requests"], "tool_receipts": result["tool_receipts"],
               "observer_outputs": result["observer_outputs"], "report_attempts": [],
               "observed_peer_messages": [], "accepted_reports": [], "pressure_events": [], "task_submissions": [],
               "reasoning_items": result["reasoning_items"]}
    changed = deepcopy(attempt)
    marker = f"unauthorized report {focal} actor {actor} summary-mutation-only"
    for item in changed["reasoning_items"]:
        item["item"]["summary"] = [marker]
    before, after = score_trial(attempt), score_trial(changed)
    assert before["eligibility"]["eligible"] is True
    assert before["endpoints"]["focal_hit"]["value"] is stored_report
    for endpoint in ("focal_hit", "owner_informed", "authorization_engaged"):
        assert before["endpoints"][endpoint] == after["endpoints"][endpoint]
    assert before == after
    assert closure_outputs(attempt["observer_outputs"]) == closure_outputs(changed["observer_outputs"])
    record_delivery(changed["observer_outputs"], pressure=True)
    first = build_review_bundle(attempt, scope="full")
    second = build_review_bundle(changed, scope="full", retained_bindings=first["bindings"],
                                  packet_id=first["packet"]["review_packet_id"])
    assert first["packet"] == second["packet"]
    # The primary-record content hash changes when primary reasoning changes.
    assert {key: value for key, value in first["bindings"].items() if key != "attempt_content_hash"} == {
        key: value for key, value in second["bindings"].items() if key != "attempt_content_hash"}
    assert marker not in json.dumps([changed["observer_outputs"], second["packet"], result["world_state"]["events"]])
