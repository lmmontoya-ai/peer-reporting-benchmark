"""Offline gate/main builders and execution-side gate ordering, without review imports."""

from copy import deepcopy
from pathlib import Path

from .bundle import load_bundle
from .lanes import lane_id
from .queue_sizing import check_sizing_record, execution_population, require, sized_caps
from .schemas import TOOL_SCHEMA_VERSION_Q


def gate_prerequisites(study_directory, *, receipt_directory, require_execution=False,
                       compatibility_directories=(), repair_evidence_directory=None):
    from . import live
    from .receipts import check_receipt

    roots = [row for row in live.registered_roots(study_directory) if row["phase"] == "gate" and row["state"] != "abandoned"]
    require(bool(roots) and all(row["state"] == "finalized" for row in roots),
            "main requires a finalized and receipted gate root first")
    result = []
    for registration in roots:
        root = live.registered_root_path(study_directory, registration)
        plan = live.read_live_plan(root)
        live.check_root_assignment_binding(root, plan, study_directory)
        check_receipt(root, receipt_directory, study_directory=study_directory)
        if require_execution:
            report = live.verify_live_root(root, study_directory=study_directory, receipt_directory=receipt_directory,
                                           compatibility_directories=compatibility_directories,
                                           repair_evidence_directory=repair_evidence_directory)
            require(not report["unreconciled_starts"], "main requires finished or deliberately stopped gate execution")
            journals = live.lane_journals(root, plan)
            for lane, records in journals.items():
                lifecycle = [row for row in records if row["kind"] in ("run_opened", "run_closed")]
                require(bool(lifecycle) and len(lifecycle) % 2 == 0 and all(
                        row["kind"] == ("run_opened" if index % 2 == 0 else "run_closed")
                        for index, row in enumerate(lifecycle)),
                        "main requires receipt-verified finished or deliberately stopped gate execution")
                pending = any(row["status"] in live.UNSTARTED for row in report["lanes"][lane]["entries"])
                halted = lifecycle[-1]["data"].get("halted") or {}
                deliberate = set(halted.get("holds", [])) & {
                    "soft_stop", "hard_stop", "admission_cutoff", "forced_stop_deadline"}
                require(not pending or halted.get("reason") == "global_admission_hold" and bool(deliberate),
                        "main requires receipt-verified finished or deliberately stopped gate execution")
        result.append({"plan_hash": plan["seal_hash"], "sizing_record_hash": plan["sizing_record_hash"]})
    return sorted(result, key=lambda row: row["plan_hash"])


def build_study_plan(phase, *, sizing_record, sizing_verifier=None, revision, study_directory,
                     compatibility_directories=(), smoke_directory=None, prior_roots=(), bundle=None,
                     study_verifier=None, receipt_directory=None, repair_evidence_directory=None):
    from . import live

    require(phase in ("gate", "main"), "Q study builder requires gate or main")
    require(study_directory is not None, "Q study builder requires its sealed study directory")
    bundle = (bundle or load_bundle()).tool_set(TOOL_SCHEMA_VERSION_Q)
    manifest = live.read_study_manifest(study_directory)
    require(manifest.get("protocol", {}).get("level") == "Q", "sized plans require a level Q study")
    require(sizing_verifier is not None, "gate/main build requires offline sizing evidence verification")
    check_sizing_record(sizing_record, study_manifest_hash=manifest["seal_hash"])
    live.check_study_binding(manifest, manifest["caps"], bundle)
    report = (study_verifier or live.verify_sealed_study)(Path(study_directory), manifest["caps"])
    require(type(report) is dict and report.get("valid") is True, "sealed Q study verification failed")
    caps = sized_caps(manifest, sizing_record)
    rows = execution_population(manifest, phase, sizing_record, caps)
    _, fixtures, source = live.load_study(study_directory, phase)
    ledger = live.prior_root_ledger(prior_roots, phase=phase, source=source, study_directory=study_directory,
        bundle=bundle, receipt_directory=receipt_directory, repair_evidence_directory=repair_evidence_directory)
    require(all(live.read_live_plan(root).get("sizing_record_hash") == sizing_record["seal_hash"] for root in prior_roots),
            "a changed sizing decision needs an amendment and a new screening")
    consumed = set(ledger["consumed_attempt_ids"])
    ledger["excluded_assignment_ids"] = sorted(row["assignment_id"] for row in rows
        if row["assignment_id"] + live.ATTEMPT_SUFFIX in consumed)
    rows = [row for row in rows if row["assignment_id"] + live.ATTEMPT_SUFFIX not in consumed]
    require(bool(rows), "every selected study assignment is already consumed")
    smoke_ids = sorted(row["assignment_id"] for row in manifest["assignments"] if row["phase"] == "smoke")
    gates = live.check_phase_gates(phase, sorted({lane_id(row["model"], row["effort"]) for row in rows}),
        bundle=bundle, source=source, compatibility_directories=compatibility_directories, smoke_directory=smoke_directory,
        smoke_assignment_ids=smoke_ids, study_directory=study_directory, receipt_directory=receipt_directory,
        repair_evidence_directory=repair_evidence_directory)
    if not gates["passed"]:
        raise live.GateError(gates["failures"])
    if phase == "main":
        require(all(row["sizing_record_hash"] == sizing_record["seal_hash"] for row in gates["evidence"]["gate_roots"]),
                "main sizing record differs from its gate roots")
    built = live.build_assignment_plan(phase, rows, fixtures, caps, revision=revision, source=source,
        gate_evidence=gates["evidence"], consumed_attempts=ledger, bundle=bundle, study_manifest=manifest,
        study_directory=study_directory, receipt_directory=receipt_directory, repair_evidence_directory=repair_evidence_directory,
        sizing_record=sizing_record, sizing_verifier=sizing_verifier)
    built[0]["smoke_assignment_ids"] = smoke_ids
    built[0]["sizing_record"] = deepcopy(sizing_record)
    return built


def build_gate_plan(**options):
    return build_study_plan("gate", **options)


def build_main_plan(**options):
    return build_study_plan("main", **options)
