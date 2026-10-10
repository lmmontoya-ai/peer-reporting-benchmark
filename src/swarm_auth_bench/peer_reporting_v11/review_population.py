"""Complete sealed assignment populations for the one-root-per-Level-P-arm review rule.

These checks read plans and registrations only; they never import scoring or review code.
"""

from __future__ import annotations

from pathlib import Path

from ..peer_reporting.storage import read_sealed, safe_child
from .live import (
    check_assignment_binding,
    read_live_plan,
    read_study_manifest,
    registered_root_path,
    registered_roots,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_pressure_arm_roots(partitions: list[tuple[str, list[dict]]], *, unit: str = "registered root") -> None:
    """Refuse any P arm planned in multiple partitions, including excluded assignments."""
    owners = {}
    for identity, assignments in partitions:
        for row in assignments:
            if row.get("level") != "P":
                continue
            arm = row["arm"]
            _require(arm not in owners or owners[arm] == identity,
                     f"level P arm {arm} has planned assignments in more than one {unit}")
            owners[arm] = identity


def registered_pressure_partitions(study_directory: Path, *, phase: str, study_manifest_hash: str,
                                   source_plan_hash: str,
                                   receipt_directory: Path | None = None) -> list[tuple[str, list[dict]]]:
    """Read every registered phase root's hash-bound lane plans, before analysis exclusions."""
    manifest = read_study_manifest(study_directory)
    _require(manifest["seal_hash"] == study_manifest_hash, "P review population belongs to another study")
    study_assignments = {row["assignment_id"]: row for row in manifest["assignments"]}
    partitions = []
    for registration in registered_roots(study_directory):
        root = registered_root_path(study_directory, registration)
        if registration["state"] == "abandoned" and not (root / "live-plan.json").exists():
            continue
        plan = read_live_plan(root)
        _require(plan["seal_hash"] == registration["plan_hash"] and plan["phase"] == registration["phase"]
                 and plan["source"]["study_manifest_hash"] == study_manifest_hash
                 and registration["study_manifest_hash"] == study_manifest_hash,
                 "P review root plan differs from its study registration")
        entries, seen = [], set()
        lane_plans = []
        for lane in plan["lanes"]:
            lane_plan = read_sealed(safe_child(root, lane["path"]) / "phase-plan.json")
            _require(lane_plan["seal_hash"] == lane["plan_hash"],
                     "P review lane plan differs from its sealed root plan")
            lane_plans.append(lane_plan)
            for entry in lane_plan["planned_order"]:
                identifier = entry["entry_id"]
                _require(identifier not in seen and identifier in study_assignments,
                         "P review root assignment missing or duplicated")
                seen.add(identifier)
                entries.append(entry)
        # Historical entries leave fixture-only labels out of their byte format.
        # Resolve omitted fixed labels from the primary sealed-study fixture.
        fixtures = {}
        aliases = {"assignment_id": "entry_id", "effort": "reasoning_effort"}
        for entry in entries:
            assignment = study_assignments[entry["entry_id"]]
            if any(value is not None and aliases.get(key, key) not in entry
                   for key, value in assignment.items()):
                identifier = assignment["fixture_id"]
                if identifier not in fixtures:
                    stored = read_sealed(safe_child(study_directory, manifest["fixtures"][identifier]["path"]))
                    fixtures[identifier] = {key: value for key, value in stored.items() if key != "seal_hash"}
        phases = {study_assignments[entry["entry_id"]]["split"] for entry in entries}
        _require(len(phases) == 1, "P review root assignments require one sealed-study phase")
        canonical_phase = phases.pop()
        assignments = check_assignment_binding(entries, manifest=manifest, source=plan["source"], phase=canonical_phase,
                                                 fixtures=fixtures)
        _require(plan["phase"] == registration["phase"] == canonical_phase
                 and all(lane_plan["phase"] == canonical_phase for lane_plan in lane_plans),
                 "P review root phase differs from its sealed-study assignments")
        has_primary_evidence = any((root / "lanes").rglob("journal.jsonl")) or any(
            (root / "lanes").rglob("attempt.json"))
        if registration["state"] == "finalized" or has_primary_evidence:
            from .receipts import check_receipt

            check_receipt(root, receipt_directory, study_directory=study_directory)
        if canonical_phase != phase:
            continue
        partitions.append((plan["seal_hash"], assignments))
    _require(source_plan_hash in {identity for identity, _ in partitions},
             "P review source root is not registered in the study phase")
    return partitions



def check_pressure_export_population(rows: list[dict], partitions: list[tuple[str, list[dict]]], *,
                                     source_plan_hash: str) -> None:
    """Require every represented P arm's full planned population before exclusions or scoring."""
    assignments = {row["assignment_id"]: row for identity, population in partitions if identity == source_plan_hash
                   for row in population}
    identifiers = {row["assignment_id"] for row in rows}
    _require(identifiers <= set(assignments),
             "P review export contains an assignment outside its sealed root plan")
    _require(len(identifiers) == len(rows), "P review export contains a duplicate assignment")
    represented = {assignments[identifier]["arm"] for identifier in identifiers
                   if assignments[identifier]["level"] == "P"}
    for arm in sorted(represented):
        planned = {identifier for identifier, row in assignments.items() if row["level"] == "P" and row["arm"] == arm}
        _require(planned <= identifiers,
                 f"level P arm {arm} export omits planned assignments from its registered root")


def pressure_export_partitions(index: dict, *, receipt_directory: Path | None = None) -> list[tuple[str, list[dict]]]:
    """Reopen the bound study registry so roots added after export are checked too."""
    directory = index.get("pressure_review_study_directory")
    _require(type(directory) is str and Path(directory).is_absolute(),
             "P review export requires its registered study directory")
    registration = index["study_registration"]
    _require(index["source_plan_hash"] == registration["plan_hash"] and index["phase"] == registration["phase"],
             "P review export root binding differs")
    partitions = registered_pressure_partitions(Path(directory), phase=index["phase"],
        study_manifest_hash=registration["study_manifest_hash"], source_plan_hash=index["source_plan_hash"],
        receipt_directory=receipt_directory)
    check_pressure_export_population(index["rows"], partitions, source_plan_hash=index["source_plan_hash"])
    return partitions
