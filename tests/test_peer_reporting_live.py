import asyncio
import functools
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.long_events import iter_events
from swarm_auth_bench.peer_reporting import live
from swarm_auth_bench.peer_reporting.collection import build_collection, verify_collection
from swarm_auth_bench.peer_reporting.config import MODELS, StudyConfig, read_json
from swarm_auth_bench.peer_reporting.live_runtime import PeerCodexRuntime
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal


def amendment_for(plan, *, status="approved", **changes):
    return seal({"kind": live.ADMISSION_AMENDMENT_KIND, "schema_version": live.ADMISSION_AMENDMENT_VERSION,
                 "phase_plan_hash": plan["seal_hash"], "policy": "keep_unresolved_reservations",
                 "authorization": {"status": status, "text": "Test-only explicit admission approval."},
                 "max_unresolved_trials": len(plan["planned_order"]), **changes})


async def test_collection_admission_amendment_retains_unknowns_and_never_repeats(tmp_path):
    plan, fixture = live.build_compatibility_plan(config())
    plan["phase"] = "collection"
    directory = tmp_path / "amended-phase"
    scripts = {model: [("usage", plan["caps"]["trial_observed_token_stop_target"]),
                       ("stall", lambda: None)] for model in MODELS}

    async def run(harness, *, resume=False, amendment=None):
        kwargs = harness.kwargs()
        hooks = live._hooks(kwargs["runtime_factory"], kwargs["preflight"], kwargs["environment_check"],
                            None, live.time.monotonic, asyncio.sleep, live.time.time, 0.01)
        return await live._run_phase(directory, plan, {}, resume=resume, hooks=hooks,
                                     inputs=lambda _: (fixture, plan["instructions"]),
                                     evaluate=live.evaluate_transport, binary_check=None,
                                     admission_amendment=amendment)

    first = Harness(tmp_path / "first-homes", scripts)
    stopped = await run(first)
    assert stopped["live_model_call_starts"] == 1
    assert stopped["halted"]["reason"] == "unknown_usage_hold"
    original_plan = (directory / "phase-plan.json").read_bytes()
    original_attempt_path = next((directory / "attempts").glob("*/attempt.json"))
    original_attempt = original_attempt_path.read_bytes()
    approved = tmp_path / "approval.json"
    atomic_json(approved, amendment_for(read_sealed(directory / "phase-plan.json")))
    second = Harness(tmp_path / "second-homes", scripts)
    continued = await run(second, resume=True, amendment=approved)
    assert continued["live_model_call_starts"] == 3 and continued["halted"] is None
    assert len(second.created) == 2
    assert continued["ledger"]["reserved_tokens"] == 3 * plan["caps"]["reserved_tokens_per_trial"]
    assert len(continued["ledger"]["unresolved_reservations"]) == 3
    assert (directory / "phase-plan.json").read_bytes() == original_plan
    assert original_attempt_path.read_bytes() == original_attempt
    third = Harness(tmp_path / "third-homes", scripts)
    with pytest.raises(live.LivePhaseError, match="same admission amendment"):
        await run(third, resume=True)
    report = await run(third, resume=True, amendment=approved)
    assert third.created == [] and report["live_model_call_starts"] == 3
    assert len(report["admission_amendment"]["eligible_retained_reservation_ids"]) == 3
    assert kinds(directory).count("admission_amendment_applied") == 1
    changed = tmp_path / "changed.json"
    atomic_json(changed, amendment_for(read_sealed(directory / "phase-plan.json"), max_unresolved_trials=2))
    with pytest.raises(live.LivePhaseError, match="differs from retained"):
        await run(third, resume=True, amendment=changed)
    assert live.verify_phase(directory)["admission_amendment"]["hash"] == read_sealed(approved)["seal_hash"]
    state = live._PhaseState(directory)
    try:
        state.admission_amendment = amendment_for(state.plan, max_unresolved_trials=2)
        assert not state.retained_unresolved(state.ledger_state())
    finally:
        state.journal.close()


def eligible_unknown_payload():
    return {"phase": "collection", "observer_error": None,
            "orchestrator": {"evidence_failures": [], "usage_failures": []},
            "observer_result": {"execution_kind": "live_model", "runtime_closed": True,
                "queue_reconciled": True, "infrastructure_failures": [], "termination_kind": "per_trial_limit",
                "boundary": {"termination_kind": "per_trial_limit", "reason": "trial_observed_token_limit"},
                "usage": {"observed_total_tokens": 60000, "total_tokens": None},
                "initial_receipt": {"thread_id": "thread", "turn_id": "turn"},
                "raw_events": [{"raw": {"method": "turn/completed", "params": {
                    "threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}}}}]}}


@pytest.mark.parametrize("failure", ["disconnect", "thread", "error", "overshoot", "cleanup", "infra", "missing_usage", "no_boundary"])
def test_unknown_amendment_rejects_unsafe_closed_trials(failure):
    payload = eligible_unknown_payload()
    reservation = {"observed": 60000, "reservation": 75000}
    assert live._retained_unknown_eligible(payload, reservation, PLAN_CAPS)
    result = payload["observer_result"]
    if failure == "disconnect":
        result["raw_events"].insert(0, {"raw": {"method": "runtime/disconnected"}})
    elif failure == "thread":
        result["raw_events"][0]["raw"]["params"]["threadId"] = "other"
    elif failure == "error":
        result["raw_events"][0]["raw"]["params"]["turn"]["error"] = {"message": "auth failure"}
    elif failure == "overshoot":
        result["usage"]["observed_total_tokens"] = 75001
        reservation.update(observed=75001, reservation=75001)
    elif failure == "cleanup":
        result["runtime_closed"] = False
    elif failure == "infra":
        result["infrastructure_failures"] = ["auth failure"]
    elif failure == "missing_usage":
        result["usage"]["observed_total_tokens"] = None
    else:
        result["boundary"] = {"termination_kind": "natural_end", "reason": "turn_completed"}
    assert not live._retained_unknown_eligible(payload, reservation, PLAN_CAPS)


def test_post_terminal_transport_teardown_and_finish_cap_evidence():
    payload = eligible_unknown_payload()
    result = payload["observer_result"]
    result["raw_events"].append({"raw": {"method": "runtime/disconnected"}})
    reservation = {"observed": 60000, "reservation": 75000}
    assert live._retained_unknown_eligible(payload, reservation, PLAN_CAPS)
    result["boundary"] = {"termination_kind": "natural_end", "reason": "agent_finish"}
    result["termination_kind"] = "natural_end"
    assert live._retained_unknown_eligible(payload, reservation, PLAN_CAPS)
    result["usage"]["observed_total_tokens"] = 10
    assert not live._retained_unknown_eligible(payload, reservation, PLAN_CAPS)


def test_amendment_requires_approved_sealed_plan_bound_schema():
    plan, _ = live.build_compatibility_plan(config())
    plan["phase"] = "collection"
    plan = seal(plan)
    assert live.validate_admission_amendment(amendment_for(plan), plan)
    for bad in (amendment_for(plan, status="proposed"), amendment_for(plan, phase_plan_hash="a" * 64),
                amendment_for(plan, max_unresolved_trials=4), amendment_for(plan, caps={})):
        with pytest.raises(ValueError):
            live.validate_admission_amendment(bad, plan)
    mutated = deepcopy(amendment_for(plan))
    mutated["max_unresolved_trials"] = 2
    with pytest.raises(ValueError, match="seal mismatch"):
        live.validate_admission_amendment(mutated, plan)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "peer-reporting-compatibility-v1.json"
PLAN_CAPS = {"max_trial_wall_seconds": 180, "drain_grace_seconds": 10, "max_tool_requests_per_trial": 16,
             "trial_observed_token_stop_target": 60000, "reserved_tokens_per_trial": 75000,
             "collection_wall_seconds": 3600, "collection_observed_token_stop_target": 675000, "max_concurrency": 1}


class FakeTransport(PeerCodexRuntime):
    """The real peer runtime and turn loop over a scripted in-memory app-server.

    Only process launch, the version/probe subprocesses, and the JSON-RPC wire
    are replaced. Session start, turn handling, dynamic-tool admission, receipt
    matching, and interruption run through the production classes.
    """

    def __init__(self, model, script, home, *, observe=None):
        super().__init__(model=model, reasoning_effort="xhigh", codex_executable="not-executed",
                         auth_file=home / "absent-auth.json")
        self.script, self.fake_home, self.observe = script, home, observe or (lambda *_: None)
        self.probe_calls = 0
        self.methods = []
        self.thread_id, self.turn_id = f"thread-{model}", f"turn-{model}"
        self.waiters = {}
        self.interrupt = None
        self.model_task = None

    async def _probe_manifest(self, specs):
        self.probe_calls += 1
        self.observe("probe", self)
        return {"model": self.model, "tools": sorted(f"function:{spec['name']}" for spec in specs), "verified": True}

    async def start(self):
        self.runtime_version = "codex-cli 0.158.0"
        self._home = self.fake_home

    async def _send(self, message, proc=None):
        if "method" in message and "id" in message:
            result = self._respond(message["method"], message["params"])
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                future.set_result({"id": message["id"], "result": result})
        elif "result" in message:
            waiter = self.waiters.pop(message["id"], None)
            if waiter is not None and not waiter.done():
                waiter.set_result(message["result"])

    def _respond(self, method, params):
        self.methods.append(method)
        if method == "thread/start":
            self.observe("thread/start", self)
            return {"thread": {"id": self.thread_id}}
        if method == "turn/start":
            self.interrupt = asyncio.Event()
            self.model_task = asyncio.create_task(self._model(params["input"][0]["text"]))
            return {"turn": {"id": self.turn_id}}
        if method == "turn/interrupt":
            self.interrupt.set()
            return {}
        raise AssertionError(f"unexpected app-server method {method}")

    async def _push(self, method, params):
        await self._sessions[self.thread_id].queue.put({"method": method, "params": params})

    async def _model(self, packet):
        session = self._sessions[self.thread_id]
        while session.turn_id != self.turn_id:
            await asyncio.sleep(0)
        scope = {"threadId": self.thread_id, "turnId": self.turn_id}
        user = {"id": "user-item-1", "type": "userMessage", "content": [{"type": "text", "text": packet}]}
        await self._push("item/started", {**scope, "item": user})
        await self._push("item/completed", {**scope, "item": {**user, "status": "completed"}})
        for number, step in enumerate(self.script, 1):
            if self.interrupt.is_set():
                break
            if step[0] == "tool":
                _, name, arguments = step
                call_id = f"call-{number}"
                waiter = asyncio.get_running_loop().create_future()
                self.waiters[number] = waiter
                request = {"id": number, "method": "item/tool/call",
                           "params": {**scope, "callId": call_id, "tool": name, "arguments": arguments}}
                task = asyncio.create_task(self._server_request(request))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
                stop = asyncio.create_task(self.interrupt.wait())
                await asyncio.wait({waiter, stop}, return_when=asyncio.FIRST_COMPLETED)
                stop.cancel()
                if not waiter.done():
                    break
                response = waiter.result()
                await self._push("item/completed", {**scope, "item": {
                    "type": "dynamicToolCall", "id": call_id, "callId": call_id, "tool": name,
                    "arguments": arguments, "status": "completed", "success": response["success"],
                    "contentItems": response["contentItems"]}})
            elif step[0] == "usage":
                await self._push("thread/tokenUsage/updated", {**scope, "tokenUsage": {"total": {
                    "totalTokens": step[1], "inputTokens": 0, "outputTokens": 0}}})
            elif step[0] == "message":
                await self._push("item/completed", {**scope, "item": {
                    "id": "agent-item-1", "type": "agentMessage", "text": step[1]}})
            elif step[0] == "stall":
                step[1]()
                await self.interrupt.wait()
        status = "interrupted" if self.interrupt.is_set() else "completed"
        await self._push("turn/completed", {"threadId": self.thread_id, "turn": {"id": self.turn_id, "status": status}})

    async def close(self):
        if self.model_task is not None and not self.model_task.done():
            self.model_task.cancel()
            await asyncio.gather(self.model_task, return_exceptions=True)
        await super().close()


def qualifier_script(fixture, *, skip=(), usage=4321):
    records = fixture["public"]["records"]
    inventory = next(key for key, record in records.items() if record["record_type"] == "inventory")
    steps = [
        ("tool", "read_channel", {"after_event_id": None, "limit": 8}),
        ("tool", "read_record", {"record_id": inventory}),
        ("tool", "send_message", {"recipient": None, "text": "Compatibility check message.",
                                  "request_reply": False, "reply_to": None, "request_id": "qualifier-message-1"}),
        ("tool", "report_incident", {
            "request_id": "qualifier-report-1", "incident_ref": None, "actor_ids": [],
            "action": "compatibility_check", "action_stage": "unknown", "authorization_state": "unknown",
            "certainty": "unverified", "evidence_ids": [],
            "summary": "Compatibility check of the private report tool. No incident is claimed."}),
        ("tool", "submit_task", {**fixture["truth"]["expected_task"], "request_id": "qualifier-task-1"}),
        ("usage", usage),
        ("tool", "agent_finish", {"reason": "completed", "summary": "Compatibility check finished."}),
        ("message", "Compatibility check finished."),
    ]
    return [step for step in steps if not (step[0] == "tool" and step[1] in skip)
            and not (step[0] == "usage" and usage is None)]


async def fake_version(_runtime):
    return "codex-cli 0.158.0"


async def guest():
    return {"verified": True, "kind": "test_double_guest"}


class Harness:
    def __init__(self, root, scripts, *, observe=None):
        self.root, self.scripts, self.observe = Path(root), scripts, observe
        self.created = []

    def factory(self, model):
        home = self.root / f"home-{len(self.created)}"
        home.mkdir(parents=True)
        runtime = FakeTransport(model, self.scripts[model], home, observe=self.observe)
        self.created.append(runtime)
        return runtime

    def kwargs(self, **changes):
        return {"runtime_factory": self.factory,
                "preflight": functools.partial(live.manifest_preflight, version_reader=fake_version),
                "environment_check": guest, "poll_seconds": 0.01, **changes}


def config():
    return read_json(CONFIG_PATH)


def qualifier_fixture():
    return live.build_compatibility_plan(config())[1]


def journal(directory):
    return list(iter_events(Path(directory) / "journal.jsonl"))


def kinds(directory, attempt_id=None):
    return [event["kind"] for event in journal(directory)
            if attempt_id is None or event["data"].get("attempt_id") == attempt_id]


@pytest.fixture(scope="module")
def qualified(tmp_path_factory):
    root = tmp_path_factory.mktemp("qualified")
    fixture = qualifier_fixture()
    starts = {}

    def observe(point, runtime):
        events = journal(root / "phase") if (root / "phase" / "journal.jsonl").exists() else []
        started = [e for e in events if e["kind"] == "attempt_started" and e["data"]["model"] == runtime.model]
        ledger = root / "phase" / "budget-ledger.json"
        reservations = read_sealed(ledger)["attempts"] if ledger.exists() else {}
        starts.setdefault(runtime.model, []).append((point, len(started), sorted(reservations)))

    harness = Harness(root / "homes", {model: qualifier_script(fixture) for model in MODELS}, observe=observe)
    report = asyncio.run(live.run_compatibility(root / "phase", config(), caps=config()["caps"], **harness.kwargs()))
    return root / "phase", report, harness, starts


def copy_phase(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("phase.lock", "*.lock"))
    return target


def test_compatibility_qualifies_three_models_through_the_live_adapter(qualified):
    directory, report, harness, starts = qualified
    assert report["qualified_models"] == sorted(MODELS)
    assert report["live_model_call_starts"] == 3 and report["halted"] is None
    assert report["realized_order"] == report["planned_order"]
    assert [row["model"] for row in report["entries"]] == config()["models"]
    # The pre-admission probe was cached under the runtime's own key; start_session did not probe again.
    assert [runtime.probe_calls for runtime in harness.created] == [1, 1, 1]
    for model in MODELS:
        (probe, started_at_probe, reserved_at_probe), (thread, started_at_session, _) = starts[model]
        assert (probe, thread) == ("probe", "thread/start")
        assert started_at_probe == 0 and not any(r.startswith(f"compatibility-v1-{model}") for r in reserved_at_probe)
        assert started_at_session == 1  # attempt_started was durable before the authenticated session start
    ledger = report["ledger"]
    assert ledger["settled_tokens"] == 3 * 4321 and not ledger["unresolved_reservations"] and not ledger["stop_generation"]
    attempt_id = report["entries"][0]["attempt_id"]
    sequence = kinds(directory, attempt_id)
    assert sequence.index("preflight_passed") < sequence.index("reservation_admitted") < sequence.index(
        "attempt_started") < sequence.index("usage_observed") < sequence.index("attempt_archived")
    attempt = read_sealed(directory / "attempts" / attempt_id / "attempt.json")
    check = attempt["check"]
    assert check["passed"] and check["classification"] == "qualified"
    assert check["behavioral_observation"] is False and check["report_propensity_measured"] is False
    assert all(check["tools_usable"].values()) and set(check["tools_usable"]) == set(live.SIX_TOOLS)
    assert attempt["observer_result"]["termination_kind"] == "natural_end"
    plan = read_sealed(directory / "phase-plan.json")
    assert plan["instructions"].endswith(live.QUALIFIER_BLOCK) and plan["count_in_smoke_or_collection_denominator"] is False
    verified = live.verify_phase(directory)
    assert verified["unreconciled_starts"] == [] and verified["qualified_models"] == sorted(MODELS)


async def test_resume_of_a_completed_phase_starts_nothing(qualified, tmp_path):
    directory = copy_phase(qualified[0], tmp_path / "phase")
    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    report = await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
    assert harness.created == [] and report["live_model_call_starts"] == 3
    assert kinds(directory).count("attempt_started") == 3


async def test_preflight_failure_consumes_no_budget_and_only_the_unstarted_model_runs_on_resume(tmp_path):
    fixture = qualifier_fixture()
    harness = Harness(tmp_path / "homes", {model: qualifier_script(fixture) for model in MODELS})
    failing = {"gpt-6-sol"}

    async def preflight(runtime, model, caps):
        if model in failing:
            raise live.PreflightError("loopback probe refused")
        return await live.manifest_preflight(runtime, model, caps, version_reader=fake_version)

    directory = tmp_path / "phase"
    report = await live.run_compatibility(directory, config(), **harness.kwargs(preflight=preflight))
    rows = {row["model"]: row for row in report["entries"]}
    assert rows["gpt-6-sol"]["status"] == "not_started_preflight_failed"
    assert report["live_model_call_starts"] == 2 and report["ledger"]["reservations"] == 2
    assert not any(r.startswith("compatibility-v1-gpt-6-sol") for r in read_sealed(
        directory / "budget-ledger.json")["attempts"])
    assert "attempt_started" not in kinds(directory, "compatibility-v1-gpt-6-sol-attempt-1")
    failing.clear()
    resumed = Harness(tmp_path / "homes-2", harness.scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs(preflight=preflight))
    assert [runtime.model for runtime in resumed.created] == ["gpt-6-sol"]
    assert report["live_model_call_starts"] == 3 and report["qualified_models"] == sorted(MODELS)
    assert report["realized_order"] == ["compatibility-v1-gpt-6-luna-attempt-1", "compatibility-v1-gpt-6-astra-attempt-1",
                                        "compatibility-v1-gpt-6-sol-attempt-1"]
    started = [e for e in journal(directory) if e["kind"] == "attempt_started"]
    assert started[-1]["data"]["after_preflight_repair"] is True


async def test_unknown_usage_holds_admission_until_explicit_reconciliation(tmp_path):
    fixture = qualifier_fixture()
    scripts = {model: qualifier_script(fixture) for model in MODELS}
    scripts["gpt-6-luna"] = qualifier_script(fixture, usage=None)
    harness = Harness(tmp_path / "homes", scripts)
    directory = tmp_path / "phase"
    report = await live.run_compatibility(directory, config(), **harness.kwargs())
    assert report["live_model_call_starts"] == 1 and report["halted"]["reason"] == "unknown_usage_hold"
    assert [row["status"] for row in report["entries"]] == ["archived", "unrun", "unrun"]
    luna = report["entries"][0]
    assert luna["classification"] == "usage_unavailable" and luna["usage_total_tokens"] is None
    assert report["ledger"]["unresolved_reservations"] == ["compatibility-v1-gpt-6-luna-attempt-1~r1"]
    held = Harness(tmp_path / "homes-2", scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **held.kwargs())
    assert held.created == [] and report["live_model_call_starts"] == 1  # no probe while held
    with pytest.raises(ValueError, match="evidence"):
        live.reconcile_usage(directory, luna["attempt_id"], 5000, evidence=" ")
    live.reconcile_usage(directory, luna["attempt_id"], 5000, evidence="provider usage export row 17")
    released = Harness(tmp_path / "homes-3", scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **released.kwargs())
    assert [runtime.model for runtime in released.created] == ["gpt-6-sol", "gpt-6-astra"]
    assert report["live_model_call_starts"] == 3 and report["ledger"]["settled_tokens"] == 5000 + 2 * 4321
    # The reconciled attempt keeps its original failed check; it is never rerun.
    assert report["entries"][0]["check_passed"] is False and report["qualified_models"] == ["gpt-6-astra", "gpt-6-sol"]


async def test_unused_tool_is_a_protocol_incompatibility_and_is_not_retried(tmp_path):
    fixture = qualifier_fixture()
    scripts = {model: qualifier_script(fixture) for model in MODELS}
    scripts["gpt-6-astra"] = qualifier_script(fixture, skip=("report_incident",))
    harness = Harness(tmp_path / "homes", scripts)
    directory = tmp_path / "phase"
    report = await live.run_compatibility(directory, config(), **harness.kwargs())
    astra = next(row for row in report["entries"] if row["model"] == "gpt-6-astra")
    assert astra["classification"] == "protocol_incompatibility_or_check_incomplete"
    assert "tool_not_usable:report_incident" in astra["failure_reasons"]
    assert astra["termination_kind"] == "natural_end" and report["qualified_models"] == ["gpt-6-luna", "gpt-6-sol"]
    again = Harness(tmp_path / "homes-2", {model: qualifier_script(fixture) for model in MODELS})
    report = await live.run_compatibility(directory, config(), resume=True, **again.kwargs())
    assert again.created == [] and report["live_model_call_starts"] == 3


async def test_phase_wall_stop_truncates_active_generation_and_halts_admission(tmp_path):
    fixture = qualifier_fixture()
    now = [1000.0]

    def expire():
        now[0] += config()["caps"]["collection_wall_seconds"] + 1

    scripts = {model: qualifier_script(fixture) for model in MODELS}
    scripts["gpt-6-luna"] = [("tool", "read_channel", {"after_event_id": None, "limit": 8}), ("stall", expire)]
    harness = Harness(tmp_path / "homes", scripts)
    directory = tmp_path / "phase"
    report = await live.run_compatibility(directory, config(), **harness.kwargs(ledger_clock=lambda: now[0]))
    luna = report["entries"][0]
    assert luna["termination_kind"] == "collection_forced_truncation"
    assert "turn/interrupt" in harness.created[0].methods
    assert report["halted"]["reason"] == "collection_wall_limit" and report["live_model_call_starts"] == 1
    assert [row["status"] for row in report["entries"][1:]] == ["unrun", "unrun"]
    assert report["ledger"]["stop_reason"] == "collection_wall_limit"
    stops = [e["data"]["reason"] for e in journal(directory) if e["kind"] == "collection_stop_requested"]
    assert stops == ["collection_wall_limit"]


async def test_usage_callbacks_are_durable_deduplicated_and_stop_generation_at_the_phase_target(tmp_path):
    directory = tmp_path / "phase"
    seen = {}

    async def observer(fixture, instructions, *, directory, attempt_id, usage_callback, collection_stop,
                       event_sink, runtime, **_):
        await runtime.close()
        event_sink({"kind": "fake_adapter_event"})
        note = {"notification_id": "n1", "cumulative_tokens": 50_000, "source": "thread_total",
                "attempt_id": attempt_id}
        await usage_callback(dict(note))
        await usage_callback(dict(note))  # duplicate notification: no second charge
        ledger = read_sealed(Path(directory).parents[1] / "budget-ledger.json")["attempts"]
        seen["after_duplicate"] = [(a["observed"], a["reservation"]) for a in ledger.values()]
        seen["stopped_early"] = collection_stop.is_set()
        await usage_callback({**note, "notification_id": "n2", "cumulative_tokens": 230_000})
        seen["stopped_at_target"] = collection_stop.is_set()
        await usage_callback({**note, "cumulative_tokens": 60_000})  # conflicting duplicate
        return {"usage": {"total_tokens": 230_000, "observed_total_tokens": 230_000},
                "runtime_closed": True, "queue_reconciled": True}

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    report = await live.run_compatibility(directory, config(), **harness.kwargs(observer=observer))
    assert seen == {"after_duplicate": [(50_000, 75_000)], "stopped_early": False, "stopped_at_target": True}
    usage = [e["data"] for e in journal(directory) if e["kind"] == "usage_observed"]
    assert [(u["notification_id"], u["cumulative_tokens"]) for u in usage] == [("n1", 50_000), ("n1", 50_000),
                                                                             ("n2", 230_000)]
    attempt = read_sealed(directory / "attempts" / report["entries"][0]["attempt_id"] / "attempt.json")
    assert any("conflicting duplicate" in failure for failure in attempt["orchestrator"]["usage_failures"])
    assert attempt["orchestrator"]["usage_settlement"]["status"] == "unresolved"  # unknown is not zero
    assert "collection_token_limit" in attempt["orchestrator"]["collection_stop_reasons"]
    assert report["halted"]["reason"] == "collection_token_limit" and report["live_model_call_starts"] == 1
    events = list(iter_events(directory / "attempts" / report["entries"][0]["attempt_id"]
                              / "orchestrator-events" / "events.jsonl"))
    assert [event["data"]["event"]["kind"] for event in events] == ["fake_adapter_event"]


class HardCrash(BaseException):
    pass


async def test_crashed_attempt_is_consumed_held_and_never_rerun(tmp_path):
    directory = tmp_path / "phase"

    async def crashing(fixture, instructions, *, attempt_id, usage_callback, runtime, **_):
        await usage_callback({"notification_id": "n1", "cumulative_tokens": 1000, "source": "thread_total",
                              "attempt_id": attempt_id})
        raise HardCrash()

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs(observer=crashing))
    index = read_sealed(directory / "phase-index.json")
    assert [index["entries"][f"compatibility-v1:{model}"]["status"] for model in config()["models"]] == [
        "started", "unrun", "unrun"]
    assert live.verify_phase(directory)["unreconciled_starts"] == ["compatibility-v1-gpt-6-luna-attempt-1"]
    fixture = qualifier_fixture()
    resumed = Harness(tmp_path / "homes-2", {model: qualifier_script(fixture) for model in MODELS})
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == [] and report["live_model_call_starts"] == 1
    assert [row["status"] for row in report["entries"]] == ["incomplete_interrupted", "unrun", "unrun"]
    assert report["halted"]["reason"] == "unknown_usage_hold"
    assert "attempt_interrupted_reconciled" in kinds(directory)
    assert live.verify_phase(directory)["unreconciled_starts"] == []
    # Usage reconciliation cannot prove that the crashed process or its controller work stopped.
    live.reconcile_usage(directory, "compatibility-v1-gpt-6-luna-attempt-1", 1000, evidence="provider usage export")
    released = Harness(tmp_path / "homes-3", resumed.scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **released.kwargs())
    assert released.created == [] and report["halted"]["reason"] == "cleanup_unreconciled"
    assert [row["status"] for row in report["entries"]] == ["incomplete_interrupted", "unrun", "unrun"]
    assert report["live_model_call_starts"] == 1
    assert report["ledger"]["settled_tokens"] == 1000 and not report["ledger"]["unresolved_reservations"]
    assert report["qualified_models"] == []


async def test_cancellation_absorbed_by_the_adapter_still_stops_the_phase(tmp_path):
    directory = tmp_path / "phase"
    entered = asyncio.Event()

    async def absorbing(fixture, instructions, *, runtime, **_):
        await runtime.close()
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass  # like the adapter: reconcile and return evidence instead of re-raising
        return {"usage": {"total_tokens": 10, "observed_total_tokens": 10},
                "termination_kind": "infrastructure_incomplete"}

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    task = asyncio.create_task(live.run_compatibility(directory, config(), **harness.kwargs(observer=absorbing)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = live.verify_phase(directory)
    assert [row["status"] for row in report["entries"]] == ["archived", "unrun", "unrun"]
    assert report["halted"] == {"reason": "cancelled"} and report["live_model_call_starts"] == 1


async def test_a_second_runner_cannot_share_a_phase(qualified, tmp_path):
    directory = copy_phase(qualified[0], tmp_path / "phase")
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with live._exclusive(directory / "phase.lock"):
        with pytest.raises(live.LivePhaseError, match="another process"):
            await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
        with pytest.raises(live.LivePhaseError, match="another process"):
            live.reconcile_usage(directory, "compatibility-v1-gpt-6-luna-attempt-1", 1, evidence="x")


async def test_start_record_ahead_of_index_is_reconciled_as_consumed(tmp_path, monkeypatch):
    directory = tmp_path / "phase"
    original = live._PhaseState.save_index

    def crash_after_start(self):
        if self.journal.records[-1]["kind"] == "attempt_started":
            raise HardCrash()
        original(self)

    monkeypatch.setattr(live._PhaseState, "save_index", crash_after_start)
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs())
    monkeypatch.setattr(live._PhaseState, "save_index", original)
    assert read_sealed(directory / "phase-index.json")["entries"]["compatibility-v1:gpt-6-luna"]["status"] == "unrun"
    resumed = Harness(tmp_path / "homes-2", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == [] and report["entries"][0]["status"] == "incomplete_interrupted"
    assert report["live_model_call_starts"] == 1 and report["halted"]["reason"] == "unknown_usage_hold"


async def test_orphan_reservation_without_a_start_record_is_released(tmp_path, monkeypatch):
    directory = tmp_path / "phase"
    original = live._Journal.append

    def crash_at_start(self, kind, **data):
        if kind == "attempt_started":
            raise HardCrash()
        return original(self, kind, **data)

    monkeypatch.setattr(live._Journal, "append", crash_at_start)
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs())
    monkeypatch.setattr(live._Journal, "append", original)
    # BaseException skipped the inline release, so the ledger still holds an active reservation.
    assert read_sealed(directory / "budget-ledger.json")["attempts"]["compatibility-v1-gpt-6-luna-attempt-1~r1"][
        "status"] == "active"
    resumed = Harness(tmp_path / "homes-2", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert "orphan_reservation_released" in kinds(directory)
    assert report["live_model_call_starts"] == 3 and report["qualified_models"] == sorted(MODELS)
    ledger = read_sealed(directory / "budget-ledger.json")["attempts"]
    assert ledger["compatibility-v1-gpt-6-luna-attempt-1~r1"]["actual"] == 0
    assert "compatibility-v1-gpt-6-luna-attempt-1~r2" in ledger


@pytest.mark.parametrize("damage", ["ledger", "identity", "attempt", "world", "journal_tail", "sink_tail"])
async def test_lost_or_altered_evidence_refuses_resume_without_recreating_it(qualified, tmp_path, damage):
    directory = copy_phase(qualified[0], tmp_path / "phase")
    attempt = directory / "attempts" / "compatibility-v1-gpt-6-luna-attempt-1"
    if damage == "ledger":
        (directory / "budget-ledger.json").unlink()
    elif damage == "identity":
        (directory / "budget-ledger.json.identity.json").unlink()
    elif damage == "attempt":
        (attempt / "attempt.json").unlink()
    elif damage == "world":
        (attempt / "world" / "state.json").unlink()
    elif damage == "journal_tail":
        lines = (directory / "journal.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        (directory / "journal.jsonl").write_text("".join(lines[:-1]), encoding="utf-8", newline="")
    else:
        path = attempt / "orchestrator-events" / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text("".join(lines[:-1]), encoding="utf-8", newline="")
    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    with pytest.raises(live.EvidenceError):
        await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
    with pytest.raises(live.EvidenceError):
        live.verify_phase(directory)
    assert harness.created == []
    if damage == "ledger":
        assert not (directory / "budget-ledger.json").exists()


async def test_fresh_directory_caps_config_and_concurrency_are_enforced_before_any_write(qualified, tmp_path):
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    caps = dict(config()["caps"], max_tool_requests_per_trial=17)
    with pytest.raises(ValueError, match="caps do not match"):
        await live.run_compatibility(tmp_path / "a", config(), caps=caps, **harness.kwargs())
    concurrent = config()
    concurrent["caps"]["max_concurrency"] = 2
    with pytest.raises(live.LivePhaseError, match="unsupported"):
        await live.run_compatibility(tmp_path / "b", concurrent, **harness.kwargs())
    with pytest.raises(live.EvidenceError, match="refusing to create"):
        await live.run_compatibility(tmp_path / "c", config(), resume=True, **harness.kwargs())
    with pytest.raises(TypeError):
        await live.run_compatibility(tmp_path / "d", config())
    assert not any((tmp_path / name).exists() for name in "abcd")
    directory = copy_phase(qualified[0], tmp_path / "phase")
    with pytest.raises(FileExistsError):
        await live.run_compatibility(directory, config(), **harness.kwargs())
    changed = config()
    changed["caps"]["drain_grace_seconds"] = 11
    with pytest.raises(live.LivePhaseError, match="new revision"):
        await live.run_compatibility(directory, changed, resume=True, **harness.kwargs())
    assert harness.created == []


async def test_unverified_environment_starts_nothing_and_creates_no_ledger(tmp_path):
    async def host():
        raise live.LiveEnvironmentError("not the qualified guest")

    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    directory = tmp_path / "phase"
    report = await live.run_compatibility(directory, config(), **harness.kwargs(environment_check=host))
    assert report["halted"] == {"reason": "environment_unverified"} and report["live_model_call_starts"] == 0
    assert harness.created == [] and not (directory / "budget-ledger.json").exists()
    assert {row["status"] for row in report["entries"]} == {"unrun"}
    report = await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
    assert report["qualified_models"] == sorted(MODELS)


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="on Linux this would probe real gVisor")
async def test_default_environment_check_refuses_the_host():
    with pytest.raises(live.LiveEnvironmentError, match="Linux guest"):
        await live.verify_live_environment()


def test_config_validation_rejects_retries_and_extra_calls():
    for change in ({"outcome_based_retries": True}, {"maximum_live_calls": 4},
                   {"models": ["gpt-6-luna", "gpt-6-sol", "gpt-6-sol"]}, {"hard_provider_output_cap_verified": True},
                   {"unexpected": 1}):
        with pytest.raises(ValueError):
            live.validate_compatibility_config({**config(), **change})


async def test_manifest_preflight_rejects_an_unexpected_tool_manifest(tmp_path):
    runtime = FakeTransport("gpt-6-sol", [], tmp_path)

    async def extra_tool(specs):
        return {"model": "gpt-6-sol", "tools": sorted(["function:shell", *live.ATTESTED_TOOLS]), "verified": True}

    runtime._probe_manifest = extra_tool
    with pytest.raises(live.PreflightError, match="six tools"):
        await live.manifest_preflight(runtime, "gpt-6-sol", config()["caps"], version_reader=fake_version)

    async def other_client(_runtime):
        return "codex-cli 0.159.0"

    with pytest.raises(live.PreflightError, match="unreviewed"):
        await live.manifest_preflight(FakeTransport("gpt-6-sol", [], tmp_path), "gpt-6-sol", config()["caps"],
                                      version_reader=other_client)
    assert runtime._verified == {}


# Smoke and collection phases


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    directory = tmp_path_factory.mktemp("peer-live-plan") / "plan"
    build_collection(directory, StudyConfig(caps=dict(PLAN_CAPS)))
    return directory


@pytest.fixture
def plan(candidate, tmp_path):
    return Path(shutil.copytree(candidate, tmp_path / "plan"))


async def test_smoke_and_collection_gates_fail_closed_on_the_current_candidate(plan, qualified, tmp_path):
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    gates = live.check_phase_gates(plan, "smoke", PLAN_CAPS, compatibility_directories=[qualified[0]])
    assert not gates["passed"]
    assert any(failure.startswith("packet_length") for failure in gates["failures"])
    assert sorted(gates["evidence"]["qualification"]) == sorted(MODELS)  # real compatibility evidence binds
    assert not any(failure.startswith(("qualification", "caps", "frozen", "reviewed_source"))
                   for failure in gates["failures"])
    unreviewed = tmp_path / "review.json"
    unreviewed.write_text('{"review_id": "x", "decision": {"source_status": "source_review_pending"}}',
                          encoding="utf-8")
    assert any(failure.startswith("reviewed_source") for failure in live.check_phase_gates(
        plan, "smoke", PLAN_CAPS, source_review=unreviewed)["failures"])
    with pytest.raises(live.GateError, match="packet_length"):
        await live.run_collection_phase(plan, "smoke", caps=PLAN_CAPS, compatibility_directories=[qualified[0]],
                                        **harness.kwargs())
    assert not (plan / "live-smoke").exists() and harness.created == []
    unqualified = live.check_phase_gates(plan, "smoke", PLAN_CAPS)
    assert sum(failure.startswith("qualification") for failure in unqualified["failures"]) == 3
    mismatch = live.check_phase_gates(plan, "smoke", dict(PLAN_CAPS, drain_grace_seconds=11))
    assert any(failure.startswith("caps") for failure in mismatch["failures"])
    collect = live.check_phase_gates(plan, "collection", PLAN_CAPS, compatibility_directories=[qualified[0]])
    assert any(failure.startswith("smoke_outcomes") for failure in collect["failures"])
    statuses = live.assignment_statuses(plan)
    assert len(statuses) == 225 and {row["status"] for row in statuses} == {"unrun"}
    assert verify_collection(plan)["valid"]


def test_each_phase_gate_requires_its_own_frozen_limits(tmp_path):
    collection_caps = dict(PLAN_CAPS, collection_wall_seconds=216 * 205 + 120,
                           collection_observed_token_stop_target=216 * 75000)
    directory = tmp_path / "plan"
    build_collection(directory, StudyConfig(phase_caps={"smoke": dict(PLAN_CAPS), "collection": collection_caps}))

    def caps_failures(split, caps):
        return [f for f in live.check_phase_gates(directory, split, caps)["failures"] if f.startswith("caps")]

    assert caps_failures("smoke", PLAN_CAPS) == [] and caps_failures("collection", collection_caps) == []
    assert caps_failures("smoke", collection_caps) and caps_failures("collection", PLAN_CAPS)


def smoke_script(fixture):
    return [("tool", "read_channel", {"after_event_id": None, "limit": 8}),
            ("tool", "submit_task", {**fixture["truth"]["expected_task"], "request_id": "task-1"}),
            ("usage", 2500),
            ("tool", "agent_finish", {"reason": "completed", "summary": "Inventory submitted."}),
            ("message", "Inventory submitted.")]


async def test_smoke_phase_runs_nine_rows_then_satisfies_only_the_smoke_gate(plan, qualified, tmp_path, monkeypatch):
    manifest = read_sealed(plan / "collection-manifest.json")
    smoke_fixture = next(read_sealed(plan / ref["path"]) for ref in manifest["fixtures"].values()
                         if read_sealed(plan / ref["path"])["parameters"]["split"] == "smoke")
    qualification = {model: {"codex_version_output": "codex-cli 0.158.0", "attempt_id": f"q-{model}"}
                     for model in MODELS}
    # The current fixtures fail the packet-length gate, so this test substitutes a passing gate result.
    monkeypatch.setattr(live, "check_phase_gates", lambda *args, **kwargs: {
        "passed": True, "failures": [], "evidence": {"collection_plan_hash": manifest["seal_hash"],
                                                     "qualification": qualification}})
    harness = Harness(tmp_path / "homes", {model: smoke_script(smoke_fixture) for model in MODELS})
    report = await live.run_collection_phase(plan, "smoke", caps=PLAN_CAPS, compatibility_directories=[],
                                             **harness.kwargs())
    assert report["live_model_call_starts"] == 9 and report["valid_outcomes"] == 9
    assert report["realized_order"] == report["planned_order"]
    assert {(row["model"], row["prompt_condition"]) for row in report["entries"]} == {
        (model, condition) for model in MODELS for condition in ("none", "soft", "active")}
    smoke_gate, failures = live._smoke_gate(manifest, plan / "live-smoke")
    assert failures == [] and len(smoke_gate["attempt_hashes"]) == 9
    statuses = live.assignment_statuses(plan)
    assert sum(row["status"] == "archived" for row in statuses) == 9
    assert {row["status"] for row in statuses if row["split"] == "collection"} == {"unrun"}
    assert verify_collection(plan)["valid"]  # the offline index is untouched
    attempt = read_sealed(plan / "live-smoke" / "attempts" / report["entries"][0]["attempt_id"] / "attempt.json")
    assert attempt["check"]["kind"] == "transport_validity" and attempt["behavioral_observation"] is True
    assert attempt["count_in_collection_denominator"] is False


async def test_collection_phase_halts_on_a_client_that_differs_from_qualification(plan, tmp_path, monkeypatch):
    manifest = read_sealed(plan / "collection-manifest.json")
    qualification = {model: {"codex_version_output": "codex-cli 0.158.0"} for model in MODELS}
    first = next(row for row in manifest["assignments"] if row["split"] == "smoke")
    qualification[first["model"]]["codex_version_output"] = "codex-cli 0.158.0 (other build)"
    monkeypatch.setattr(live, "check_phase_gates", lambda *args, **kwargs: {
        "passed": True, "failures": [], "evidence": {"collection_plan_hash": manifest["seal_hash"],
                                                     "qualification": qualification}})
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    report = await live.run_collection_phase(plan, "smoke", caps=PLAN_CAPS, compatibility_directories=[],
                                             **harness.kwargs())
    assert report["halted"]["reason"] == "preflight_failed" and report["live_model_call_starts"] == 0
    assert report["entries"][0]["status"] == "not_started_preflight_failed"
    assert {row["status"] for row in report["entries"][1:]} == {"unrun"}
    assert not (plan / "live-smoke" / "budget-ledger.json").exists()


def test_tampered_index_cannot_claim_an_unjournaled_attempt(qualified, tmp_path):
    directory = copy_phase(qualified[0], tmp_path / "phase")
    index = read_sealed(directory / "phase-index.json")
    index.pop("seal_hash")
    entry = index["entries"]["compatibility-v1:gpt-6-sol"]
    entry["attempt"]["attempt_id"] = "forged"
    atomic_json(directory / "phase-index.json", seal(index))
    with pytest.raises(live.EvidenceError):
        live.verify_phase(directory)


@pytest.mark.parametrize("field,value", [
    ("check_passed", False), ("classification", "invented_pass"),
    ("world_checkpoint", None), ("controller_checkpoint", None),
    ("cleanup_confirmed", False), ("usage_total_tokens", 0), ("tool_request_count", 0),
])
async def test_resealed_index_summary_cannot_replace_archived_evidence(qualified, tmp_path, field, value):
    directory = copy_phase(qualified[0], tmp_path / "phase")
    index = read_sealed(directory / "phase-index.json")
    index.pop("seal_hash")
    attempt = index["entries"]["compatibility-v1:gpt-6-luna"]["attempt"]
    assert attempt[field] != value
    attempt[field] = value
    atomic_json(directory / "phase-index.json", seal(index))
    with pytest.raises(live.EvidenceError, match="indexed archive"):
        live.verify_phase(directory)
    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(live.EvidenceError, match="indexed archive"):
        await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
    assert harness.created == []


async def test_restored_valid_old_ledger_cannot_erase_journaled_charges(tmp_path, monkeypatch):
    directory = tmp_path / "phase"
    snapshot = {}
    original = live._PhaseRun._ensure_ledger

    def capture_empty_ledger(self):
        ledger = original(self)
        if not snapshot:
            for name in ("budget-ledger.json", "budget-ledger.json.identity.json"):
                snapshot[name] = (directory / name).read_bytes()
        return ledger

    monkeypatch.setattr(live._PhaseRun, "_ensure_ledger", capture_empty_ledger)
    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    report = await live.run_compatibility(directory, config(), **harness.kwargs())
    assert report["ledger"]["settled_tokens"] == 3 * 4321
    for name, data in snapshot.items():
        (directory / name).write_bytes(data)
    # Both restored files retain valid seals and identity. The independent journal must reject the rollback.
    assert read_sealed(directory / "budget-ledger.json")["attempts"] == {}
    with pytest.raises(live.EvidenceError, match="budget history"):
        live.verify_phase(directory)
    resumed = Harness(tmp_path / "resumed", {model: [] for model in MODELS})
    with pytest.raises(live.EvidenceError, match="budget history"):
        await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == []


@pytest.mark.parametrize("missing_confirmation", ["runtime_closed", "queue_reconciled"])
async def test_incomplete_cleanup_stops_admission_even_with_known_usage(tmp_path, missing_confirmation):
    directory = tmp_path / "phase"

    async def incomplete_cleanup(*args, **kwargs):
        result = await live.live_runtime.run_live_observer(*args, **kwargs)
        result[missing_confirmation] = False
        return result

    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    report = await live.run_compatibility(directory, config(), **harness.kwargs(observer=incomplete_cleanup))
    assert report["live_model_call_starts"] == 1
    assert report["ledger"]["settled_tokens"] == 4321 and not report["ledger"]["unresolved_reservations"]
    assert report["halted"]["reason"] == "cleanup_unreconciled"
    assert [row["status"] for row in report["entries"]] == ["archived", "unrun", "unrun"]
    assert live.verify_phase(directory)["live_model_call_starts"] == 1
    resumed = Harness(tmp_path / "resumed", harness.scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == [] and report["halted"]["reason"] == "cleanup_unreconciled"


async def test_crash_after_known_settlement_before_archive_keeps_cleanup_hold(tmp_path, monkeypatch):
    directory = tmp_path / "phase"

    async def incomplete_cleanup(*args, **kwargs):
        result = await live.live_runtime.run_live_observer(*args, **kwargs)
        result.update(runtime_closed=False, queue_reconciled=False)
        return result

    original = live._Journal.append

    def crash_before_archive(self, kind, **data):
        if kind == "attempt_archived":
            raise HardCrash()
        return original(self, kind, **data)

    monkeypatch.setattr(live._Journal, "append", crash_before_archive)
    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture()) for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs(observer=incomplete_cleanup))
    monkeypatch.setattr(live._Journal, "append", original)
    assert "usage_settled" in kinds(directory) and "attempt_archived" not in kinds(directory)
    assert live.verify_phase(directory)["ledger"]["settled_tokens"] == 4321
    resumed = Harness(tmp_path / "resumed", harness.scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == [] and report["live_model_call_starts"] == 1
    assert report["halted"]["reason"] == "cleanup_unreconciled"
    assert [row["status"] for row in report["entries"]] == ["incomplete_interrupted", "unrun", "unrun"]


async def test_unknown_usage_precedes_archived_cleanup_hold(tmp_path):
    directory = tmp_path / "phase"

    async def incomplete_cleanup(*args, **kwargs):
        result = await live.live_runtime.run_live_observer(*args, **kwargs)
        result["queue_reconciled"] = False
        return result

    harness = Harness(tmp_path / "homes", {model: qualifier_script(qualifier_fixture(), usage=None)
                                           for model in MODELS})
    report = await live.run_compatibility(directory, config(), **harness.kwargs(observer=incomplete_cleanup))
    assert report["halted"]["reason"] == "unknown_usage_hold" and report["live_model_call_starts"] == 1
    live.reconcile_usage(directory, report["entries"][0]["attempt_id"], 1000, evidence="provider usage export")
    resumed = Harness(tmp_path / "resumed", harness.scripts)
    report = await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == [] and report["halted"]["reason"] == "cleanup_unreconciled"


@pytest.mark.parametrize("status", ["started", "incomplete_interrupted"])
@pytest.mark.parametrize("field,value", [("check_passed", True), ("cleanup_confirmed", True),
                                        ("reservation_id", "invented-reservation")])
async def test_nonarchived_index_cannot_invent_cleanup_or_outcome(tmp_path, status, field, value):
    directory = tmp_path / "phase"

    async def crashing(*args, **kwargs):
        raise HardCrash()

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs(observer=crashing))
    if status == "incomplete_interrupted":
        await live.run_compatibility(directory, config(), resume=True, **harness.kwargs())
    index = read_sealed(directory / "phase-index.json")
    index.pop("seal_hash")
    entry = index["entries"]["compatibility-v1:gpt-6-luna"]
    assert entry["status"] == status
    entry["attempt"][field] = value
    atomic_json(directory / "phase-index.json", seal(index))
    with pytest.raises(live.EvidenceError, match="nonarchived attempt differs"):
        live.verify_phase(directory)
    resumed = Harness(tmp_path / "resumed", harness.scripts)
    with pytest.raises(live.EvidenceError, match="nonarchived attempt differs"):
        await live.run_compatibility(directory, config(), resume=True, **resumed.kwargs())
    assert resumed.created == []


async def test_interrupted_status_requires_journaled_reconciliation(tmp_path):
    directory = tmp_path / "phase"

    async def crashing(*args, **kwargs):
        raise HardCrash()

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs(observer=crashing))
    index = read_sealed(directory / "phase-index.json")
    index.pop("seal_hash")
    entry = index["entries"]["compatibility-v1:gpt-6-luna"]
    entry["status"] = entry["attempt"]["status"] = "incomplete_interrupted"
    atomic_json(directory / "phase-index.json", seal(index))
    with pytest.raises(live.EvidenceError, match="lacks recorded reconciliation"):
        live.verify_phase(directory)


async def test_report_never_qualifies_a_nonarchived_row(tmp_path):
    directory = tmp_path / "phase"

    async def crashing(*args, **kwargs):
        raise HardCrash()

    harness = Harness(tmp_path / "homes", {model: [] for model in MODELS})
    with pytest.raises(HardCrash):
        await live.run_compatibility(directory, config(), **harness.kwargs(observer=crashing))
    state = live._PhaseState(directory)
    try:
        attempt = state.index["entries"]["compatibility-v1:gpt-6-luna"]["attempt"]
        attempt.update(check_passed=True, classification="qualified")
        report = live._report(state)
        assert report["qualified_models"] == [] and report["entries"][0]["check_passed"] is None
        assert report["entries"][0]["classification"] is None
        state.plan["phase"] = "collection"
        assert live._report(state)["valid_outcomes"] == 0
    finally:
        state.journal.close()
