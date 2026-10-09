"""Sealed assignment populations for the one-root-per-Level-P-arm review rule.

These checks read plans and registrations only; they never import scoring or review code.
"""

from __future__ import annotations

from pathlib import Path

from ..peer_reporting.storage import read_sealed, safe_child
from .live import read_live_plan, read_study_manifest, registered_root_path, registered_roots


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
                                   source_plan_hash: str) -> list[tuple[str, list[dict]]]:
    """Read every registered phase root's hash-bound lane plans, before analysis exclusions."""
    manifest = read_study_manifest(study_directory)
    _require(manifest["seal_hash"] == study_manifest_hash, "P review population belongs to another study")
    study_assignments = {row["assignment_id"]: row for row in manifest["assignments"]}
    partitions = []
    for registration in registered_roots(study_directory):
        if registration["phase"] != phase:
            continue
        root = registered_root_path(study_directory, registration)
        plan = read_live_plan(root)
        _require(plan["seal_hash"] == registration["plan_hash"] and plan["phase"] == phase
                 and plan["source"]["study_manifest_hash"] == study_manifest_hash
                 and registration["study_manifest_hash"] == study_manifest_hash,
                 "P review root plan differs from its study registration")
        assignments, seen = [], set()
        for lane in plan["lanes"]:
            lane_plan = read_sealed(safe_child(root, lane["path"]) / "phase-plan.json")
            _require(lane_plan["seal_hash"] == lane["plan_hash"] and lane_plan["phase"] == phase,
                     "P review lane plan differs from its sealed root plan")
            for entry in lane_plan["planned_order"]:
                identifier = entry["entry_id"]
                _require(identifier not in seen and identifier in study_assignments,
                         "P review root assignment missing or duplicated")
                seen.add(identifier)
                assignment = study_assignments[identifier]
                _require(entry["arm"] == assignment["arm"] and entry["level"] == assignment["level"],
                         "P review root assignment differs from its study")
                assignments.append(assignment)
        partitions.append((plan["seal_hash"], assignments))
    _require(source_plan_hash in {identity for identity, _ in partitions},
             "P review source root is not registered in the study phase")
    return partitions


def pressure_export_partitions(index: dict) -> list[tuple[str, list[dict]]]:
    """Reopen the bound study registry so roots added after export are checked too."""
    directory = index.get("pressure_review_study_directory")
    _require(type(directory) is str and Path(directory).is_absolute(),
             "P review export requires its registered study directory")
    registration = index["study_registration"]
    _require(index["source_plan_hash"] == registration["plan_hash"] and index["phase"] == registration["phase"],
             "P review export root binding differs")
    partitions = registered_pressure_partitions(Path(directory), phase=index["phase"],
        study_manifest_hash=registration["study_manifest_hash"], source_plan_hash=index["source_plan_hash"])
    identifiers = {row["assignment_id"] for identity, rows in partitions if identity == index["source_plan_hash"]
                   for row in rows}
    _require(all(row["assignment_id"] in identifiers for row in index["rows"]),
             "P review export contains an assignment outside its sealed root plan")
    return partitions
