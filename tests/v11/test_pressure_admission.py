"""Retained root phases and bindings are checked before admission phase filters."""

import asyncio
import hashlib
import shutil
from collections import deque

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.config import PRESSURE_ARMS
from swarm_auth_bench.peer_reporting_v11.receipts import check_receipt
from swarm_auth_bench.peer_reporting_v11.review_population import registered_pressure_population
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

from .live_fakes import Harness, run_phase
from .r6_observers import observer
from .receipt_helpers import commit_receipt
from .test_r6_remaining_consistency import v2_compatibility as v2_compatibility


def primary_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
            and (path.name == "journal.jsonl" or "attempts" in path.parts)}


@pytest.fixture(scope="module")
def admission_inputs(tmp_path_factory, wp6_study, wp6_inputs, v2_compatibility):
    base = tmp_path_factory.mktemp("pressure-admission")
    study = base / "study"
    shutil.copytree(wp6_study[0], study)
    compatibility, original_receipts = v2_compatibility
    receipts = base / "receipts"
    shutil.copytree(original_receipts, receipts)
    bundle = load_bundle()
    options = {"study_directory": study, "compatibility_directories": [compatibility],
               "receipt_directory": receipts, "bundle": bundle}
    candidate = live.build_phase_plan("calibration", wp6_inputs["caps_record"], revision="replacement",
                                     arms=["pressure_gate_xhigh"], **options)
    built = live.build_phase_plan("calibration", wp6_inputs["caps_record"], revision="gate-pilot",
                                 arms=PRESSURE_ARMS[:4], **options)
    root = study / "roots" / "gate-pilot"
    live.prepare_live_root(root, built, **options)
    plan = live.read_live_plan(root)
    queues = {(lane["model"], lane["reasoning_effort"]): deque(read_sealed(
        root / lane["path"] / "phase-plan.json")["planned_order"]) for lane in plan["lanes"]}

    def script_for(model, effort):
        entry = queues[model, effort].popleft()
        raw = observer(built[2][entry["fixture_id"]], "hit")
        actions = [("tool", action["tool"], action["arguments"]) for action in raw["actions"]]
        actions.insert(-1, ("usage", 2000))
        return actions + [("message", raw["final_response"])]

    harness = Harness(base / "homes", script_for)
    assert asyncio.run(run_phase(root, plan, harness, **options))["status"] == "complete"
    assert len(harness.created) == 156
    commit_receipt(root, receipts, study_directory=study)
    return study, root, plan, receipts, compatibility, candidate


@pytest.fixture
def retained_root(tmp_path, admission_inputs):
    original_study, original_root, _, original_receipts, compatibility, candidate = admission_inputs
    study = tmp_path / "study"
    study.mkdir()
    shutil.copy2(original_study / live.STUDY_MANIFEST, study / live.STUDY_MANIFEST)
    shutil.copytree(original_study / "fixtures", study / "fixtures")
    root = study / "roots" / original_root.name
    shutil.copytree(original_root, root)
    shutil.copytree(original_study / live.STUDY_REGISTRY, study / live.STUDY_REGISTRY)
    receipts = tmp_path / "receipts"
    shutil.copytree(original_receipts, receipts)
    # Start claims are derived. Keep all journal starts and primary archives.
    assert not (study / live.START_LEDGER).exists()
    check_receipt(root, receipts, study_directory=study)
    return study, root, live.read_live_plan(root), receipts, compatibility, candidate


def replace_plan(study, root, previous, altered):
    atomic_json(root / live.LIVE_PLAN_FILE, altered)
    registry = study / live.STUDY_REGISTRY
    for subdirectory in ("", live.FINALIZED_DIRECTORY):
        path = registry / subdirectory / f"{previous['seal_hash']}.json"
        record = {key: value for key, value in read_sealed(path).items() if key != "seal_hash"}
        record.update(plan_hash=altered["seal_hash"], phase=altered["phase"])
        if "live_plan_hash" in record:
            record["live_plan_hash"] = altered["seal_hash"]
        atomic_json(path.with_name(f"{altered['seal_hash']}.json"), seal(record))
        path.unlink()


@pytest.mark.parametrize("matching_plan_receipt", [False, True])
def test_retained_phase_consistency_refuses_planning_and_preparation(
        retained_root, tmp_path, matching_plan_receipt):
    study, root, plan, receipts, compatibility, candidate = retained_root
    before = primary_hashes(root)
    manifest_raw = (study / live.STUDY_MANIFEST).read_bytes()
    started = {record["data"]["entry_id"] for records in live.lane_journals(root, plan).values()
               for record in records if record["kind"] == "attempt_started"}
    assert len(started) == 156
    planned = {entry["entry_id"] for lane in candidate[1].values() for entry in lane["planned_order"]}
    assert len(planned) == 12 and planned <= started
    altered = seal({**{key: value for key, value in plan.items() if key != "seal_hash"}, "phase": "smoke"})
    replace_plan(study, root, plan, altered)
    with pytest.raises(ValueError, match="receipt mismatch"):
        check_receipt(root, receipts, study_directory=study)
    if matching_plan_receipt:
        commit_receipt(root, receipts, study_directory=study)
        check_receipt(root, receipts, study_directory=study)
    options = {"study_directory": study, "compatibility_directories": [compatibility],
               "receipt_directory": receipts, "bundle": load_bundle(), "prior_roots": []}
    message = "phase differs from its sealed-study assignments"
    with pytest.raises(ValueError, match=message):
        live.checked_pressure_registrations(study, receipt_directory=receipts)
    with pytest.raises(ValueError, match=message):
        live.prior_root_ledger([], phase="calibration", source=plan["source"], study_directory=study,
            bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_P), receipt_directory=receipts)
    with pytest.raises(ValueError, match=message):
        live.build_phase_plan("calibration", plan["caps"], revision="replacement",
                              arms=["pressure_gate_xhigh"], **options)
    with pytest.raises(ValueError, match=message):
        registered_pressure_population(study, phase="calibration",
            study_manifest_hash=plan["source"]["study_manifest_hash"],
            source_plan_hash=altered["seal_hash"], receipt_directory=receipts)
    replacement = study / "roots" / "replacement"
    registry_before = {path.relative_to(study): path.read_bytes()
                       for path in (study / live.STUDY_REGISTRY).rglob("*.json")}
    harness = Harness(tmp_path / "replacement-homes", lambda *args: pytest.fail("unexpected fake session"))

    async def prepare_and_run():
        live.prepare_live_root(replacement, candidate, **options)
        await run_phase(replacement, live.read_live_plan(replacement), harness, **options)

    with pytest.raises(ValueError, match=message):
        asyncio.run(prepare_and_run())
    assert not replacement.exists() and harness.created == []
    assert {path.relative_to(study): path.read_bytes()
            for path in (study / live.STUDY_REGISTRY).rglob("*.json")} == registry_before
    assert primary_hashes(root) == before
    assert (study / live.STUDY_MANIFEST).read_bytes() == manifest_raw


@pytest.mark.parametrize("change", ["assignment", "receipt"])
def test_retained_root_checks_precede_unrelated_phase_filter(retained_root, change):
    study, root, plan, receipts, _, _ = retained_root
    before = primary_hashes(root)
    if change == "assignment":
        altered = {key: value for key, value in plan.items() if key != "seal_hash"}
        lane = altered["lanes"][0]
        path = root / lane["path"] / "phase-plan.json"
        lane_plan = {key: value for key, value in read_sealed(path).items() if key != "seal_hash"}
        lane_plan["planned_order"][0]["instructions"] += "\nRetained instruction consistency check."
        lane_plan = seal(lane_plan)
        atomic_json(path, lane_plan)
        lane["plan_hash"] = lane_plan["seal_hash"]
        replace_plan(study, root, plan, seal(altered))
        commit_receipt(root, receipts, study_directory=study)
        check_receipt(root, receipts, study_directory=study)
        message = "assignment instructions differs from sealed study"
    else:
        receipts = receipts / "missing"
        message = "receipt"
    with pytest.raises(ValueError, match=message):
        live.prior_root_ledger([], phase="smoke", source=plan["source"], study_directory=study,
            bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_P), receipt_directory=receipts)
    assert primary_hashes(root) == before
