"""Astra review, live layer: copied roots (B1), late transport contradictions (M1), contradictory tool receipts
(M2), export authorization evidence (M4), cleanup reconciliation (m2), and the review plan binding (M3). Each
reproduction from docs/v11/review-astra.md is kept as a regression test. No model or provider call."""

import asyncio
import json
import shutil
import time
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import live_runtime
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review

from .live_fakes import (
    FakeTransport,
    Harness,
    authorization,
    caps_record,
    compat_fixture,
    compat_root,
    fake_bundle,
    guest,
    qualified_root,
    qualifier_steps,
    report_steps,
    review_plan_for,
)
from .live_fakes import run_phase as run
from .test_live_r2 import (
    LUNA_L1,
    READ,
    SERIAL,
    amendment,
    attempt_of,
    build_plan,
    payloads,
    records,
    scripted,
    smoke_root,
    smoke_study,
)


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("astra-compat"))[0]


class TransportHarness(Harness):
    """A harness whose transport class can differ per lane."""

    def __init__(self, root, script_for, transports, *, observe=None):
        super().__init__(root, script_for, observe=observe)
        self.transports = transports

    def factory(self, model, effort):
        home = self.root / f"home-{len(self.created)}"
        home.mkdir(parents=True)
        runtime = self.transports.get((model, effort), FakeTransport)(
            model, effort, self.script_for(model, effort), home, observe=self.observe)
        self.created.append(runtime)
        return runtime


def stop_after(root, count):
    """An observer that writes root/STOP after ``count`` attempts finish (a soft stop)."""
    seen = []

    async def observer(*args, **kwargs):
        result = await live_runtime.run_live_observer(*args, **kwargs)
        seen.append(kwargs["attempt_id"])
        if len(seen) >= count:
            (Path(root) / "STOP").touch()
        return result
    return observer


# B1: a root is accepted only at its registered path, and every start is claimed in the study


async def test_a_copied_smoke_root_cannot_run_or_serve_as_evidence(compat, tmp_path):
    # The reviewer's scenario: copy a prepared smoke root before either copy runs, with one unchanged study and
    # one authorization. The original runs; the copy is refused everywhere.
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    outside, inside = tmp_path / "copy", study / "roots" / "smoke-copy"
    shutil.copytree(root, outside)
    shutil.copytree(root, inside)
    auth = authorization(plan)
    first = await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), auth=auth,
                      compatibility_directories=[compat], study_directory=study)
    assert first["status"] == "complete" and first["live_model_call_starts"] == 12
    for copy in (outside, inside):
        harness = Harness(tmp_path / f"h-{copy.name}", scripted(fixtures))
        with pytest.raises(v11_live.EvidenceError, match="registered path"):
            await run(copy, plan, harness, auth=auth, compatibility_directories=[compat], study_directory=study)
        assert harness.created == [] and records(copy, "attempt_started") == []
        with pytest.raises(v11_live.EvidenceError, match="registered path"):
            v11_live.verify_live_root(copy, bundle=fake_bundle(), study_directory=study)
        with pytest.raises(v11_live.GateError, match="registered path"):
            build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=copy,
                       review_plan=review_plan_for(study))
        with pytest.raises(v11_live.EvidenceError, match="registered path"):
            export_live_review(copy, tmp_path / f"export-{copy.name}", bundle=fake_bundle(), study_directory=study)
    # Every start is claimed in the study-level start ledger and matches the original's journal.
    claims = v11_live.study_start_claims(study)
    starts = {record["data"]["attempt_id"]: record for record in records(root, "attempt_started")}
    assert set(claims) == set(starts)
    assert all(claims[key]["started_record_hash"] == starts[key]["hash"] for key in starts)
    assert all(claims[key]["root_path"] == "roots/smoke-v1" for key in claims)


async def test_a_stale_copy_moved_to_the_registered_path_is_refused_by_the_start_ledger(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    stale = tmp_path / "stale"
    shutil.copytree(root, stale)
    auth = authorization(plan)
    assert (await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), auth=auth,
                      compatibility_directories=[compat], study_directory=study))["live_model_call_starts"] == 12
    moved = tmp_path / "original-moved"
    root.rename(moved)
    stale.rename(root)  # the copy now sits at the registered path, with an empty journal
    harness = Harness(tmp_path / "h2", scripted(fixtures))
    with pytest.raises(v11_live.EvidenceError, match="start ledger"):
        await run(root, plan, harness, auth=auth, compatibility_directories=[compat], study_directory=study)
    assert harness.created == []
    with pytest.raises(v11_live.GateError, match="start ledger"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root,
                   review_plan=review_plan_for(study))
    with pytest.raises(v11_live.EvidenceError, match="start ledger"):
        export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study)


async def test_a_stale_copy_cannot_serve_as_a_prior_root(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    stale = tmp_path / "stale"
    shutil.copytree(root, stale)
    failing = scripted(fixtures, {("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [
        ("raw", "item/started", {"item": {"id": "native-1", "type": "commandExecution"}})]})
    status = await run(root, plan, Harness(tmp_path / "h1", failing), compatibility_directories=[compat],
                       study_directory=study)
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    with pytest.raises(ValueError, match="registered path"):
        build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat],
                   prior_roots=[stale])
    moved = tmp_path / "original-moved"
    root.rename(moved)
    stale.rename(root)
    with pytest.raises(v11_live.EvidenceError, match="start ledger"):
        build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat],
                   prior_roots=[root])
    root.rename(tmp_path / "stale-again")
    moved.rename(root)
    built = build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat],
                       prior_roots=[root])
    assert built[0]["maximum_live_calls"] == 9
    assert set(built[0]["consumed_attempts"]["consumed_attempt_ids"]) == set(v11_live.study_start_claims(study))


async def test_a_copied_compatibility_root_needs_an_authorization_for_its_own_path(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=SERIAL)
    copy = tmp_path / "compat-copy"
    shutil.copytree(root, copy)
    sample = compat_fixture(root, plan)
    auth = authorization(plan, root=root)
    assert auth["root_path"] == str(root.resolve())
    status = await run(root, plan, Harness(tmp_path / "h1", lambda model, effort: qualifier_steps(sample)), auth=auth)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 6
    harness = Harness(tmp_path / "h2", lambda model, effort: qualifier_steps(sample))
    with pytest.raises(ValueError, match="root path"):
        await run(copy, plan, harness, auth=auth)
    without = {key: value for key, value in auth.items() if key not in {"root_path", "seal_hash"}}
    with pytest.raises(ValueError, match="exactly"):
        await run(copy, plan, harness, auth=seal(without))
    assert harness.created == [] and records(copy, "attempt_started") == []


async def test_the_start_is_claimed_before_the_session_and_a_held_claim_refuses_the_start(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    first = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    second = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    claim_path = study / v11_live.START_LEDGER / f"{second}.json"
    foreign = seal({"kind": v11_live.START_CLAIM_KIND, "attempt_id": second, "plan_hash": "0" * 64})
    seen = []

    async def observer(*args, **kwargs):
        seen.append((kwargs["attempt_id"], kwargs["attempt_id"] in v11_live.study_start_claims(study)))
        # Meanwhile another copy claims the next assignment: its start is refused before any session.
        atomic_json(claim_path, foreign)
        return await live_runtime.run_live_observer(*args, **kwargs)

    harness = Harness(tmp_path / "h1", scripted(fixtures))
    status = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study,
                       observer=observer)
    assert seen == [(first, True)]
    assert status["status"] == "held" and any(hold.startswith("lane_halted:gpt-6-sol-xhigh:start_claim_refused")
                                             for hold in status["holds"])
    assert len(harness.created) == 2  # the second runtime ran only its preflight
    refused = records(root, "start_claim_refused")
    assert [record["data"]["attempt_id"] for record in refused] == [second]
    assert payloads(root).keys() == {first}
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["start_claims"]["refused_starts"] == [second]
    assert report["lanes"]["gpt-6-sol-xhigh"]["entries"][0]["status"] == "incomplete_interrupted"
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    again = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert resumed.created == [] and again["status"] == "held"


async def test_a_foreign_claim_of_a_planned_attempt_refuses_the_run_before_any_runtime(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    second = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    claim_path = study / v11_live.START_LEDGER / f"{second}.json"
    claim_path.parent.mkdir(parents=True)
    atomic_json(claim_path, seal({"kind": v11_live.START_CLAIM_KIND, "attempt_id": second, "plan_hash": "0" * 64}))
    harness = Harness(tmp_path / "h1", scripted(fixtures))
    with pytest.raises(v11_live.EvidenceError, match="start ledger"):
        await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    assert harness.created == []


async def test_a_crash_between_the_start_and_its_claim_is_recovered_and_held(compat, tmp_path, monkeypatch):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash()

    with monkeypatch.context() as patched:
        patched.setattr(v11_live, "claim_attempt_start", crash)
        with pytest.raises(Crash):
            await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                      study_directory=study)
    first = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert v11_live.study_start_claims(study) == {}
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["start_claims"]["unclaimed_starts"] == [first]
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    status = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert resumed.created == [] and status["status"] == "held"
    claims = v11_live.study_start_claims(study)
    assert list(claims) == [first] and claims[first]["recovered"] is True
    assert v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)["start_claims"][
        "unclaimed_starts"] == []


def test_a_behavioral_root_is_built_only_under_the_study_roots_directory(compat, tmp_path, capsys, monkeypatch):
    study, _, _ = smoke_study(tmp_path / "study")
    built = build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat])
    for directory in (tmp_path / "smoke-v1", study / "smoke-v1", study / "roots" / "nested" / "smoke-v1"):
        with pytest.raises(ValueError, match="roots"):
            v11_live.prepare_live_root(directory, built, study_directory=study)
    assert v11_live.registered_roots(study) == []
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    monkeypatch.setattr(v11_live, "verify_sealed_study", lambda directory, caps: {"valid": True, "errors": []})
    caps_path = tmp_path / "caps.json"
    atomic_json(caps_path, SERIAL)
    argv = ["build", str(tmp_path / "outside"), "--phase", "smoke", "--caps", str(caps_path), "--revision",
            "smoke-v1", "--study", str(study), "--compatibility", str(compat)]
    assert main(argv) == 2 and "roots" in json.loads(capsys.readouterr().out)["error"]
    argv[1] = str(study / "roots" / "smoke-v1")
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["maximum_live_calls"] == 12
    (registered,) = v11_live.registered_roots(study)
    assert registered["root_path"] == "roots/smoke-v1"


# M1: a contradictory request is an execution failure even after admission closed


class LateConflictTransport(FakeTransport):
    """Immediately before forwarding turn/completed, reuse the read_channel call ID with other arguments."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_calls = {}
        self.injected = None

    async def _server_request(self, message):
        if message.get("method") == "item/tool/call":
            self.seen_calls.setdefault(message["params"]["tool"], deepcopy(message["params"]))
        await super()._server_request(message)

    async def _push(self, method, params):
        if method == "turn/completed" and self.injected is None and "read_channel" in self.seen_calls:
            self.injected = {**self.seen_calls["read_channel"], "arguments": {"after_event_id": None, "limit": 2}}
            await self._server_request({"id": 900, "method": "item/tool/call", "params": self.injected})
        await super()._push(method, params)


async def test_a_late_conflict_after_agent_finish_fails_qualification_and_holds(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=SERIAL)
    sample = compat_fixture(root, plan)
    harness = TransportHarness(tmp_path / "homes", lambda model, effort: qualifier_steps(sample),
                               {("gpt-6-luna", "xhigh"): LateConflictTransport})
    status = await run(root, plan, harness)
    assert len(harness.created) == 1 and status["status"] == "held"
    (payload,) = payloads(root).values()
    result = payload["observer_result"]
    late = [request for request in result["tool_requests"] if request["request_id"] == 900]
    assert len(late) == 1 and late[0]["admitted"] is False and late[0]["result"]["error"] == "idempotency_conflict"
    assert "conflicting duplicate transport call ID" in result["infrastructure_failures"]
    assert result["termination_kind"] == "infrastructure_incomplete" and payload["check"]["passed"] is False
    assert f"execution_check_failure:{payload['attempt_id']}" in status["holds"]


def _late_scripts(root, fixtures, scenario, clock):
    def script(fixture):
        if scenario == "finish":
            return report_steps(fixture)
        if scenario == "tool_cap":
            return [("usage", 1000), *[READ] * 32]
        if scenario == "wall_cap":
            return [("usage", 1000), READ, ("call", lambda: clock.__setitem__(0, clock[0] + 10 ** 6)), READ]
        if scenario == "token_cap":
            return [READ, ("usage", 60000), READ]
        assert scenario == "hard_stop"
        return [READ, ("usage", 1000), ("call", lambda: (root / "HARD_STOP").touch()), ("wait", asyncio.Event())]
    return scripted(fixtures, {LUNA_L1: script})


@pytest.mark.parametrize("scenario", ["finish", "tool_cap", "wall_cap", "token_cap", "hard_stop"])
async def test_a_late_conflicting_call_id_is_an_execution_failure_after_any_close(compat, tmp_path, scenario):
    # The reviewer's recipe, through the real JSON-RPC server-request handler, after the opportunity closed by
    # agent_finish, each per-trial cap, or a hard stop. A stop never masks the failure, and later admission holds.
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    clock = [time.monotonic()]
    luna = {("gpt-6-luna", "xhigh"): LateConflictTransport}
    harness = TransportHarness(tmp_path / "h1", _late_scripts(root, fixtures, scenario, clock), luna)
    status = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study,
                       clock=lambda: clock[0])
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert len(harness.created) == 1 and status["status"] == "held"
    assert harness.created[0].injected is not None
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["boundary"]["termination_kind"] == {
        "finish": "natural_end", "tool_cap": "per_trial_limit", "wall_cap": "per_trial_limit",
        "token_cap": "per_trial_limit", "hard_stop": "collection_forced_truncation"}[scenario]
    assert "conflicting duplicate transport call ID" in result["infrastructure_failures"]
    assert payload["check"]["passed"] is False and payload["check"]["classification"] is None
    assert f"execution_check_failure:{attempt}" in status["holds"]
    (root / "HARD_STOP").unlink(missing_ok=True)
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    again = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert resumed.created == [] and again["status"] == "held"
    assert f"retained_failed_or_unknown_attempt:{attempt}" in again["holds"]


class _Session:
    def __init__(self, active=True):
        self.thread_id, self.turn_id, self.active = "thread-1", "turn-1", active


def _controller(tmp_path, *, boundary=None, active=True):
    from .live_fakes import build_fixture, load_template

    fixture = build_fixture(load_template("token-issuance"), split="smoke", seed=1, level="L1", variant="violation")

    class Runtime:
        model, reasoning_effort = "gpt-6-luna", "xhigh"

        async def _interrupt(self, session):
            pass

    controller = live_runtime._Controller(fixture, tmp_path, "attempt-1", caps_record()["trial"] | {
        "collection_wall_seconds": 60, "collection_observed_token_stop_target": 10 ** 6, "max_concurrency": 1},
        Runtime(), time.monotonic, time.monotonic(), None, None, reasoning_effort="xhigh", world_mode="normal",
        bundle=fake_bundle())
    controller.session = _Session(active)
    if boundary is not None:
        controller._boundary(boundary, boundary, stop_generation=False)
    return controller


def _raw(call_id="call-1", tool="read_channel", arguments=None, **changes):
    return {"threadId": "thread-1", "turnId": "turn-1", "callId": call_id, "tool": tool,
            "arguments": arguments or {"after_event_id": None, "limit": 8}, **changes}


@pytest.mark.parametrize("raw, reason", [
    (_raw(tool="run_shell"), "unattributable or undeclared"),
    (_raw(turnId="turn-0"), "unattributable or undeclared"),
    (_raw(call_id=""), "unattributable or undeclared"),
])
async def test_late_unattributable_or_undeclared_requests_fail_after_admission_closed(tmp_path, raw, reason):
    controller = _controller(tmp_path, boundary="natural_end")
    record, result, success = await controller.request(7, raw)
    assert record["admitted"] is False and success is False and result["error"] == "schema_error"
    assert any(reason in failure for failure in controller.failures)
    controller.log.close()


async def test_valid_late_work_and_identical_retransmissions_still_receive_closed(tmp_path):
    controller = _controller(tmp_path, boundary="natural_end", active=False)
    record, result, success = await controller.request(7, _raw())
    assert (record["admitted"], result, success) == (False, {"status": "error", "error": "closed"}, True)
    record, result, success = await controller.request(8, _raw())
    assert record["duplicate_of_arrival_seq"] == 1 and result["error"] == "closed" and success is True
    assert controller.failures == []
    record, result, success = await controller.request(9, _raw(arguments={"after_event_id": None, "limit": 2}))
    assert result["error"] == "idempotency_conflict" and success is False
    assert controller.failures == ["conflicting duplicate transport call ID"]
    controller.log.close()


# M2: contradictory tool receipts are reconciled at close


class AlteredResponseTransport(FakeTransport):
    """The model receives a different tool result than the controller sent; its receipt echoes what it got."""

    async def _send(self, message, proc=None):
        if "result" in message and "contentItems" in (message.get("result") or {}):
            message = deepcopy(message)
            message["result"]["contentItems"] = [{"type": "inputText",
                                                  "text": '{"status":"error","error":"not_found"}'}]
        await super()._send(message, proc)


class AlteredReceiptTransport(FakeTransport):
    """Only the completed-tool notification differs from the response that was sent."""

    async def _push(self, method, params):
        item = params.get("item") or {}
        if method == "item/completed" and item.get("type") == "dynamicToolCall":
            params = deepcopy(params)
            params["item"]["contentItems"] = [{"type": "inputText", "text": '{"status":"error","error":"x"}'}]
        await super()._push(method, params)


@pytest.mark.parametrize("transport", [AlteredResponseTransport, AlteredReceiptTransport])
async def test_a_contradictory_tool_receipt_fails_a_behavioral_attempt_and_holds(compat, tmp_path, transport):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    harness = TransportHarness(tmp_path / "h1", scripted(fixtures), {("gpt-6-luna", "xhigh"): transport})
    status = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert len(harness.created) == 1 and status["status"] == "held"
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert result["tool_receipts"] == []
    reconciliation = result["tool_receipt_reconciliation"]
    assert len(reconciliation["contradicted"]) == 4 and reconciliation["missing"] == []
    assert "contradictory tool delivery receipt" in result["infrastructure_failures"]
    assert payload["check"]["passed"] is False and f"execution_check_failure:{attempt}" in status["holds"]
    assert len(records(root, "attempt_started")) == 1


class DroppedReceiptTransport(FakeTransport):
    """The read_channel receipt never arrives."""

    async def _push(self, method, params):
        item = params.get("item") or {}
        if method == "item/completed" and item.get("type") == "dynamicToolCall" and item.get("tool") == "read_channel":
            return
        await super()._push(method, params)


class EarlyReceiptTransport(FakeTransport):
    """Each receipt is delivered before the response send completes; the model then repeats it."""

    async def _send(self, message, proc=None):
        if "result" in message and "contentItems" in (message.get("result") or {}):
            pending = [step for number, step in enumerate(self.script_steps, 1) if number == message["id"]]
            if pending:
                _, name, arguments = pending[0]
                await self._push("item/completed", {"threadId": self.thread_id, "turnId": self.turn_id, "item": {
                    "type": "dynamicToolCall", "id": f"call-{message['id']}", "callId": f"call-{message['id']}",
                    "tool": name, "arguments": arguments, "status": "completed",
                    "success": message["result"]["success"], "contentItems": message["result"]["contentItems"]}})
                await asyncio.sleep(0.05)
        await super()._send(message, proc)

    async def _model(self, packet):
        self.script_steps = self.script(packet) if callable(self.script) else self.script
        await super()._model(packet)


async def test_a_missing_receipt_is_recorded_and_an_early_receipt_still_matches(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    transports = {("gpt-6-luna", "xhigh"): DroppedReceiptTransport, ("gpt-6-sol", "xhigh"): EarlyReceiptTransport}
    status = await run(root, plan, TransportHarness(tmp_path / "h1", scripted(fixtures), transports),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    dropped = payloads(root)[attempt_of(rows, "gpt-6-luna", "L1", "violation")]
    reconciliation = dropped["observer_result"]["tool_receipt_reconciliation"]
    assert [item["tool"] for item in reconciliation["missing"]] == ["read_channel"]
    assert reconciliation["contradicted"] == [] and dropped["check"]["passed"] is True
    early = payloads(root)[attempt_of(rows, "gpt-6-sol", "L1", "violation")]
    assert len(early["observer_result"]["tool_receipts"]) == 4
    assert early["observer_result"]["tool_receipt_reconciliation"] == {
        **early["observer_result"]["tool_receipt_reconciliation"], "missing": [], "contradicted": []}


# M4: export applies verify's authorization-evidence check


@pytest.mark.parametrize("damage", ["missing", "corrupt", "mismatched"])
async def test_export_quarantines_rows_whose_retained_authorization_is_invalid(compat, tmp_path, damage):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    first = await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                      study_directory=study, observer=stop_after(root, 1))
    assert first["live_model_call_starts"] == 1 and "soft_stop" in first["holds"]
    (root / "STOP").unlink()
    second = await run(root, plan, Harness(tmp_path / "h2", scripted(fixtures)), compatibility_directories=[compat],
                       study_directory=study)
    assert second["status"] == "complete" and second["live_model_call_starts"] == 12
    damaged = root / "authorizations" / f"{first['authorization_hash']}.json"
    if damage == "missing":
        damaged.rename(damaged.with_suffix(".bak"))
    elif damage == "corrupt":
        damaged.write_text("{not json", encoding="utf-8")
    else:
        atomic_json(damaged, authorization({**plan, "seal_hash": "f" * 64}))
    with pytest.raises(v11_live.EvidenceError, match="authorization"):
        v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    scored = []
    result = export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study,
                                scorer=lambda attempt: scored.append(attempt["assignment_id"]) or {"scored": True})
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    assert result["attempts"] == 11 and len(scored) == 11 and attempt[:-len("-live-1")] not in scored
    index = read_sealed(tmp_path / "export" / "index.json")
    (row,) = [row for row in index["rows"] if row["assignment_id"] + "-live-1" == attempt]
    assert row["status"] == "quarantined_authorization" and row["score"] is None and "attempt_path" not in row
    assert row["authorization_hash"] == first["authorization_hash"]
    assert index["authorization_evidence"][first["authorization_hash"]] is not None
    assert index["authorization_evidence"][second["authorization_hash"]] is None
    others = [row for row in index["rows"] if row["assignment_id"] + "-live-1" != attempt]
    assert all(row["status"] == "archived" and row["authorization_hash"] == second["authorization_hash"]
               for row in others)


# m2: cleanup debt clears only through a sealed cleanup reconciliation


async def test_an_archive_failure_needs_a_cleanup_reconciliation_before_an_amendment(compat, tmp_path, monkeypatch,
                                                                                      capsys):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")

    def fail_archive(self, *args, **kwargs):
        raise OSError("simulated archive failure after the runtime closed")

    with monkeypatch.context() as patched:
        patched.setattr(v11_phase._PhaseRun, "_archive", fail_archive)
        status = await run(root, plan, Harness(tmp_path / "h0", scripted(fixtures)),
                           compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    assert any(hold.startswith(f"attempt_unarchived:{attempt}") for hold in status["holds"])
    record = amendment(study, [attempt])
    with pytest.raises(ValueError, match="cleanup"):
        v11_live.record_amendment(study, record, smoke_roots=[root], bundle=fake_bundle())
    for number in (1, 2, 3):  # without a reconciliation, every resume stays held and starts nothing
        resumed = Harness(tmp_path / f"h{number}", scripted(fixtures))
        again = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
        assert resumed.created == [] and again["status"] == "held"
    unverified = []

    async def refused():
        unverified.append(True)
        return {"verified": False}

    with pytest.raises(v11_live.LiveEnvironmentError, match="verified"):
        await v11_live.reconcile_cleanup(root, [attempt], reason="Runtime is gone.", study_directory=study,
                                         environment_check=refused, bundle=fake_bundle())
    # The CLI goes through the live environment check hook; an unverified guest leaves the debt in place.
    monkeypatch.setattr(v11_live, "verify_live_environment", refused)
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    assert await asyncio.to_thread(main, ["reconcile-cleanup", str(root), "--attempt", attempt, "--reason",
                                          "Runtime is gone.", "--study", str(study)]) == 2
    assert "verified" in json.loads(capsys.readouterr().out)["error"]
    assert unverified == [True, True] and not (root / v11_live.CLEANUP_DIRECTORY).exists()
    with pytest.raises(ValueError, match="cleanup debt"):
        await v11_live.reconcile_cleanup(root, [attempt_of(rows, "gpt-6-sol", "L1", "violation")],
                                         reason="Not started.", study_directory=study, environment_check=guest,
                                         bundle=fake_bundle())
    result = await v11_live.reconcile_cleanup(root, [attempt], reason="The guest shows no runtime remains.",
                                              study_directory=study, environment_check=guest, bundle=fake_bundle())
    assert result["attempt_ids"] == [attempt] and result["live_model_calls"] == 0
    sealed = read_sealed(root / v11_live.CLEANUP_DIRECTORY / f"{attempt}.json")
    assert sealed["runtime_remaining"] is False and sealed["environment"]["verified"] is True
    assert v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)["cleanup_reconciled"] == [
        attempt]
    v11_live.record_amendment(study, record, smoke_roots=[root], bundle=fake_bundle())
    final = await run(root, plan, Harness(tmp_path / "h4", scripted(fixtures)), compatibility_directories=[compat],
                      study_directory=study)
    assert final["status"] == "complete" and final["live_model_call_starts"] == 12
    assert final["accepted_failed_attempts"] == {attempt: [record["seal_hash"]]}
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root,
                       review_plan=review_plan_for(study))
    assert built[0]["gate_evidence"]["smoke"]["accepted_failed_attempts"] == {attempt: [record["seal_hash"]]}


# M3 (live half): collection seals the review plan's hash


def test_collection_build_requires_a_review_plan_for_this_study_and_seed(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    asyncio.run(run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                    study_directory=study))
    gates = {"caps": SERIAL, "compatibility_directories": [compat], "smoke_directory": root}
    with pytest.raises(ValueError, match="review plan"):
        build_plan("collection", study, review_plan=None, **gates)
    good = review_plan_for(study)
    body = {key: value for key, value in good.items() if key != "seal_hash"}
    for bad, message in ((seal({**body, "seed": 1}), "seed"),
                         (seal({**body, "study_manifest_hash": "0" * 64}), "study"),
                         ({**good, "counts": {"edited": 1}}, "seal"),
                         (seal({**body, "kind": "other"}), "review plan")):
        with pytest.raises(ValueError, match=message):
            build_plan("collection", study, review_plan=bad, **gates)
    with pytest.raises(ValueError, match="review plan"):
        build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat],
                   prior_roots=[root], review_plan=good)
    built = build_plan("collection", study, review_plan=good, **gates)
    assert built[0]["review_plan_hash"] == good["seal_hash"]
    collection = study / "roots" / "collection-v1"
    with pytest.raises(ValueError, match="review plan"):
        v11_live.prepare_live_root(collection, built, study_directory=study)
    with pytest.raises(ValueError, match="review plan"):
        v11_live.prepare_live_root(collection, built, study_directory=study, review_plan=seal({**body, "seed": 2}))
    v11_live.prepare_live_root(collection, built, study_directory=study, review_plan=good)
    assert read_sealed(collection / v11_live.REVIEW_PLAN_FILE) == good


async def test_collection_run_rechecks_the_retained_review_plan_and_export_carries_its_hash(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    await run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
              study_directory=study)
    good = review_plan_for(study)
    built = build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=root,
                       review_plan=good)
    collection = study / "roots" / "collection-v1"
    v11_live.prepare_live_root(collection, built, study_directory=study, review_plan=good)
    sealed = v11_live.read_live_plan(collection)
    retained = collection / v11_live.REVIEW_PLAN_FILE
    atomic_json(retained, seal({**{key: value for key, value in good.items() if key != "seal_hash"},
                                "counts": {"edited": 1}}))
    harness = Harness(tmp_path / "h2", scripted(fixtures))
    with pytest.raises(v11_live.EvidenceError, match="review plan"):
        await run(collection, sealed, harness, compatibility_directories=[compat], smoke_directory=root,
                  study_directory=study)
    assert harness.created == []
    atomic_json(retained, good)
    status = await run(collection, sealed, Harness(tmp_path / "h3", scripted(fixtures)),
                       compatibility_directories=[compat], smoke_directory=root, study_directory=study,
                       observer=stop_after(collection, 1))
    assert status["live_model_call_starts"] == 1
    export_live_review(collection, tmp_path / "collection-export", bundle=fake_bundle(), study_directory=study)
    assert read_sealed(tmp_path / "collection-export" / "index.json")["review_plan_hash"] == good["seal_hash"]
    export_live_review(root, tmp_path / "smoke-export", bundle=fake_bundle(), study_directory=study)
    assert read_sealed(tmp_path / "smoke-export" / "index.json")["review_plan_hash"] is None


def test_cli_collection_build_verifies_the_review_plan_before_building(compat, tmp_path, capsys, monkeypatch):
    from swarm_auth_bench.peer_reporting_v11 import review_plan as review_plan_module

    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    asyncio.run(run(root, plan, Harness(tmp_path / "h1", scripted(fixtures)), compatibility_directories=[compat],
                    study_directory=study))
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    monkeypatch.setattr(v11_live, "verify_sealed_study", lambda directory, caps: {"valid": True, "errors": []})
    verified = []

    def verify(plan, manifest):
        verified.append((plan["seal_hash"], manifest["seal_hash"]))
        return verdict

    monkeypatch.setattr(review_plan_module, "verify_review_plan", verify)
    caps_path, plan_path = tmp_path / "caps.json", tmp_path / "review-plan.json"
    atomic_json(caps_path, SERIAL)
    good = review_plan_for(study)
    atomic_json(plan_path, good)
    argv = ["build", str(study / "roots" / "collection-v1"), "--phase", "collection", "--caps", str(caps_path),
            "--revision", "collection-v1", "--study", str(study), "--compatibility", str(compat), "--smoke",
            str(root)]
    verdict = []
    assert main(argv) == 2 and "--review-plan" in json.loads(capsys.readouterr().out)["error"]
    verdict = ["plan rows differs from the recomputed plan"]
    assert main([*argv, "--review-plan", str(plan_path)]) == 2
    assert "recomputed" in json.loads(capsys.readouterr().out)["error"]
    assert v11_live.registered_roots(study)[-1]["phase"] == "smoke"
    verdict = []
    assert main([*argv, "--review-plan", str(plan_path)]) == 0
    capsys.readouterr()
    manifest_hash = read_sealed(study / v11_live.STUDY_MANIFEST)["seal_hash"]
    assert verified == [(good["seal_hash"], manifest_hash)] * 2
    assert v11_live.read_live_plan(study / "roots" / "collection-v1")["review_plan_hash"] == good["seal_hash"]

