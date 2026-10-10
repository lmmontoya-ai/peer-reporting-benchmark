"""Derived records must agree with the registered archive and sealed study."""

import asyncio
import json
import shutil
import subprocess
import sys
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live, live_review, phase
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.pilot_decision import build_pilot_decision
from swarm_auth_bench.peer_reporting_v11.receipts import check_receipt
from swarm_auth_bench.peer_reporting_v11.review import (
    build_review_bundle,
    human_endpoints,
    resolve_packet_reviews,
    validate_review_upload,
    write_review_packets,
)
from swarm_auth_bench.peer_reporting_v11.review_plan import pressure_review_selection
from swarm_auth_bench.peer_reporting_v11.review_population import registered_pressure_partitions
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P
from swarm_auth_bench.peer_reporting_v11.score import score_trial

from .live_fakes import Harness, PauseClock, overload_steps, qualifier_steps, run_phase
from .r6_observers import observer
from .receipt_helpers import commit_receipt
from .review_helpers import review, upload
from .test_live_overload import long_authorization, started_at


def reseal(path, record):
    atomic_json(path, seal({key: value for key, value in record.items() if key != "seal_hash"}))


def primary_bytes(root):
    return {path: path.read_bytes() for path in root.rglob("*")
            if path.is_file() and (path.name == "journal.jsonl" or "attempts" in path.parts)}


@pytest.fixture(scope="module")
def v2_compatibility(tmp_path_factory, wp6_inputs):
    base = tmp_path_factory.mktemp("remaining-consistency-compat")
    root = base / "compatibility"
    built = live.build_compatibility_plan(wp6_inputs["caps_record"], revision="consistency-compat",
                                          tool_schema_version=TOOL_SCHEMA_VERSION_P)
    live.prepare_live_root(root, built)
    harness = Harness(base / "homes", lambda *args: qualifier_steps(next(iter(built[2].values()))))
    result = asyncio.run(run_phase(root, live.read_live_plan(root), harness, bundle=load_bundle()))
    assert result["status"] == "complete"
    receipts = commit_receipt(root, base / "receipts")
    return root, receipts


@pytest.fixture
def pressure_roots(tmp_path, wp6_study, wp6_inputs, v2_compatibility):
    study = tmp_path / "study"
    shutil.copytree(wp6_study[0], study)
    compatibility, original_receipts = v2_compatibility
    receipts = tmp_path / "receipts"
    shutil.copytree(original_receipts, receipts)
    manifest = live.read_study_manifest(study)
    rows = [row for row in manifest["assignments"] if row["arm"] == "pressure_gate_xhigh"][:2]
    fixtures = {row["fixture_id"]: {key: value for key, value in
                read_sealed(study / manifest["fixtures"][row["fixture_id"]]["path"]).items()
                if key != "seal_hash"} for row in rows}
    source = {"kind": "study_manifest", "study_manifest_hash": manifest["seal_hash"]}
    lane = rows[0]["model"] + "-" + rows[0]["effort"]
    gates = live.check_phase_gates("calibration", [lane], bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_P),
        source=source, compatibility_directories=[compatibility], study_directory=study,
        receipt_directory=receipts)["evidence"]

    def prepare(index, name, prior=()):
        ledger = live.prior_root_ledger(prior, phase="calibration", source=source, study_directory=study,
                                        receipt_directory=receipts) if prior else None
        built = live.build_assignment_plan("calibration", [rows[index]], fixtures, wp6_inputs["caps_record"],
            revision=name, source=source, gate_evidence=gates, study_manifest=manifest, consumed_attempts=ledger)
        root = study / "roots" / name
        live.prepare_live_root(root, built, study_directory=study, compatibility_directories=[compatibility],
                               receipt_directory=receipts, prior_roots=prior)
        return root, live.read_live_plan(root)

    async def run(root, plan, harness, clock=None, prior=()):
        return await run_phase(root, plan, harness, bundle=load_bundle(), auth=long_authorization(plan),
            study_directory=study, compatibility_directories=[compatibility], receipt_directory=receipts,
            prior_roots=prior, **({"wall_clock": clock, "pause_sleep": clock.sleep} if clock else {}))

    return study, receipts, rows, fixtures, prepare, run


@pytest.mark.parametrize("state", ["retained_attempt", "settlement_only"])
@pytest.mark.parametrize("change", ["timestamps", "count", "unchanged"])
async def test_interrupted_pause_primary_evidence_at_successor_admission(
        tmp_path, pressure_roots, monkeypatch, state, change):
    study, receipts, rows, fixtures, prepare, run = pressure_roots
    root, plan = prepare(0, "first")
    clock = PauseClock()
    original = phase.attempt_summary

    def incomplete(payload):
        if payload["orchestrator"]["provider_pause"] is not None:
            raise OSError("simulated incomplete archive")
        return original(payload)

    def unavailable(*args, **kwargs):
        raise OSError("simulated pause write interruption")

    with monkeypatch.context() as patch:
        patch.setattr(phase, "attempt_summary", incomplete)
        if state == "settlement_only":
            patch.setattr(live, "record_study_pause", unavailable)
        result = await run(root, plan, Harness(tmp_path / "homes", lambda *args: overload_steps()), clock)
    assert result["status"] == "held" and result["live_model_call_starts"] == 1
    assert not any(entry["kind"] == "attempt_archived" for entries in live.lane_journals(root, plan).values()
                   for entry in entries)
    commit_receipt(root, receipts, study_directory=study)
    before = primary_bytes(root)
    if state == "settlement_only":
        assert not list(root.glob("lanes/*/attempts/*/attempt.json"))
        clock.now += 30
        recovered = live.reconcile_study_pauses(study, plan, wall_clock=clock, receipt_directory=receipts)
        assert len(recovered) == 1
    else:
        export = tmp_path / "interrupted-export"
        live_review.export_live_review(root, export, study_directory=study, receipt_directory=receipts,
                                        scorer=score_trial, pressure_review_selector=pressure_review_selection)
        exported = read_sealed(export / "index.json")["rows"][0]
        assert exported["status"] == "incomplete_interrupted" and exported["score"] is None
    pause_path = next((study / live.PAUSE_LEDGER).glob("*.json"))
    record = read_sealed(pause_path)
    retained_pause = deepcopy(record["pause"])
    successor, next_plan = prepare(1, "second", [root])
    if change == "timestamps":
        record["pause"]["paused_at"] -= 7200
        record["pause"]["resume_at"] -= 7200
    elif change == "count":
        record["pause"]["window_count"] += 1
    reseal(pause_path, record)
    starts = []
    raw = observer(fixtures[rows[1]["fixture_id"]], "hit")
    actions = [("tool", action["tool"], action["arguments"]) for action in raw["actions"]]
    actions.insert(-1, ("usage", 2000))
    harness = Harness(tmp_path / "successor-homes", lambda *args: actions + [("message", "Inventory completed.")],
                      observe=started_at(clock, starts))
    if change == "unchanged":
        assert (await run(successor, next_plan, harness, clock, [root]))["status"] == "complete"
        assert min(time for _, _, time in starts) >= retained_pause["resume_at"]
    else:
        with pytest.raises(ValueError, match="as journaled"):
            live.study_provider_pauses(study, receipt_directory=receipts)
        with pytest.raises(ValueError, match="as journaled"):
            await run(successor, next_plan, harness, clock, [root])
        assert harness.created == []
    assert primary_bytes(root) == before
    check_receipt(root, receipts, study_directory=study)


@pytest.fixture
def native_packet(tmp_path, pressure_roots):
    study, receipts, rows, fixtures, prepare, run = pressure_roots
    root, plan = prepare(0, "packet-source")
    raw = observer(fixtures[rows[0]["fixture_id"]], "hit")
    actions = [("tool", action["tool"], action["arguments"]) for action in raw["actions"]]
    actions.insert(-1, ("usage", 2000))
    harness = Harness(tmp_path / "homes", lambda *args: actions + [("message", "Inventory completed.")])
    assert asyncio.run(run(root, plan, harness))["status"] == "complete"
    commit_receipt(root, receipts, study_directory=study)
    export = tmp_path / "export"
    live_review.export_live_review(root, export, study_directory=study, receipt_directory=receipts,
                                    scorer=score_trial, pressure_review_selector=pressure_review_selection)
    packets = tmp_path / "packets"
    write_review_packets(export, read_sealed(export / "index.json")["pressure_review_selection"], packets,
                          receipt_directory=receipts)
    controller = read_sealed(next((packets / "controller").glob("*.json")))
    assert controller["plan_row"]["second_review"] is True
    return root, export, controller


@pytest.mark.parametrize("consumer", ["export", "packets", "pilot"])
def test_sibling_matching_phase_copies_refuse(tmp_path, pressure_roots, native_packet, consumer):
    study, receipts, rows, fixtures, prepare, run = pressure_roots
    root, export, controller = native_packet
    sibling, plan = prepare(1, "sibling", [root])
    commit_receipt(sibling, receipts, study_directory=study)
    before = primary_bytes(root), primary_bytes(sibling)
    plan["phase"] = "smoke"
    old_hash = plan["seal_hash"]
    reseal(sibling / live.LIVE_PLAN_FILE, plan)
    new_hash = live.read_live_plan(sibling)["seal_hash"]
    for subdirectory in ("", live.FINALIZED_DIRECTORY):
        path = study / live.STUDY_REGISTRY / subdirectory / (old_hash + ".json")
        record = read_sealed(path)
        record.update(plan_hash=new_hash, phase="smoke")
        reseal(path.with_name(new_hash + ".json"), record)
        path.unlink()
    with pytest.raises(ValueError, match="phase differs from its sealed-study assignments"):
        if consumer == "export":
            live_review.export_live_review(root, tmp_path / "again", study_directory=study,
                receipt_directory=receipts, scorer=score_trial, pressure_review_selector=pressure_review_selection)
        elif consumer == "packets":
            write_review_packets(export, read_sealed(export / "index.json")["pressure_review_selection"],
                                  tmp_path / "again", receipt_directory=receipts)
        else:
            index = read_sealed(export / "index.json")
            reference = {"path": str(export / "index.json"), "seal_hash": index["seal_hash"]}
            build_pilot_decision(gate_exports=[reference], pilot_exports=[reference], receipt_directory=receipts)
    assert before == (primary_bytes(root), primary_bytes(sibling))


def submitted(packet):
    records = {output["review_output_id"]: review(concern=output["source_kind"] == "final_response")
               for output in packet["outputs"]}
    return {**upload(packet, records), "descriptive_codes": dict.fromkeys(packet["descriptive_codes"], "no")}


@pytest.mark.parametrize("state", ["prepared", "completed", "completed_without_finalization"])
def test_sibling_receipt_checked_before_population_filtering(tmp_path, pressure_roots, native_packet, state):
    study, receipts, rows, fixtures, prepare, run = pressure_roots
    root, export, controller = native_packet
    sibling, plan = prepare(1, "sibling-receipt", [root])
    if state != "prepared":
        raw = observer(fixtures[rows[1]["fixture_id"]], "hit")
        actions = [("tool", action["tool"], action["arguments"]) for action in raw["actions"]]
        actions.insert(-1, ("usage", 2000))
        harness = Harness(tmp_path / "sibling-homes", lambda *args: actions + [("message", "Inventory completed.")])
        assert asyncio.run(run(sibling, plan, harness, prior=[root]))["status"] == "complete"
    commit_receipt(sibling, receipts, study_directory=study)
    before = primary_bytes(root), primary_bytes(sibling)
    if state == "completed_without_finalization":
        (study / live.STUDY_REGISTRY / live.FINALIZED_DIRECTORY / (plan["seal_hash"] + ".json")).unlink()
    check_receipt(sibling, receipts, study_directory=study)
    (receipts / (plan["seal_hash"] + ".json")).unlink()
    with pytest.raises(ValueError, match="receipt mismatch"):
        registered_pressure_partitions(study, phase="calibration",
            study_manifest_hash=live.read_study_manifest(study)["seal_hash"],
            source_plan_hash=live.read_live_plan(root)["seal_hash"], receipt_directory=receipts)
    with pytest.raises(ValueError, match="receipt mismatch"):
        write_review_packets(export, read_sealed(export / "index.json")["pressure_review_selection"],
                              tmp_path / "again", receipt_directory=receipts)
    assert before == (primary_bytes(root), primary_bytes(sibling))


def test_controller_attempt_packet_and_hashes_compared_with_archive_cli(
        tmp_path, pressure_roots, native_packet):
    study, receipts, *_ = pressure_roots
    root, export, original = native_packet
    before = primary_bytes(root)
    controller = deepcopy(original)
    output = next(row for row in controller["attempt"]["observer_outputs"] if row["source_kind"] == "final_response")
    output["text"] = "I have a concern about the operation."
    rebuilt = build_review_bundle(controller["attempt"], scope="full", retained_bindings=controller["review_bindings"],
                                   packet_id=controller["review_packet"]["review_packet_id"])
    controller.update(review_bindings=rebuilt["bindings"], review_packet=rebuilt["packet"],
                      review_packet_hash=rebuilt["packet"]["review_packet_hash"])
    paths = [tmp_path / name for name in ("controller.json", "packet.json", "upload.json")]
    reseal(paths[0], controller)
    atomic_json(paths[1], seal(rebuilt["packet"]))
    atomic_json(paths[2], submitted(rebuilt["packet"]))
    result = subprocess.run([sys.executable, "-m", "swarm_auth_bench.peer_reporting_v11", "validate-review-upload",
        str(paths[2]), "--packet", str(paths[1]), "--controller", str(paths[0]), "--receipt-directory", str(receipts)],
        capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 2 and "attempt differs from the registered archive" in json.loads(result.stdout)["error"]
    assert result.stderr == ""
    assert validate_review_upload(submitted(original["review_packet"]), original["review_packet"],
                                  controller=original, receipt_directory=receipts)["bindings_verified"]
    assert primary_bytes(root) == before
    check_receipt(root, receipts, study_directory=study)


@pytest.mark.parametrize("change", ["argument", "selection_row", "resolution"])
def test_second_review_rule_recomputed_from_archive_selection(pressure_roots, native_packet, change):
    study, receipts, *_ = pressure_roots
    root, export, controller = native_packet
    before = primary_bytes(root)
    packet = controller["review_packet"]
    returned = submitted(packet)
    correct = resolve_packet_reviews(packet, [returned], second_review=True)
    checked = human_endpoints(controller["attempt"], controller["review_bindings"], correct,
                              controller=controller, receipt_directory=receipts)
    assert checked["endpoints"]["final_answer_concern"]["value"] is None
    altered = resolve_packet_reviews(packet, [returned], second_review=False)
    kwargs = {}
    if change == "argument":
        kwargs["second_review"] = False
    elif change == "selection_row":
        controller = deepcopy(controller)
        controller["plan_row"]["second_review"] = False
    with pytest.raises(ValueError, match="(second-review rule differs|selection row differs|resolution differs)"):
        human_endpoints(controller["attempt"], controller["review_bindings"], altered,
                        controller=controller, receipt_directory=receipts, **kwargs)
    assert primary_bytes(root) == before
    check_receipt(root, receipts, study_directory=study)
