"""Complete Level P arm populations across verified successor roots.

These checks bind plans and registrations to journals and receipts without importing scoring or review code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .live import (
    check_start_claims,
    checked_pressure_roots,
    lane_journals,
    read_study_manifest,
    verify_consumed_ledger,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_pressure_arm_roots(partitions: list[tuple[str, list[dict]]], *, unit: str = "registered root") -> None:
    """Refuse a P arm in multiple independent populations, including excluded rows."""
    owners = {}
    for identity, assignments in partitions:
        for row in assignments:
            if row.get("level") not in ("P", "Q"):
                continue
            arm = row["arm"]
            _require(arm not in owners or owners[arm] == identity,
                     f"level P arm {arm} has planned assignments in more than one {unit}")
            owners[arm] = identity


@dataclass
class PressureRoot:
    directory: Path
    plan: dict
    assignments: list[dict]
    started: set[str]


@dataclass
class PressurePopulation:
    roots: dict[str, PressureRoot]
    ancestors: dict[str, set[str]]
    arm_latest: dict[str, str]
    partitions: list[tuple[str, list[dict]]]

    def source_roots(self, source_plan_hash: str) -> list[str]:
        """Arms sharing a root export together from their latest successor."""
        arms = {row["arm"] for row in self.roots[source_plan_hash].assignments if row["level"] in ("P", "Q")}
        for arm in sorted(arms):
            latest = self.arm_latest[arm]
            _require(latest == source_plan_hash,
                     f"level P arm {arm} export is incomplete; export latest root "
                     f"{self.roots[latest].directory} ({latest})")
        arms = {arm for arm, latest in self.arm_latest.items() if latest == source_plan_hash}
        return sorted((identity for identity in self.ancestors[source_plan_hash]
                       if any(row["arm"] in arms for row in self.roots[identity].assignments)),
                      key=lambda identity: (len(self.ancestors[identity]), identity))


def registered_pressure_population(study_directory: Path, *, phase: str, study_manifest_hash: str,
                                   source_plan_hash: str, receipt_directory: Path | None = None,
                                   repair_evidence_directory: Path | None = None) -> PressurePopulation:
    """Bind successor links to registered plans, journals and committed receipts."""
    manifest = read_study_manifest(study_directory)
    _require(manifest["seal_hash"] == study_manifest_hash, "P review population belongs to another study")
    roots = {}
    for checked in checked_pressure_roots(study_directory, receipt_directory=receipt_directory):
        root, registration, plan = checked.directory, checked.registration, checked.plan
        if registration["state"] == "abandoned":
            continue
        if plan["phase"] != phase:
            continue
        assignments = checked.assignments
        seen = {assignment["assignment_id"] for assignment in assignments}
        journals = lane_journals(root, plan) if checked.has_primary_evidence else {}
        if journals:
            check_start_claims(study_directory, plan, root, journals)
        started = {record["data"]["entry_id"] for records in journals.values() for record in records
                   if record["kind"] == "attempt_started"}
        _require(started <= seen, "P review journal starts an assignment outside its sealed plan")
        roots[plan["seal_hash"]] = PressureRoot(root, plan, assignments, started)
    _require(source_plan_hash in roots, "P review source root is not registered in the study phase")
    links = {}
    for identity, root in roots.items():
        ledger = root.plan.get("consumed_attempts") or {}
        prior = [record["plan_hash"] for record in ledger.get("prior_roots", [])]
        _require(len(prior) == len(set(prior)) and set(prior) <= set(roots),
                 "P review consumed ledger names duplicate or unregistered phase roots")
        if ledger:
            verify_consumed_ledger(root.directory, root.plan, [roots[item].directory for item in prior],
                                   study_directory=study_directory, receipt_directory=receipt_directory,
                                   repair_evidence_directory=repair_evidence_directory)
        links[identity] = set(prior)
    ancestors = {}

    def ancestry(identity, visiting):
        _require(identity not in visiting, "P review consumed ledgers contain a successor cycle")
        if identity not in ancestors:
            ancestors[identity] = {identity}
            for prior in links[identity]:
                ancestors[identity] |= ancestry(prior, visiting | {identity})
        return ancestors[identity]

    for identity in roots:
        ancestry(identity, set())
    owners = {}
    for identity, root in roots.items():
        for row in root.assignments:
            if row["level"] in ("P", "Q"):
                owners.setdefault(row["arm"], set()).add(identity)
    for arm, identities in owners.items():
        _require(any(identities <= ancestors[identity] for identity in identities),
                 f"level P arm {arm} has planned assignments in more than one registered root "
                 "outside a verified successor chain")
    # Arms that shared a root stay in one export if a successor plans only some
    # of them. Disjoint arms (for example gate/pilot and core) keep their exports.
    components = []
    for identities in owners.values():
        merged = set(identities)
        separate = []
        for component in components:
            if component & merged:
                merged |= component
            else:
                separate.append(component)
        components = separate + [merged]
    arm_latest = {}
    for component in components:
        arms = {arm for arm, identities in owners.items() if identities & component}
        latest = [identity for identity in component if component <= ancestors[identity]]
        _require(len(latest) == 1,
                 f"level P arms {sorted(arms)} require one latest registered root in their successor chain")
        arm_latest.update(dict.fromkeys(arms, latest[0]))
    for arm, identities in owners.items():
        starts = set()
        for identity in identities:
            root = roots[identity]
            arm_starts = root.started & {row["assignment_id"] for row in root.assignments if row["arm"] == arm}
            _require(not starts & arm_starts, f"level P arm {arm} has an assignment started in two chain roots")
            starts |= arm_starts
    grouped = {}
    for identity, root in roots.items():
        for row in root.assignments:
            owner = arm_latest[row["arm"]] if row["level"] in ("P", "Q") else identity
            grouped.setdefault(owner, {})[row["assignment_id"]] = row
    partitions = [(identity, list(rows.values())) for identity, rows in grouped.items()]
    return PressurePopulation(roots, ancestors, arm_latest, partitions)


def registered_pressure_partitions(study_directory: Path, **kwargs) -> list[tuple[str, list[dict]]]:
    """Return one complete planned population per arm's latest successor root."""
    return registered_pressure_population(study_directory, **kwargs).partitions



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
                   if assignments[identifier]["level"] in ("P", "Q")}
    for arm in sorted(represented):
        planned = {identifier for identifier, row in assignments.items() if row["level"] in ("P", "Q") and row["arm"] == arm}
        _require(planned <= identifiers,
                 f"level P arm {arm} export omits planned assignments from its registered root")


def pressure_export_partitions(index: dict, *, receipt_directory: Path | None = None,
                               repair_evidence_directory: Path | None = None) -> list[tuple[str, list[dict]]]:
    """Reopen the bound study registry so roots added after export are checked too."""
    directory = index.get("pressure_review_study_directory")
    _require(type(directory) is str and Path(directory).is_absolute(),
             "P review export requires its registered study directory")
    registration = index["study_registration"]
    _require(index["source_plan_hash"] == registration["plan_hash"] and index["phase"] == registration["phase"],
             "P review export root binding differs")
    population = registered_pressure_population(Path(directory), phase=index["phase"],
        study_manifest_hash=registration["study_manifest_hash"], source_plan_hash=index["source_plan_hash"],
        receipt_directory=receipt_directory, repair_evidence_directory=repair_evidence_directory)
    population.source_roots(index["source_plan_hash"])
    partitions = population.partitions
    check_pressure_export_population(index["rows"], partitions, source_plan_hash=index["source_plan_hash"])
    return partitions
