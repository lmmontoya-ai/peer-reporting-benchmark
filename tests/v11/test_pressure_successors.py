"""Receipt-bound successor exports use each assignment's journaled start once."""

import shutil
from collections import deque
from copy import deepcopy

import pytest
import pytest_asyncio

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.config import PRESSURE_ARMS
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
from swarm_auth_bench.peer_reporting_v11.pilot_decision import (
    build_pilot_decision,
    export_reference,
    validate_core_decision,
)
from swarm_auth_bench.peer_reporting_v11.receipts import check_receipt, receipt_bytes
from swarm_auth_bench.peer_reporting_v11.review import (
    human_endpoints,
    validate_review_upload,
    verify_pressure_export_evidence,
    write_review_packets,
)
from swarm_auth_bench.peer_reporting_v11.review_plan import REVIEW_SEED, pressure_review_selection
from swarm_auth_bench.peer_reporting_v11.review_sampling import pressure_negative_sample
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P
from swarm_auth_bench.peer_reporting_v11.score import score_trial, summarize

from .live_fakes import Harness, guest, qualifier_steps, run_phase
from .r6_observers import case_for, observer
from .receipt_helpers import commit_receipt, git


def entries(root, plan):
    return [entry for lane in plan["lanes"]
            for entry in read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"]]


@pytest_asyncio.fixture(scope="module")
async def successor_inputs(tmp_path_factory, wp6_study, wp6_inputs):
    base = tmp_path_factory.mktemp("successors")
    study = base / "study"
    shutil.copytree(wp6_study[0], study)
    manifest = read_sealed(study / live.STUDY_MANIFEST)
    bundle, caps = load_bundle(), wp6_inputs["caps_record"]
    compatibility = base / "compatibility"
    built = live.build_compatibility_plan(caps, revision="successor-compat", bundle=bundle,
                                          tool_schema_version=TOOL_SCHEMA_VERSION_P)
    live.prepare_live_root(compatibility, built, bundle=bundle)
    top = live.read_live_plan(compatibility)
    harness = Harness(base / "compat-homes", lambda *args: qualifier_steps(next(iter(built[2].values()))))
    assert (await run_phase(compatibility, top, harness, bundle=bundle))["status"] == "complete"
    receipts = commit_receipt(compatibility, base / "receipts")
    roots, exports, groups = [], {}, {}
    decision = None

    def selected_case(entry):
        if entry["arm"] in PRESSURE_ARMS[:2]:
            return "hit"
        if entry["arm"] in PRESSURE_ARMS[2:4] and entry["variant"] == "violation":
            if entry["visibility"] == "private" and entry["pressure"] == "neutral":
                return "hit"
            if entry["visibility"] == "visible" and entry["pressure"] in ("P1", "P2", "P3"):
                return "silent" if (entry["model"] == manifest["protocol"]["models"][0]
                    and entry["template_id"] == "firewall-change" and entry["reasoning_effort"] == "low") else "hit"
        return case_for(entry)

    for group, arms, count in (("gate-pilot", PRESSURE_ARMS[:4], 156), ("core", PRESSURE_ARMS[4:], 600)):
        group_roots, first_starts = [], set()
        group_assignments = [row for row in manifest["assignments"] if row["arm"] in arms]
        for version in (1, 2):
            root = study / "roots" / f"{group}-v{version}"
            built = live.build_phase_plan("calibration", caps, revision=f"{group}-v{version}",
                study_directory=study, compatibility_directories=[compatibility], prior_roots=roots,
                bundle=bundle, arms=sorted({row["arm"] for row in group_assignments
                    if row["assignment_id"] not in first_starts}),
                pilot_decision=decision, receipt_directory=receipts,
                pilot_decision_verifier=validate_core_decision if decision else None)
            live.prepare_live_root(root, built, study_directory=study, prior_roots=roots, bundle=bundle,
                compatibility_directories=[compatibility], receipt_directory=receipts,
                pilot_decision_verifier=validate_core_decision if decision else None)
            plan = live.read_live_plan(root)
            planned = entries(root, plan)
            ids = {entry["entry_id"] for entry in planned}
            if version == 1:
                assert len(planned) == count
            else:
                assert len(planned) == count - len(first_starts)
                assert not ids & first_starts
                assert group_roots[0][1]["seal_hash"] in {
                    prior["plan_hash"] for prior in plan["consumed_attempts"]["prior_roots"]}
                # With no starts in the successor, overlapping unrun rows come from its plan.
                commit_receipt(root, receipts, study_directory=study)
                unrun_export = base / f"{group}-unrun-export"
                export_live_review(root, unrun_export, study_directory=study, prior_roots=roots,
                    scorer=score_trial, pressure_review_selector=pressure_review_selection,
                    receipt_directory=receipts)
                index = read_sealed(unrun_export / "index.json")
                assert len(index["rows"]) == count
                assert {row["assignment_id"] for row in index["rows"]} == ids | first_starts
                assert all(row["status"] == "unrun" and row["source_root_plan_hash"] == plan["seal_hash"]
                           for row in index["rows"] if row["assignment_id"] in ids)
                verify_pressure_export_evidence(unrun_export, index, receipt_directory=receipts)
            queues = {(lane["model"], lane["reasoning_effort"]): deque(read_sealed(
                root / lane["path"] / "phase-plan.json")["planned_order"]) for lane in plan["lanes"]}
            current, failed = {}, []

            def script_for(model, effort):
                entry = queues[model, effort].popleft()
                current[model, effort] = entry
                script = observer(built[2][entry["fixture_id"]], selected_case(entry))
                steps = [("tool", action["tool"], action["arguments"]) for action in script["actions"]]
                steps.insert(-1, ("usage", 2000))
                return steps + [("message", script["final_response"])]

            harness = Harness(base / f"{group}-v{version}-homes", script_for)
            factory = harness.factory

            def runtime_factory(model, effort):
                runtime = factory(model, effort)
                entry = current[model, effort]
                if version == 1 and entry["variant"] == "twin" and not failed:
                    failed.append(entry["attempt_id"])
                    close = runtime.close

                    async def timeout_close():
                        await close()
                        raise TimeoutError("simulated runtime cleanup timeout")

                    runtime.close = timeout_close
                return runtime

            harness.factory = runtime_factory
            status = await run_phase(root, plan, harness, bundle=bundle, study_directory=study,
                prior_roots=roots, compatibility_directories=[compatibility], receipt_directory=receipts)
            if version == 1:
                assert status["status"] == "held" and failed, status
                first_starts = {record["data"]["entry_id"] for records in live.lane_journals(root, plan).values()
                                for record in records if record["kind"] == "attempt_started"}
                assert 0 < len(first_starts) < count
                commit_receipt(root, receipts, study_directory=study)
                reconciled = await live.reconcile_cleanup(root, failed, reason="Fake runtime cleanup completed.",
                    study_directory=study, environment_check=guest, bundle=bundle, receipt_directory=receipts,
                    compatibility_directories=[compatibility])
                assert reconciled["attempt_ids"] == failed
            else:
                assert status["status"] == "complete"
                assert len(harness.created) == count - len(first_starts)
            commit_receipt(root, receipts, study_directory=study)
            export = base / f"{group}-v{version}-export"
            export_live_review(root, export, study_directory=study, prior_roots=roots, bundle=bundle,
                scorer=score_trial, summarize=summarize, pressure_review_selector=pressure_review_selection,
                receipt_directory=receipts)
            exports[group, version] = export
            roots.append(root)
            group_roots.append((root, plan))
        groups[group] = (group_roots, first_starts)
        if group == "gate-pilot":
            reference = export_reference(exports[group, 2])
            decision = build_pilot_decision(gate_exports=[reference], pilot_exports=[reference],
                                            receipt_directory=receipts)
            assert decision["decision"] == "proceed" and decision["Psel"] == "P3"
            assert decision["pooled_gate"]["hits"] == 36
            validate_core_decision(decision, study_manifest_hash=manifest["seal_hash"], receipt_directory=receipts)
    return base, study, manifest, receipts, groups, exports, decision


def test_successor_populations_packets_uploads_endpoints_and_decisions(successor_inputs):
    base, study, manifest, receipts, groups, exports, decision = successor_inputs
    for group, count in (("gate-pilot", 156), ("core", 600)):
        roots, starts = groups[group]
        first, latest = roots[0][1]["seal_hash"], roots[-1][1]["seal_hash"]
        export = exports[group, 2]
        index = read_sealed(export / "index.json")
        assert len(index["rows"]) == index["planned_count"] == count
        assert len({row["assignment_id"] for row in index["rows"]}) == count
        assert all(row["source_root_plan_hash"] == (first if row["assignment_id"] in starts else latest)
                   for row in index["rows"])
        scores = verify_pressure_export_evidence(export, index, receipt_directory=receipts)
        assert index["pressure_review_selection"] == pressure_review_selection(scores)
        sampled = pressure_negative_sample(scores, seed=REVIEW_SEED)
        assert sampled == {row["assignment_id"] for row in index["pressure_review_selection"]["rows"]
                           if "mechanical_negative_sample" in row["reasons"]}
        assert index["summary"] == summarize(scores)
        packets = base / f"{group}-packets"
        result = write_review_packets(export, index["pressure_review_selection"], packets,
                                      study_directory=study, receipt_directory=receipts)
        assert result["packets"] == len(index["pressure_review_selection"]["rows"])
        checked_sources = set()
        provenance = {row["assignment_id"]: row["source_root_plan_hash"] for row in index["rows"]}
        for controller in (packets / "controller").glob("*.json"):
            bound = read_sealed(controller)
            source = provenance[bound["assignment_id"]]
            if source in checked_sources:
                continue
            checked_sources.add(source)
            packet = bound["review_packet"]
            upload = {"review_packet_hash": packet["review_packet_hash"],
                      "labels_by_output_id": {},
                      "descriptive_codes": dict.fromkeys(packet["descriptive_codes"], "no")}
            validated = validate_review_upload(upload, packet, controller=bound, receipt_directory=receipts)
            assert validated["bindings_verified"]
            endpoints = human_endpoints(bound["attempt"], bound["review_bindings"], uploads=[upload],
                                        controller=bound, receipt_directory=receipts)
            assert endpoints
    reference = export_reference(exports["gate-pilot", 2])
    assert build_pilot_decision(gate_exports=[reference], pilot_exports=[reference], receipt_directory=receipts) == decision
    assert validate_core_decision(decision, study_manifest_hash=manifest["seal_hash"],
                                  receipt_directory=receipts)["decision"] == "proceed"
    assert groups["core"][1]
    assert groups["core"][0][-1][1]["pilot_decision_hash"] == decision["seal_hash"]


@pytest.mark.parametrize("group", ["gate-pilot", "core"])
def test_earlier_chain_export_refuses_and_names_latest(successor_inputs, group):
    base, study, _, receipts, groups, exports, _ = successor_inputs
    roots, _ = groups[group]
    root, plan = roots[0]
    prior = [live.registered_root_path(study, record) for record in live.registered_roots(study)
             if record["plan_hash"] in {item["plan_hash"] for item in plan["consumed_attempts"]["prior_roots"]}]
    output = base / f"{group}-earlier-refused"
    with pytest.raises(ValueError, match=f"export latest root .*{group}-v2"):
        export_live_review(root, output, study_directory=study, prior_roots=prior,
            scorer=score_trial, pressure_review_selector=pressure_review_selection, receipt_directory=receipts)
    assert not output.exists()
    earlier = exports[group, 1]
    with pytest.raises(ValueError, match=f"export latest root .*{group}-v2"):
        verify_pressure_export_evidence(earlier, read_sealed(earlier / "index.json"), receipt_directory=receipts)


@pytest.mark.parametrize("remove_related_records", [False, True])
def test_retained_successor_requires_registration_for_population_consumers(
        successor_inputs, tmp_path, remove_related_records):
    _, study, _, receipts, groups, exports, decision = successor_inputs
    first, first_plan = groups["core"][0][0]
    latest, latest_plan = groups["core"][0][1]
    identity = latest_plan["seal_hash"]
    registrations = live.registered_roots(study)
    roots = [live.registered_root_path(study, record) for record in registrations]
    before = {root: receipt_bytes(root, study_directory=study) for root in roots}
    removed = [study / live.STUDY_REGISTRY / f"{identity}.json",
               study / live.STUDY_REGISTRY / live.FINALIZED_DIRECTORY / f"{identity}.json"]
    if remove_related_records:
        removed += [path for root in roots for path in (root / live.SUPERSEDED_DIRECTORY).glob(f"{identity}.json")]
        removed += [path for path in (study / live.START_LEDGER).glob("*.json")
                    if read_sealed(path)["plan_hash"] == identity]
    retained = {path: path.read_bytes() for path in removed}
    prior = [live.registered_root_path(study, record) for record in registrations
             if record["plan_hash"] in {item["plan_hash"] for item in first_plan["consumed_attempts"]["prior_roots"]}]
    export = exports["core", 1]
    index = read_sealed(export / "index.json")
    try:
        for path in removed:
            path.unlink()
        with pytest.raises(ValueError, match="retained root inventory differs"):
            export_live_review(first, tmp_path / "refused-export", study_directory=study, prior_roots=prior,
                scorer=score_trial, pressure_review_selector=pressure_review_selection, receipt_directory=receipts)
        assert not (tmp_path / "refused-export").exists()
        with pytest.raises(ValueError, match="retained root inventory differs"):
            verify_pressure_export_evidence(export, index, receipt_directory=receipts)
        with pytest.raises(ValueError, match="retained root inventory differs"):
            write_review_packets(export, index["pressure_review_selection"], tmp_path / "refused-packets",
                                 study_directory=study, receipt_directory=receipts)
        assert not (tmp_path / "refused-packets").exists()
        with pytest.raises(ValueError, match="retained root inventory differs"):
            build_pilot_decision(gate_exports=decision["gate_exports"], pilot_exports=decision["pilot_exports"],
                                 receipt_directory=receipts)
        with pytest.raises(ValueError, match="retained root inventory differs"):
            live.prior_root_ledger([root for root in roots if root != latest], phase="calibration",
                source=first_plan["source"], study_directory=study,
                bundle=load_bundle().tool_set(TOOL_SCHEMA_VERSION_P), receipt_directory=receipts)
        for root, raw in before.items():
            assert receipt_bytes(root, study_directory=study) == raw
            check_receipt(root, receipts, study_directory=study)
    finally:
        for path, raw in retained.items():
            path.write_bytes(raw)


def test_abandoned_root_with_committed_journal_starts_refuses_population(successor_inputs, tmp_path):
    _, study, _, receipts, groups, exports, _ = successor_inputs
    root, plan = groups["core"][0][-1]
    assert any(record["kind"] == "attempt_started"
               for records in live.lane_journals(root, plan).values() for record in records)
    registry = study / live.STUDY_REGISTRY
    registration = read_sealed(registry / f"{plan['seal_hash']}.json")
    marker = registry / live.ABANDONED_DIRECTORY / f"{plan['seal_hash']}.json"
    marker.parent.mkdir(exist_ok=True)
    atomic_json(marker, seal({"kind": live.ABANDONED_KIND,
        **{key: registration[key] for key in ("plan_hash", "root_path", "phase", "study_manifest_hash")},
        "consumed_attempt_ids": [], "root_journals_checked": True,
        "reason": "Offline consistency check."}))
    try:
        export = exports["gate-pilot", 2]
        with pytest.raises(ValueError, match="abandoned root has journaled attempt starts"):
            verify_pressure_export_evidence(export, read_sealed(export / "index.json"), receipt_directory=receipts)
        with pytest.raises(ValueError, match="abandoned root has journaled attempt starts"):
            export_live_review(root, tmp_path / "refused-abandoned-export",
                study_directory=study, scorer=score_trial, pressure_review_selector=pressure_review_selection,
                receipt_directory=receipts)
        check_receipt(root, receipts, study_directory=study)
    finally:
        marker.unlink()


@pytest.mark.parametrize("mutation", ["source_root_plan_hash", "score", "status", "missing_row", "duplicate_row"])
def test_chain_consumers_rederive_earlier_and_latest_rows(successor_inputs, mutation):
    _, _, _, receipts, groups, exports, _ = successor_inputs
    original = read_sealed(exports["core", 2] / "index.json")
    for version in (1, 2):
        index = deepcopy(original)
        identity = groups["core"][0][version - 1][1]["seal_hash"]
        row = next(row for row in index["rows"] if row["source_root_plan_hash"] == identity)
        if mutation == "source_root_plan_hash":
            row[mutation] = groups["core"][0][2 - version][1]["seal_hash"]
        elif mutation == "score":
            row["score"]["task"]["outcome"] = not bool(row["score"]["task"]["outcome"])
        elif mutation == "status":
            row["status"] = "unrun"
        elif mutation == "missing_row":
            index["rows"].remove(row)
        else:
            index["rows"].append(deepcopy(row))
        index = seal({key: value for key, value in index.items() if key != "seal_hash"})
        with pytest.raises(ValueError, match="differs|omits|duplicate"):
            verify_pressure_export_evidence(exports["core", 2], index, receipt_directory=receipts)


def test_consistent_derived_chain_changes_disagree_with_committed_receipt(successor_inputs):
    _, study, _, receipts, groups, exports, _ = successor_inputs
    root, original = groups["core"][0][-1]
    old_hash = original["seal_hash"]
    plan = deepcopy(original)
    earlier_hash = groups["core"][0][0][1]["seal_hash"]
    plan["consumed_attempts"]["prior_roots"] = [record for record in plan["consumed_attempts"]["prior_roots"]
                                              if record["plan_hash"] != earlier_hash]
    plan = seal({key: value for key, value in plan.items() if key != "seal_hash"})
    registry = study / live.STUDY_REGISTRY
    old_registration = read_sealed(registry / f"{old_hash}.json")
    altered_registration = seal({**{key: value for key, value in old_registration.items() if key != "seal_hash"},
                                 "plan_hash": plan["seal_hash"]})
    index = deepcopy(read_sealed(exports["core", 2] / "index.json"))
    index["source_plan_hash"] = plan["seal_hash"]
    index["study_registration"]["plan_hash"] = plan["seal_hash"]
    for row in index["rows"]:
        if row["source_root_plan_hash"] == old_hash:
            row["source_root_plan_hash"] = plan["seal_hash"]
    index = seal({key: value for key, value in index.items() if key != "seal_hash"})
    # Keep the original receipt's hashes under the consistently changed plan identity.
    altered_receipt = receipts / f"{plan['seal_hash']}.json"
    altered_receipt.write_bytes((receipts / f"{old_hash}.json").read_bytes())
    git(receipts, "add", "--", altered_receipt.name)
    git(receipts, "commit", "-qm", "Retain unchanged hashes under an altered derived identity.")
    original_plan_bytes = (root / live.LIVE_PLAN_FILE).read_bytes()
    original_registration_bytes = (registry / f"{old_hash}.json").read_bytes()
    finalizations = registry / live.FINALIZED_DIRECTORY
    original_finalization_bytes = (finalizations / f"{old_hash}.json").read_bytes()
    finalization = read_sealed(finalizations / f"{old_hash}.json")
    altered_finalization = seal({**{key: value for key, value in finalization.items() if key != "seal_hash"},
                                "plan_hash": plan["seal_hash"]})
    try:
        atomic_json(root / live.LIVE_PLAN_FILE, plan)
        (registry / f"{old_hash}.json").unlink()
        atomic_json(registry / f"{plan['seal_hash']}.json", altered_registration)
        (finalizations / f"{old_hash}.json").unlink()
        atomic_json(finalizations / f"{plan['seal_hash']}.json", altered_finalization)
        with pytest.raises(ValueError, match="receipt mismatch"):
            verify_pressure_export_evidence(exports["core", 2], index, receipt_directory=receipts)
    finally:
        (root / live.LIVE_PLAN_FILE).write_bytes(original_plan_bytes)
        (registry / f"{plan['seal_hash']}.json").unlink()
        (registry / f"{old_hash}.json").write_bytes(original_registration_bytes)
        (finalizations / f"{plan['seal_hash']}.json").unlink()
        (finalizations / f"{old_hash}.json").write_bytes(original_finalization_bytes)


@pytest.mark.parametrize("version", [1, 2])
def test_chain_checks_each_row_roots_committed_receipt(successor_inputs, version):
    _, _, _, receipts, groups, exports, _ = successor_inputs
    root, _ = groups["core"][0][version - 1]
    path = next(root.rglob("attempt.json"))
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n")
        export = exports["core", 2]
        with pytest.raises(ValueError, match="receipt mismatch"):
            verify_pressure_export_evidence(export, read_sealed(export / "index.json"), receipt_directory=receipts)
    finally:
        path.write_bytes(original)
