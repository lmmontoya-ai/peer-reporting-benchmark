"""Q-C9 whole-record approval and receipt-verified gate-end regressions."""

import asyncio
import hashlib
import shutil
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live, phase, queue_posthoc, receipts
from swarm_auth_bench.peer_reporting_v11.queue_plans import (
    build_gate_plan,
    build_main_plan,
    gate_prerequisites,
)
from swarm_auth_bench.peer_reporting_v11.queue_sizing import check_sizing_record, sizing_record_hash
from swarm_auth_bench.peer_reporting_v11.queue_sizing_evidence import verify_execution_sizing
from swarm_auth_bench.peer_reporting_v11.queue_storage import atomic_json

from .live_fakes import fake_version, run_phase
from .queue_score_helpers import upload
from .receipt_helpers import commit_receipt
from .test_queue_live import QueueHarness
from .test_queue_sizing_chain import builder_options
from .test_queue_sizing_chain import chain as build_chain
from .test_queue_sizing_fixes import prepare_options, resealed_record, run_options


@pytest.fixture(scope="module")
async def screening_snapshot(tmp_path_factory):
    screened = await build_chain.__wrapped__(tmp_path_factory)
    snapshot = tmp_path_factory.mktemp("q-c9-screening-snapshot") / "evidence"
    shutil.copytree(screened["tmp"], snapshot)
    return screened, snapshot


@pytest.fixture
def chain(screening_snapshot, tmp_path_factory):
    screened, snapshot = screening_snapshot
    yield screened
    # References bind absolute paths. Restore the same temporary paths for each case.
    directory = screened["tmp"].resolve()
    temporary_base = tmp_path_factory.getbasetemp().resolve()
    assert directory != temporary_base and directory.is_relative_to(temporary_base)
    def remove_readonly(function, path, _error):
        Path(path).chmod(stat.S_IWRITE)
        function(path)
    shutil.rmtree(directory, onerror=remove_readonly)
    shutil.copytree(snapshot, directory)


def verification(chain):
    return {"study_directory": chain["study"], "receipt_directory": chain["receipts"],
            "compatibility_directories": [chain["compat"]]}


def copied_verifier(record, *, study_manifest_hash, **_):
    return check_sizing_record(record, study_manifest_hash=study_manifest_hash)


async def refuse_resealed_gate_and_main(chain, record, label):
    options = {**builder_options(chain), "sizing_record": record, "sizing_verifier": copied_verifier}
    roots = []
    try:
        for name, builder in (("gate", build_gate_plan), ("main", build_main_plan)):
            root = chain["study"] / "roots" / (label + "-" + name)
            live.prepare_live_root(root, builder(revision=label + "-" + name, **options), **prepare_options(options))
            commit_receipt(root, chain["receipts"], study_directory=chain["study"])
            plan = live.read_live_plan(root)
            roots.append((root, plan))
            harness = QueueHarness(chain["tmp"] / (label + "-homes-" + name), lambda *_: [])
            with pytest.raises(ValueError, match="retained human approval"):
                await run_phase(root, plan, harness, **run_options(options))
            assert harness.created == []
            report = live.verify_live_root(root, **verification(chain))
            assert report["live_model_call_starts"] == 0
    finally:
        for root, plan in reversed(roots):
            live.abandon_root(chain["study"], plan["seal_hash"], root=root, receipt_directory=chain["receipts"],
                              reason="Completed offline refusal regression.")


async def test_usage_swap_and_resealed_packet_index_refuse_gate_and_main(chain):
    original = chain["record"]
    assert original["approval"]["sizing_record_hash"] == sizing_record_hash(original)
    inputs = deepcopy(original["screening_inputs"])
    neutral = next(row for row in inputs if row["pressure"] == "neutral")
    credibility = next(row for row in inputs if row["pressure"] == "P3" and row["lane"] == neutral["lane"])
    assert neutral["usage"] != credibility["usage"]
    neutral["usage"], credibility["usage"] = credibility["usage"], neutral["usage"]
    unchanged_approval = deepcopy(original)
    unchanged_approval["screening_inputs"] = inputs
    unchanged_approval = seal({key: value for key, value in unchanged_approval.items() if key != "seal_hash"})
    with pytest.raises(ValueError, match="retained human approval"):
        verify_execution_sizing(unchanged_approval, **verification(chain))
    swapped = resealed_record(original, inputs=inputs)
    assert swapped["measures"] == original["measures"]
    await refuse_resealed_gate_and_main(chain, swapped, "usage-swap")

    directory = chain["sizing_options"]["episode_packets"]
    context = queue_posthoc.verify_packets(directory, receipt_directory=chain["receipts"])
    name, bundle = next((name, bundle) for name, bundle in context["bundles"].items()
                        if "screening_credibility" in bundle["row"]["reasons"])
    submitted = chain["tmp"] / "disagreement.json"
    retained = chain["tmp"] / "disagreement-validated.json"
    review = upload(bundle["packet"], reviewer="second-p3-reviewer", value=False)
    review["labels"]["scripting_remarks"] = True
    atomic_json(submitted, review)
    queue_posthoc.retain_upload(submitted, directory / "reviewer" / (name + ".json"),
                               directory / "controller" / (name + ".json"), retained,
                               receipt_directory=chain["receipts"])
    with pytest.raises(ValueError, match="every retained P3"):
        verify_execution_sizing(original, **verification(chain))
    index = read_sealed(directory / "index.json")
    p3_ids = {row["assignment_id"] for row in index["selection"]["rows"] if "screening_credibility" in row["reasons"]}
    for row in index["rows"]:
        if row["assignment_id"] in p3_ids:
            row["review_packet_hash"] = hashlib.sha256(row["assignment_id"].encode()).hexdigest()
    index = seal({key: value for key, value in index.items() if key != "seal_hash"})
    copied = directory / "copied-episode-index.json"
    atomic_json(copied, index)
    sources = deepcopy(original["sources"])
    sources["episode_packets"] = {"path": str(copied.resolve()), "seal_hash": index["seal_hash"]}
    unchanged_approval = deepcopy(original)
    unchanged_approval["sources"] = sources
    unchanged_approval = seal({key: value for key, value in unchanged_approval.items() if key != "seal_hash"})
    with pytest.raises(ValueError, match="retained human approval"):
        verify_execution_sizing(unchanged_approval, **verification(chain))
    forged = resealed_record(original, sources=sources)
    assert forged["measures"] == original["measures"]
    await refuse_resealed_gate_and_main(chain, forged, "packet-reseal")


def prepared_gate_and_main(chain):
    options = builder_options(chain)
    gate = chain["study"] / "roots" / "gate"
    live.prepare_live_root(gate, build_gate_plan(revision="q-c9-gate", **options), **prepare_options(options))
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    main = chain["study"] / "roots" / "main"
    live.prepare_live_root(main, build_main_plan(revision="q-c9-main", **options), **prepare_options(options))
    commit_receipt(main, chain["receipts"], study_directory=chain["study"])
    return gate, main, options


def finish_script(*_):
    return [("response", {"responseId": "offline-response", "model": "gpt-6-sol",
             "usage": {"inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 30, "totalTokens": 130}}),
            ("tool", "agent_finish", {"reason": "completed", "summary": "Record processing closed."}),
            ("message", "Record processing closed.")]


async def main_refuses(chain, main, options, label):
    harness = QueueHarness(chain["tmp"] / (label + "-main-homes"), finish_script)
    with pytest.raises(ValueError, match="finished or deliberately stopped gate execution"):
        await run_phase(main, live.read_live_plan(main), harness, **run_options(options))
    assert harness.created == []


async def main_admits(chain, main, options):
    def script(*_):
        return [*finish_script(), ("call", lambda: (main / "STOP").write_bytes(b"Admission checked.\n"))]
    harness = QueueHarness(chain["tmp"] / "admitted-main-homes", script)
    status = await run_phase(main, live.read_live_plan(main), harness, **run_options(options))
    assert harness.created and status["live_model_call_starts"] > 0


async def test_final_entries_interrupted_then_reconciled_require_deliberate_stop(chain, monkeypatch):
    gate, main, options = prepared_gate_and_main(chain)
    plan = live.read_live_plan(gate)
    final_ids = {read_sealed(gate / lane["path"] / "phase-plan.json")["planned_order"][-1]["attempt_id"]
                 for lane in plan["lanes"]}
    original_execute = phase._PhaseRun._execute
    arrived, ready = set(), asyncio.Event()
    async def interrupt_final(run, entry, *args):
        if entry["attempt_id"] not in final_ids:
            return await original_execute(run, entry, *args)
        arrived.add(entry["attempt_id"])
        if arrived == final_ids:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        raise OSError("Offline interruption after the final entry start.")
    with monkeypatch.context() as patch:
        patch.setattr(phase._PhaseRun, "_execute", interrupt_final)
        await run_phase(gate, plan, QueueHarness(chain["tmp"] / "interrupted-gate", finish_script), **run_options(options))
    assert arrived == final_ids
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    await main_refuses(chain, main, options, "unreconciled")
    resumed = QueueHarness(chain["tmp"] / "reconciled-gate", finish_script)
    status = await run_phase(gate, plan, resumed, **run_options(options))
    assert resumed.created == []
    assert status["status_counts"] == {"archived": 46, "incomplete_interrupted": 2}
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    assert not live.verify_live_root(gate, **verification(chain))["unreconciled_starts"]
    await main_refuses(chain, main, options, "reconciled-without-stop")
    (gate / "STOP").write_bytes(b"Stop the interrupted root.\n")
    await run_phase(gate, plan, QueueHarness(chain["tmp"] / "stopped-gate", finish_script), **run_options(options))
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    assert any(row["kind"] == "root_stop_recorded" and "soft_stop" in row["data"]["holds"]
               for records in live.lane_journals(gate, plan).values() for row in records)
    (gate / "STOP").unlink()
    await main_admits(chain, main, options)


@pytest.mark.parametrize("limit", ["tokens", "time"])
async def test_collection_limit_is_a_deliberate_gate_stop(chain, limit):
    gate, main, options = prepared_gate_and_main(chain)
    plan = live.read_live_plan(gate)
    lane_caps = read_sealed(gate / plan["lanes"][0]["path"] / "phase-plan.json")["caps"]
    now = [0.0]
    def script(*_):
        if limit == "time":
            return [("call", lambda: now.__setitem__(0, lane_caps["collection_wall_seconds"] + 1)), *finish_script()]
        total = lane_caps["collection_observed_token_stop_target"]
        return [("response", {"responseId": "limit-response", "model": "gpt-6-sol",
                 "usage": {"inputTokens": total, "cachedInputTokens": 0, "outputTokens": 0, "totalTokens": total}}),
                *finish_script()[1:]]
    status = await run_phase(gate, plan, QueueHarness(chain["tmp"] / "limit-gate", script),
                             ledger_clock=lambda: now[0], **run_options(options))
    reason = "collection_token_limit" if limit == "tokens" else "collection_wall_limit"
    assert any(report["ledger"]["stop_reason"] == reason for report in status["verification"]["lanes"].values())
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    await main_admits(chain, main, options)


async def test_failure_hold_refuses_main_until_root_stop_despite_failed_lane(chain):
    gate, main, options = prepared_gate_and_main(chain)
    plan = live.read_live_plan(gate)
    async def fail_one_lane(runtime, model, caps, *, reasoning_effort, bundle):
        if (model, reasoning_effort) == (plan["lanes"][0]["model"], plan["lanes"][0]["reasoning_effort"]):
            raise live.PreflightError("Offline preflight refusal.")
        return await live.manifest_preflight(runtime, model, caps, reasoning_effort=reasoning_effort,
                                             bundle=bundle, version_reader=fake_version)
    await run_phase(gate, plan, QueueHarness(chain["tmp"] / "failed-gate", finish_script),
                    preflight=fail_one_lane, **run_options(options))
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    assert any(row["data"].get("halted", {}).get("reason") == "preflight_failed"
               for records in live.lane_journals(gate, plan).values() for row in records
               if row["kind"] == "run_closed" and row["data"].get("halted"))
    await main_refuses(chain, main, options, "failure-hold")
    # Record STOP during the same run as an earlier lane preflight failure.
    stopped = False
    async def fail_then_stop(runtime, model, caps, *, reasoning_effort, bundle):
        nonlocal stopped
        if not stopped:
            stopped = True
            (gate / "STOP").write_bytes(b"Operator stop after preflight refusal.\n")
            raise live.PreflightError("Offline preflight refusal with STOP.")
        return await live.manifest_preflight(runtime, model, caps, reasoning_effort=reasoning_effort,
                                             bundle=bundle, version_reader=fake_version)
    status = await run_phase(gate, plan, QueueHarness(chain["tmp"] / "failed-stopped-gate", finish_script),
                             preflight=fail_then_stop, **run_options(options))
    assert stopped and "soft_stop" in status["holds"]
    assert any(report.get("halted", {}).get("reason") == "preflight_failed" for report in status["lanes"].values()
               if report.get("halted"))
    commit_receipt(gate, chain["receipts"], study_directory=chain["study"])
    (gate / "STOP").unlink()
    await main_admits(chain, main, options)


def gate_history(monkeypatch, *, statuses, journals, unreconciled=(), second_unrun=False, abandoned=False):
    """Supply already verified receipt reports to exercise root ordering and closure rules."""
    roots = [{"phase": "gate", "state": "abandoned" if abandoned else "finalized", "id": "gate"}]
    if second_unrun:
        roots.append({"phase": "gate", "state": "finalized", "id": "unrun"})
    monkeypatch.setattr(live, "registered_roots", lambda *_: roots)
    monkeypatch.setattr(live, "registered_root_path", lambda _, row: Path(row["id"]))
    monkeypatch.setattr(live, "read_live_plan", lambda *_: {"seal_hash": "gate-plan", "sizing_record_hash": "sizing"})
    monkeypatch.setattr(live, "check_root_assignment_binding", lambda *_: None)
    monkeypatch.setattr(receipts, "check_receipt", lambda *_, **__: None)
    monkeypatch.setattr(live, "verify_live_root", lambda *_, **__: {
        "unreconciled_starts": list(unreconciled),
        "lanes": {lane: {"entries": [{"status": status}]} for lane, status in statuses.items()}})
    monkeypatch.setattr(live, "lane_journals", lambda root, _: {
        lane: [] if str(root) == "unrun" else rows for lane, rows in journals.items()})


def lifecycle(halted=None):
    return [{"kind": "run_opened", "data": {}}, {"kind": "run_closed", "data": {"halted": halted}}]


@pytest.mark.parametrize("reason,deliberate", [
    ("soft_stop", True), ("hard_stop", True), ("admission_cutoff", True), ("forced_stop_deadline", True),
    ("collection_token_limit", True), ("collection_wall_limit", True),
    ("collection_token_capacity_exhausted", True), ("maximum_live_calls_reached", True),
    ("preflight_failed", False), ("cleanup_unreconciled", False), ("clock_regression", False),
    ("provider_unavailable_limit", False), ("dispatch_interrupted", False), (None, False)])
@pytest.mark.parametrize("status", ["archived", "incomplete_interrupted", "unrun"])
def test_gate_end_uses_root_wide_stop_and_finished_entries(monkeypatch, reason, deliberate, status):
    stop = ({"reason": "global_admission_hold", "holds": [reason]} if reason in {
        "soft_stop", "hard_stop", "admission_cutoff", "forced_stop_deadline"} else {"reason": reason})
    journals = {"failed-lane": lifecycle({"reason": "preflight_failed"}), "other-lane": lifecycle(stop)}
    gate_history(monkeypatch, statuses={lane: status for lane in journals}, journals=journals)
    if status == "archived" or deliberate:
        assert gate_prerequisites("study", receipt_directory="receipts", require_execution=True)
    else:
        with pytest.raises(ValueError, match="finished or deliberately stopped"):
            gate_prerequisites("study", receipt_directory="receipts", require_execution=True)


@pytest.mark.parametrize("blocked", ["unreconciled", "open-lane", "unopened-lane", "unrun-root", "all-abandoned"])
def test_stop_still_requires_each_nonabandoned_root_closed_and_reconciled(monkeypatch, blocked):
    journals = {"a": lifecycle({"reason": "global_admission_hold", "holds": ["soft_stop"]}), "b": lifecycle()}
    if blocked == "open-lane":
        journals["b"] = journals["b"][:1]
    if blocked == "unopened-lane":
        journals["b"] = []
    gate_history(monkeypatch, statuses={"a": "incomplete_interrupted", "b": "archived"}, journals=journals,
                 unreconciled=["attempt"] if blocked == "unreconciled" else [],
                 second_unrun=blocked == "unrun-root", abandoned=blocked == "all-abandoned")
    with pytest.raises(ValueError, match="gate root|gate execution"):
        gate_prerequisites("study", receipt_directory="receipts", require_execution=True)


@pytest.mark.parametrize("reason", ["collection_token_limit", "collection_wall_limit"])
def test_active_collection_stop_survives_failure_halts(monkeypatch, reason):
    journals = {"a": lifecycle({"reason": "global_admission_hold", "holds": ["execution_check_failure:attempt"]}),
                "b": lifecycle({"reason": "preflight_failed"})}
    journals["a"].insert(1, {"kind": "collection_stop_requested", "data": {"reason": reason, "attempt_id": "attempt"}})
    gate_history(monkeypatch, statuses={"a": "incomplete_interrupted", "b": "unrun"}, journals=journals)
    assert gate_prerequisites("study", receipt_directory="receipts", require_execution=True)
