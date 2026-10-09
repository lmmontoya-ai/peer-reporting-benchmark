"""Execution-side binding checks for a sealed, offline-verified pilot decision."""

from __future__ import annotations

from ..peer_reporting.storage import check_seal

DECISION_KIND = "peer_reporting_v11_pressure_pilot_decision"
CORE_ARMS = {"pressure_core_xhigh", "pressure_core_low"}
PILOT_ARMS = {"pressure_pilot_xhigh", "pressure_pilot_low"}
GATE_ARMS = {"pressure_gate_xhigh", "pressure_gate_low"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_core_decision(record: dict, *, study_manifest_hash: str) -> dict:
    """Check frozen decision data without loading or rescoring post-hoc evidence."""
    _require(type(record) is dict and record.get("kind") == DECISION_KIND, "core requires a pilot decision record")
    check_seal(record)
    _require(record["study_manifest_hash"] == study_manifest_hash, "core pilot decision belongs to another study")
    _require(not record["ceiling"] or record["ceiling_choice"] == "a", "core ceiling requires user choice (a)")
    _require(record["decision"] == "proceed", "core refuses a stop pilot decision")
    _require(record["Psel"] == "P3", "core supports Psel P3 only; a fallback needs an amendment")
    return record


def verify_core_binding(plan: dict, *, entry_lanes: set[str] | None = None, entry_arms: set[str] | None = None) -> None:
    arms = entry_arms if entry_arms is not None else set(plan.get("selected_arms") or [])
    if not arms & CORE_ARMS:
        return
    record = check_core_decision(plan.get("pilot_decision"), study_manifest_hash=plan["source"]["study_manifest_hash"])
    _require(plan.get("pilot_decision_hash") == record["seal_hash"], "core pilot decision hash binding differs")
    lanes = entry_lanes if entry_lanes is not None else {lane["lane_id"] for lane in plan["lanes"]}
    allowed = set(record["eligible_lanes"])
    _require(lanes <= allowed and bool(lanes), "core contains a lane excluded by its pilot decision")
