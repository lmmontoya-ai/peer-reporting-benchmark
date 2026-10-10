"""Round-five consistency findings against retained primary evidence."""

import asyncio
import json
import shutil
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live, live_review
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.pilot_decision import (
    build_pilot_decision,
    validate_core_decision,
)
from swarm_auth_bench.peer_reporting_v11.review import (
    human_endpoints,
    resolve_packet_reviews,
    validate_review_upload,
    verify_pressure_export_evidence,
    write_review_packets,
)
from swarm_auth_bench.peer_reporting_v11.review_plan import pressure_review_selection
from swarm_auth_bench.peer_reporting_v11.score import score_trial

from .live_fakes import (
    Harness,
    PauseClock,
    caps_record,
    fake_bundle,
    overload_steps,
    qualified_root,
    report_steps,
    run_phase,
    study_rows,
    write_study,
)
from .receipt_helpers import commit_receipt
from .review_helpers import bundle_for, fixture, output_ids, post, replay, report, review, structured, upload
from .test_cli_core import native_pilot
from .test_ledger_repair import APPROVAL, command, corrupt_lane
from .test_ledger_repair import lane as lane
from .test_live_overload import SOL_L1, run_paused, sealed_root, started_at
from .test_live_r2 import SERIAL, scripted


def reseal(path, record):
    atomic_json(path, seal({key: value for key, value in record.items() if key != "seal_hash"}))


@pytest.fixture(scope="module")
def flagged_native_pilot(tmp_path_factory, wp6_study, wp6_inputs):
    return native_pilot(tmp_path_factory.mktemp("flagged-native-pilot"), wp6_study, wp6_inputs, flag_count=4)


def test_native_156_trial_index_mismatch_refuses_through_600_call_core_preparation(
    tmp_path, flagged_native_pilot, wp6_inputs
):
    record, study, root, compatibility, receipts, original_export = flagged_native_pilot
    assert record["Psel"] == "P2" and record["rungs"]["P3"]["test_aware_count"] == 4
    targets = sorted(identifier for identifier in record["flag_resolutions"])[:2]
    assert len(targets) == 2
    primary = {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and (path.name == "journal.jsonl" or "attempts" in path.parts)
    }
    retained = {}
    try:
        changed = 0
        for path in root.glob("lanes/*/phase-index.json"):
            index = read_sealed(path)
            for identifier in targets:
                if identifier in index["entries"]:
                    retained[path] = path.read_bytes()
                    index["entries"][identifier]["status"] = "incomplete_interrupted"
                    changed += 1
            if path in retained:
                reseal(path, index)
        assert changed == 2
        with pytest.raises(ValueError, match="index archive status differs"):
            live_review.inspect_live_root(root, scorer=score_trial)
        with pytest.raises(ValueError, match="index archive status differs"):
            live_review.export_live_review(
                root,
                tmp_path / "export",
                study_directory=study,
                receipt_directory=receipts,
                scorer=score_trial,
                pressure_review_selector=pressure_review_selection,
            )
        index = read_sealed(original_export / "index.json")
        with pytest.raises(ValueError, match="index archive status differs"):
            write_review_packets(
                original_export,
                index["pressure_review_selection"],
                tmp_path / "packets",
                receipt_directory=receipts,
            )
        with pytest.raises(ValueError, match="index archive status differs"):
            build_pilot_decision(
                gate_exports=record["gate_exports"],
                pilot_exports=record["pilot_exports"],
                ceiling_choice="a",
                receipt_directory=receipts,
            )
        altered = deepcopy(record)
        altered.update(Psel="P3", ceiling=False, ceiling_choice=None)
        altered["rungs"]["P3"].update(test_aware_count=2, correct_answer_count=10, passes=True)
        altered = seal({key: value for key, value in altered.items() if key != "seal_hash"})
        manifest = live.read_study_manifest(study)
        rows = [
            row
            for row in manifest["assignments"]
            if row["arm"] in {"pressure_core_xhigh", "pressure_core_low"}
        ]
        fixtures = {
            identifier: {
                key: value
                for key, value in read_sealed(study / manifest["fixtures"][identifier]["path"]).items()
                if key != "seal_hash"
            }
            for identifier in {row["fixture_id"] for row in rows}
        }
        built = live.build_assignment_plan(
            "calibration",
            rows,
            fixtures,
            wp6_inputs["caps_record"],
            revision="refused-native-core",
            source=live.read_live_plan(root)["source"],
            gate_evidence={},
            study_manifest=manifest,
            pilot_decision=altered,
            pilot_decision_verifier=lambda value, **kwargs: value,
        )
        assert built[0]["maximum_live_calls"] == 600
        later = study / "roots" / "refused-native-core"
        with pytest.raises(ValueError, match="index archive status differs"):
            live.prepare_live_root(
                later,
                built,
                study_directory=study,
                receipt_directory=receipts,
                pilot_decision_verifier=validate_core_decision,
            )
        assert not later.exists()
        assert primary == {path: path.read_bytes() for path in primary}
    finally:
        for path, raw in retained.items():
            path.write_bytes(raw)


@pytest.mark.parametrize("content", ["text", "payload", "context", "instructions", "records"])
def test_packet_content_recomputed_after_consistent_hash_changes(tmp_path, content):
    item = fixture()
    attempt = replay(tmp_path, item, [report(item), post(item, "Room update.")], final="Inventory completed.")
    bundle = bundle_for(attempt, scope="reports_and_posts" if content == "context" else "full")
    packet = deepcopy(bundle["packet"])
    if content == "text":
        packet["outputs"][-1]["text"] = "A concern about unauthorized work."
    elif content == "payload":
        packet["outputs"][0]["payload"]["summary"] = "Different report evidence."
    elif content == "context":
        packet["context_outputs"][-1]["text"] = "Different context evidence."
    elif content == "instructions":
        packet["common_instructions"] += " Different instructions."
    else:
        packet["delivered_packet"] += " "
    packet["review_packet_hash"] = content_hash(
        {key: value for key, value in packet.items() if key != "review_packet_hash"}
    )
    bindings = deepcopy(bundle["bindings"])
    bindings["review_packet_hash"] = packet["review_packet_hash"]
    controller = {
        "attempt": attempt,
        "review_bindings": bindings,
        "review_packet_hash": packet["review_packet_hash"],
    }
    returned = upload(packet, {identifier: review() for identifier in packet["label_template"]})
    with pytest.raises(ValueError, match="packet content differs"):
        validate_review_upload(returned, packet, controller=controller)


@pytest.mark.parametrize("case", ["disagreement", "missing_second", "absent_adjudication"])
def test_endpoint_resolution_recomputed_from_reviews_and_bound_rule(tmp_path, case):
    item = fixture()
    attempt = replay(tmp_path, item, [], final="Inventory completed.")
    bundle = bundle_for(attempt)
    packet = bundle["packet"]
    identifier = output_ids(packet, "final_response")[0]
    first = upload(packet, {identifier: review(concern=False)})
    second = upload(packet, {identifier: review(concern=True, reviewer="reviewer-2")})
    rule = case == "missing_second"
    resolved = resolve_packet_reviews(packet, [first] if rule else [first, second], second_review=rule)
    legitimate = human_endpoints(
        attempt, bundle["bindings"], resolved, structured=structured(attempt), second_review=rule
    )
    assert legitimate["endpoints"]["final_answer_concern"]["value"] is None
    altered = deepcopy(resolved)
    altered[identifier].update(
        status="final", resolution="adjudicated", labels=review(concern=True)["labels"]
    )
    assert altered[identifier]["adjudication"] is None
    with pytest.raises(ValueError, match="resolution differs"):
        human_endpoints(
            attempt, bundle["bindings"], altered, structured=structured(attempt), second_review=rule
        )


@pytest.mark.parametrize(
    "field",
    [
        "continue_after_preflight_failure",
        "unknown_usage_policy",
        "outcome_based_retries",
        "hard_provider_output_cap_verified",
    ],
)
def test_lane_execution_policy_refuses_before_prepare_verify_or_run(tmp_path, field):
    rows, fixtures = study_rows("calibration", cells=(("L1", "violation", "guided", "xhigh", "normal"),))
    study = write_study(tmp_path / "study", rows, fixtures)
    manifest = live.read_study_manifest(study)
    built = live.build_assignment_plan(
        "calibration",
        rows,
        fixtures,
        caps_record(),
        revision="policy-test",
        source={"kind": "study_manifest", "study_manifest_hash": manifest["seal_hash"]},
        gate_evidence={},
        bundle=fake_bundle(),
        study_manifest=manifest,
    )
    original = deepcopy(built)
    lane = built[0]["lanes"][0]
    plan = built[1][lane["lane_id"]]
    plan[field] = True if field != "unknown_usage_policy" else "different_policy"
    lane["plan_hash"] = seal(plan)["seal_hash"]
    root = study / "roots" / "policy"
    with pytest.raises(ValueError, match="execution policy differs"):
        live.prepare_live_root(root, built, study_directory=study)
    live.prepare_live_root(root, original, study_directory=study)
    top = live.read_live_plan(root)
    atomic_json(root / lane["path"] / "phase-plan.json", seal(plan))
    top["lanes"][0]["plan_hash"] = lane["plan_hash"]
    reseal(root / live.LIVE_PLAN_FILE, top)
    with pytest.raises(ValueError, match="execution policy differs"):
        live.verify_live_root(root, study_directory=study, bundle=fake_bundle())
    harness = Harness(tmp_path / "homes", lambda *args: pytest.fail("inconsistent plan admitted"))
    with pytest.raises(ValueError, match="execution policy differs"):
        asyncio.run(run_phase(root, live.read_live_plan(root), harness, study_directory=study))
    assert harness.created == []


@pytest.mark.parametrize("field", ["near_miss_type", "variant", "prompt_condition"])
def test_historical_entry_requires_ordinary_labels(tmp_path, field):
    rows, fixtures = study_rows("calibration", cells=(("L2", "violation", "guided", "xhigh", "normal"),))
    study = write_study(tmp_path / "study", rows, fixtures)
    manifest = live.read_study_manifest(study)
    built = live.build_assignment_plan(
        "calibration",
        rows,
        fixtures,
        caps_record(),
        revision="labels-test",
        source={"kind": "study_manifest", "study_manifest_hash": manifest["seal_hash"]},
        gate_evidence={},
        bundle=fake_bundle(),
        study_manifest=manifest,
    )
    assert "compound_type" not in built[1][built[0]["lanes"][0]["lane_id"]]["planned_order"][0]
    live.check_plan_assignment_binding(built[0], built[1], manifest, fixtures)
    lane = built[0]["lanes"][0]
    built[1][lane["lane_id"]]["planned_order"][0].pop(field)
    lane["plan_hash"] = seal(built[1][lane["lane_id"]])["seal_hash"]
    with pytest.raises(ValueError, match="is missing"):
        live.prepare_live_root(study / "roots" / "missing-label", built, study_directory=study)


@pytest.mark.parametrize("change", ["commit", "evidence"])
def test_repair_consumer_rechecks_named_local_commit(lane, capsys, change):
    corrupt_lane(lane)
    before = lane["path"].read_bytes()
    if change == "commit":
        binding = read_sealed(lane["binding"])
        binding["commit"] = "0" * 40
        reseal(lane["binding"], binding)
    else:
        path = lane["evidence"] / "journal.jsonl"
        path.write_bytes(path.read_bytes() + b" ")
    code, result = command(lane, capsys)
    assert code == 2 and "Git commit" in result["error"]
    assert lane["path"].read_bytes() == before
    assert not (lane["directory"] / "ledger-repairs").exists()


@pytest.mark.parametrize("change", ["timestamps", "count"])
async def test_successor_pause_admission_checks_owning_journal(tmp_path, change):
    compatibility = (await asyncio.to_thread(qualified_root, tmp_path / "compat-base"))[0]
    rows, fixtures = study_rows("smoke")
    later_rows, later_fixtures = study_rows("calibration", template_id="release-request")
    study = write_study(tmp_path / "study", rows + later_rows, {**fixtures, **later_fixtures}, caps=SERIAL)
    root, plan = sealed_root(study, compatibility)
    clock, starts = PauseClock(), []
    # End after the first overload so its pause still binds a successor.
    harness = Harness(
        tmp_path / "homes",
        scripted(fixtures, {SOL_L1: lambda f: overload_steps()}),
        observe=started_at(clock, starts),
    )
    await run_paused(root, plan, harness, clock, compatibility, study)
    pause_path = next((study / live.PAUSE_LEDGER).glob("*.json"))
    record = read_sealed(pause_path)
    if change == "timestamps":
        record["pause"]["paused_at"] -= 7200
        record["pause"]["resume_at"] -= 7200
    else:
        record["pause"]["window_count"] += 1
    reseal(pause_path, record)
    with pytest.raises(ValueError, match="as journaled"):
        live.study_provider_pauses(study)
    later, later_plan = sealed_root(study, compatibility, revision="later", phase="calibration")
    later_harness = Harness(
        tmp_path / "later-homes", lambda *args: pytest.fail("inconsistent pause admitted")
    )
    with pytest.raises(ValueError, match="as journaled"):
        await run_paused(later, later_plan, later_harness, clock, compatibility, study)
    assert later_harness.created == []


@pytest.mark.parametrize("consumer", ["export", "packets", "pilot"])
def test_sibling_registration_phase_checked_before_filtering(
    tmp_path, flagged_native_pilot, wp6_inputs, consumer
):
    record, study, root, compatibility, receipts, output = flagged_native_pilot
    manifest = live.read_study_manifest(study)
    row = next(row for row in manifest["assignments"] if row["arm"] == "pressure_pilot_low")
    stored = read_sealed(study / manifest["fixtures"][row["fixture_id"]]["path"])
    f = {key: value for key, value in stored.items() if key != "seal_hash"}
    built = live.build_assignment_plan(
        "calibration",
        [row],
        {row["fixture_id"]: f},
        wp6_inputs["caps_record"],
        revision="sibling-" + consumer,
        source=live.read_live_plan(root)["source"],
        gate_evidence={},
        study_manifest=manifest,
    )
    sibling = study / "roots" / ("sibling-" + consumer)
    # This test isolates population checks, so the sibling has no prior ledger.
    # Write the same primary plans that preparation emits, then its registration.
    sibling.mkdir()
    top = seal({**built[0], "root_instance_nonce": consumer})
    atomic_json(sibling / live.LIVE_PLAN_FILE, top)
    for lane_entry in top["lanes"]:
        path = sibling / lane_entry["path"] / "phase-plan.json"
        path.parent.mkdir(parents=True)
        atomic_json(path, seal(built[1][lane_entry["lane_id"]]))
    registration_path = study / live.STUDY_REGISTRY / (top["seal_hash"] + ".json")
    registration = {
        "kind": live.REGISTRY_KIND,
        "plan_hash": top["seal_hash"],
        "phase": "calibration",
        "study_manifest_hash": manifest["seal_hash"],
        "root_path": "roots/" + sibling.name,
    }
    atomic_json(registration_path, seal(registration))
    try:
        registration["phase"] = "smoke"
        atomic_json(registration_path, seal(registration))
        with pytest.raises(ValueError, match="root plan differs from its study registration"):
            if consumer == "export":
                live_review.export_live_review(
                    root,
                    tmp_path / "export",
                    study_directory=study,
                    receipt_directory=receipts,
                    scorer=score_trial,
                    pressure_review_selector=pressure_review_selection,
                )
            elif consumer == "packets":
                write_review_packets(
                    output,
                    read_sealed(output / "index.json")["pressure_review_selection"],
                    tmp_path / "packets",
                    receipt_directory=receipts,
                )
            else:
                build_pilot_decision(
                    gate_exports=record["gate_exports"],
                    pilot_exports=record["pilot_exports"],
                    ceiling_choice="a",
                    receipt_directory=receipts,
                )
    finally:
        registration_path.unlink()


async def test_one_of_twelve_smoke_population_refuses_collection_preparation_and_run(tmp_path):
    compatibility = (await asyncio.to_thread(qualified_root, tmp_path / "compat-base"))[0]
    smoke_rows, smoke_fixtures = study_rows("smoke")
    collection_rows, collection_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    study = write_study(
        tmp_path / "study", smoke_rows + collection_rows, {**smoke_fixtures, **collection_fixtures}
    )
    manifest = live.read_study_manifest(study)
    source = {"kind": "study_manifest", "study_manifest_hash": manifest["seal_hash"]}
    built = live.build_assignment_plan(
        "smoke",
        smoke_rows[:1],
        smoke_fixtures,
        caps_record(),
        revision="one-smoke",
        source=source,
        gate_evidence=live.check_phase_gates(
            "smoke",
            [smoke_rows[0]["model"] + "-" + smoke_rows[0]["effort"]],
            bundle=fake_bundle(),
            source=source,
            compatibility_directories=[compatibility],
            study_directory=study,
        )["evidence"],
        bundle=fake_bundle(),
        study_manifest=manifest,
    )
    smoke = study / "roots" / "one-smoke"
    live.prepare_live_root(smoke, built, study_directory=study)
    harness = Harness(
        tmp_path / "smoke-homes", lambda *args: report_steps(smoke_fixtures[smoke_rows[0]["fixture_id"]])
    )
    assert (
        await run_phase(
            smoke,
            live.read_live_plan(smoke),
            harness,
            study_directory=study,
            compatibility_directories=[compatibility],
        )
    )["status"] == "complete"
    copied_ids = [smoke_rows[0]["assignment_id"]]
    evidence, failures = live.smoke_evidence(
        smoke, bundle=fake_bundle(), source=source, smoke_assignment_ids=copied_ids, study_directory=study
    )
    assert evidence is None and "study's smoke rows" in failures[0]
    from .live_fakes import review_plan_for

    review_plan = review_plan_for(study)
    full_ids = sorted(row["assignment_id"] for row in smoke_rows)
    collection = live.build_assignment_plan(
        "collection",
        collection_rows,
        collection_fixtures,
        caps_record(),
        revision="collection-population",
        source=source,
        gate_evidence={},
        bundle=fake_bundle(),
        study_manifest=manifest,
        smoke_assignment_ids=full_ids,
        review_plan_hash=review_plan["seal_hash"],
    )
    original = deepcopy(collection)
    collection[0]["smoke_assignment_ids"] = copied_ids
    root = study / "roots" / "collection"
    with pytest.raises(ValueError, match="smoke population differs"):
        live.prepare_live_root(root, collection, study_directory=study, review_plan=review_plan)
    live.prepare_live_root(root, original, study_directory=study, review_plan=review_plan)
    top = live.read_live_plan(root)
    top["smoke_assignment_ids"] = copied_ids
    reseal(root / live.LIVE_PLAN_FILE, top)
    later = Harness(
        tmp_path / "later-homes", lambda *args: pytest.fail("inconsistent smoke population admitted")
    )
    with pytest.raises(ValueError, match="smoke population differs"):
        await run_phase(
            root,
            live.read_live_plan(root),
            later,
            study_directory=study,
            compatibility_directories=[compatibility],
            smoke_directory=smoke,
        )
    assert later.created == []


@pytest.mark.parametrize("change", ["missing", "inconsistent", "incomplete"])
def test_shared_export_rederivation_rechecks_current_root_repairs_after_export(
    tmp_path, flagged_native_pilot, change
):
    from swarm_auth_bench.peer_reporting_v11.ledger_repair import build_ledger_repair_binding, repair_ledger

    from .test_ledger_repair import committed_evidence, flipped

    record, source_study, source_root, compatibility, old_receipts, old_export = flagged_native_pilot
    study = tmp_path / "study"
    shutil.copytree(source_study, study)
    root = study / "roots" / source_root.name
    top = live.read_live_plan(root)
    selected = top["lanes"][0]
    lane_dir = root / selected["path"]
    ledger = lane_dir / "budget-ledger.json"
    raw = flipped(ledger.read_bytes(), ledger.read_bytes().index(b"notifications") + 4)
    evidence = committed_evidence(
        tmp_path / "repair-evidence",
        raw,
        (lane_dir / "budget-ledger.json.identity.json").read_bytes(),
        (lane_dir / "journal.jsonl").read_bytes(),
    )
    binding = build_ledger_repair_binding(
        evidence,
        study=study.name,
        root=root.name,
        lane_id=selected["lane_id"],
        plan_hash=top["seal_hash"],
        commit="HEAD",
        approval_text=APPROVAL,
    )
    binding_path = tmp_path / "binding.json"
    atomic_json(binding_path, binding)
    ledger.write_bytes(raw)
    with pytest.raises(ValueError, match="receipt mismatch"):
        repair_ledger(
            root,
            study_directory=study,
            lane_id=selected["lane_id"],
            reason="Restore journal-consistent bytes.",
            approval_text=APPROVAL,
            binding_path=binding_path,
            evidence_directory=evidence,
            receipt_directory=tmp_path / "missing-receipts",
        )
    assert ledger.read_bytes() == raw
    repaired = repair_ledger(
        root,
        study_directory=study,
        lane_id=selected["lane_id"],
        reason="Restore journal-consistent bytes.",
        approval_text=APPROVAL,
        binding_path=binding_path,
        evidence_directory=evidence,
        receipt_directory=old_receipts,
    )
    receipts = commit_receipt(root, tmp_path / "receipts", study_directory=study)
    export = tmp_path / "export"
    live_review.export_live_review(
        root,
        export,
        study_directory=study,
        receipt_directory=receipts,
        repair_evidence_directory=evidence,
        scorer=score_trial,
        pressure_review_selector=pressure_review_selection,
    )
    index = read_sealed(export / "index.json")
    assert (
        len(
            verify_pressure_export_evidence(
                export, index, receipt_directory=receipts, repair_evidence_directory=evidence
            )
        )
        == 156
    )
    path = root / repaired["repair_record"]
    if change == "missing":
        path.unlink()
    else:
        value = read_sealed(path)
        value["status" if change == "incomplete" else "reason"] = (
            "prepared" if change == "incomplete" else "Different reason."
        )
        reseal(path, value)
    with pytest.raises(ValueError, match="ledger repair"):
        verify_pressure_export_evidence(
            export, index, receipt_directory=receipts, repair_evidence_directory=evidence
        )
    with pytest.raises(ValueError, match="ledger repair"):
        write_review_packets(
            export,
            index["pressure_review_selection"],
            tmp_path / "packets",
            receipt_directory=receipts,
            repair_evidence_directory=evidence,
        )


def test_root_verification_rechecks_binding_against_committed_external_evidence(lane, capsys):
    corrupt_lane(lane)
    assert command(lane, capsys)[0] == 0
    assert live.verify_live_root(
        lane["root"],
        study_directory=lane["study"],
        bundle=fake_bundle(),
        repair_evidence_directory=lane["evidence"],
    )["ledger_repairs"]
    path = lane["evidence"] / "journal.jsonl"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="named Git commit"):
        live.verify_live_root(
            lane["root"],
            study_directory=lane["study"],
            bundle=fake_bundle(),
            repair_evidence_directory=lane["evidence"],
        )


@pytest.mark.parametrize(
    "consumer", ["export", "packets", "pilot", "build_prior", "prepare_prior", "verify_prior", "abandon", "cleanup"]
)
def test_native_post_run_consumers_require_committed_receipts(
    tmp_path, flagged_native_pilot, wp6_inputs, monkeypatch, consumer
):
    record, study, root, compatibility, receipts, output = flagged_native_pilot
    empty = tmp_path / "missing-receipts"
    empty.mkdir()
    with pytest.raises(ValueError, match="receipt mismatch"):
        if consumer == "export":
            live_review.export_live_review(
                root, tmp_path / "export", study_directory=study, receipt_directory=empty
            )
        elif consumer == "packets":
            write_review_packets(
                output,
                read_sealed(output / "index.json")["pressure_review_selection"],
                tmp_path / "packets",
                receipt_directory=empty,
            )
        elif consumer == "pilot":
            build_pilot_decision(
                gate_exports=record["gate_exports"],
                pilot_exports=record["pilot_exports"],
                ceiling_choice="a",
                receipt_directory=empty,
            )
        elif consumer == "build_prior":
            live.prior_root_ledger(
                [root],
                phase="calibration",
                source=live.read_live_plan(root)["source"],
                study_directory=study,
                receipt_directory=empty,
            )
        elif consumer == "abandon":
            monkeypatch.setattr(live, "_journal_starts", lambda *args: pytest.fail("journal read before receipt"))
            live.abandon_root(study, live.read_live_plan(root)["seal_hash"], root=root,
                              reason="Check the registered root.", receipt_directory=empty)
        elif consumer == "cleanup":
            monkeypatch.setattr(live, "verify_live_root", lambda *a, **k: pytest.fail("archive verification before receipt"))
            top = live.read_live_plan(root)
            entry = read_sealed(root / top["lanes"][0]["path"] / "phase-plan.json")["planned_order"][0]
            asyncio.run(live.reconcile_cleanup(root, [entry["attempt_id"]], reason="Check retained cleanup.",
                                               study_directory=study, receipt_directory=empty))
        else:
            manifest = live.read_study_manifest(study)
            row = next(
                row
                for row in manifest["assignments"]
                if row["split"] == "calibration" and row["level"] == "L0"
            )
            f = read_sealed(study / manifest["fixtures"][row["fixture_id"]]["path"])
            f = {key: value for key, value in f.items() if key != "seal_hash"}
            ledger = live.prior_root_ledger(
                [root],
                phase="calibration",
                source=live.read_live_plan(root)["source"],
                study_directory=study,
                receipt_directory=receipts,
            )
            built = live.build_assignment_plan(
                "calibration",
                [row],
                {row["fixture_id"]: f},
                wp6_inputs["caps_record"],
                revision="receipt-prior-" + consumer,
                source=live.read_live_plan(root)["source"],
                gate_evidence={},
                study_manifest=manifest,
                consumed_attempts=ledger,
            )
            later = study / "roots" / ("receipt-prior-" + consumer)
            if consumer == "prepare_prior":
                live.prepare_live_root(
                    later, built, study_directory=study, prior_roots=[root], receipt_directory=empty
                )
            else:
                live.prepare_live_root(
                    later, built, study_directory=study, prior_roots=[root], receipt_directory=receipts
                )
                try:
                    live.verify_live_root(
                        later, study_directory=study, prior_roots=[root], receipt_directory=empty
                    )
                finally:
                    # Remove only this synthetic registration so the shared fixture remains independent.
                    top = live.read_live_plan(later)
                    (study / live.STUDY_REGISTRY / (top["seal_hash"] + ".json")).unlink()
                    (
                        study / live.STUDY_REGISTRY / live.FINALIZED_DIRECTORY / (top["seal_hash"] + ".json")
                    ).unlink()
                    (root / live.SUPERSEDED_DIRECTORY / (top["seal_hash"] + ".json")).unlink()


def test_unstarted_pressure_abandonment_cli_accepts_committed_receipt(tmp_path, wp6_study, wp6_inputs, capsys):
    from swarm_auth_bench.peer_reporting_v11.cli import main
    from swarm_auth_bench.peer_reporting_v11.receipts import receipt_bytes

    manifest = live.read_study_manifest(wp6_study[0])
    row = next(row for row in manifest["assignments"] if row["level"] == "P")
    fixture = read_sealed(wp6_study[0] / manifest["fixtures"][row["fixture_id"]]["path"])
    fixture = {key: value for key, value in fixture.items() if key != "seal_hash"}
    study = tmp_path / "study"
    study.mkdir()
    atomic_json(study / live.STUDY_MANIFEST, manifest)
    root = study / "roots" / "unstarted-pressure"
    built = live.build_assignment_plan(
        "calibration", [row], {row["fixture_id"]: fixture}, wp6_inputs["caps_record"],
        revision="unstarted-pressure", source={"study_manifest_hash": manifest["seal_hash"]},
        gate_evidence={}, study_manifest=manifest)
    live.prepare_live_root(root, built, study_directory=study)
    top = live.read_live_plan(root)
    directory = commit_receipt(root, tmp_path / "receipts", study_directory=study)
    before = receipt_bytes(root, study_directory=study)
    assert main(["abandon-root", str(study), "--plan-hash", top["seal_hash"], "--root", str(root),
                 "--reason", "Retire the unstarted plan.", "--receipt-directory", str(directory)]) == 0
    assert json.loads(capsys.readouterr().out)["root_journals_checked"] is True
    assert live.registered_roots(study)[0]["state"] == "abandoned"
    assert receipt_bytes(root, study_directory=study) == before


@pytest.mark.parametrize("consumer", ["build", "prepare", "verify"])
def test_v2_compatibility_prerequisite_receipt_required_at_later_root_gates(
    tmp_path, flagged_native_pilot, wp6_inputs, consumer
):
    record, study, root, compatibility, receipts, output = flagged_native_pilot
    manifest = live.read_study_manifest(study)
    empty = tmp_path / "empty-receipts"
    empty.mkdir()
    if consumer == "build":
        with pytest.raises(ValueError, match="receipt mismatch"):
            live.check_phase_gates(
                "calibration",
                [lane["lane_id"] for lane in live.read_live_plan(root)["lanes"]],
                bundle=load_bundle().tool_set(live.read_live_plan(root)["tool_schema_version"]),
                source=live.read_live_plan(root)["source"],
                compatibility_directories=[compatibility],
                study_directory=study,
                receipt_directory=empty,
            )
    else:
        row = next(
            row
            for row in manifest["assignments"]
            if row["level"] == "P" and row["arm"] == "pressure_gate_xhigh"
        )
        f = read_sealed(study / manifest["fixtures"][row["fixture_id"]]["path"])
        f = {key: value for key, value in f.items() if key != "seal_hash"}
        built = live.build_assignment_plan(
            "calibration",
            [row],
            {row["fixture_id"]: f},
            wp6_inputs["caps_record"],
            revision="compat-prerequisite",
            source=live.read_live_plan(root)["source"],
            gate_evidence=deepcopy(live.read_live_plan(root)["gate_evidence"]),
            study_manifest=manifest,
        )
        later_study = tmp_path / "study"
        later_study.mkdir()
        atomic_json(later_study / live.STUDY_MANIFEST, manifest)
        later = later_study / "roots" / "later"
        if consumer == "prepare":
            with pytest.raises(ValueError, match="receipt mismatch"):
                live.prepare_live_root(
                    later,
                    built,
                    study_directory=later_study,
                    compatibility_directories=[compatibility],
                    receipt_directory=empty,
                )
        else:
            live.prepare_live_root(
                later,
                built,
                study_directory=later_study,
                compatibility_directories=[compatibility],
                receipt_directory=receipts,
            )
            with pytest.raises(ValueError, match="receipt mismatch"):
                live.verify_live_root(
                    later,
                    study_directory=later_study,
                    compatibility_directories=[compatibility],
                    receipt_directory=empty,
                )
