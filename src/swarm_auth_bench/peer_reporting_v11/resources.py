"""Offline resource proposals from verified engineering archives (spec 10, step 2).

The proposal wraps an exact lane caps record and its evidence. The existing
lane validator accepts only candidate/frozen statuses, so proposed caps are
validated on a temporary frozen copy. Approval never changes the proposal.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path

from ..events import content_hash
from ..peer_reporting.storage import check_seal, seal
from . import PROTOCOL_ID, live
from .config import validate_protocol
from .lanes import (
    CAPS_KIND,
    MAX_GLOBAL_CONCURRENCY,
    PHASES,
    TOOL_REQUEST_CAP,
    lane_caps,
    lane_id,
    lane_order,
    validate_caps_record,
)

PROPOSAL_KIND = "peer_reporting_v11_resource_proposal"
PER_ROW_MARGIN_SECONDS = 15
PHASE_MARGIN_SECONDS = 120
WALL_QUANTUM_SECONDS = 30
FORMULA = {
    "trial_token_stop": "max(60000, ceil(1.5 * largest settled usage / 5000) * 5000)",
    "reservation": "ceil(1.25 * trial_token_stop / 5000) * 5000",
    "trial_wall": "max(180, ceil(2 * largest settled elapsed seconds / 30) * 30)",
    "drain_seconds": 10,
    "tool_requests": "protocol.caps.max_tool_requests_per_trial (32)",
    "global_concurrency": "protocol.caps.global_max_concurrency (1..6)",
    "lane_wall": "ceil((slowest_lane_rows * (trial_wall + drain + 15) * dispatch_waves + 120) / 30) * 30",
    "dispatch_waves": "ceil(active_lanes / global_max_concurrency)",
    "lane_token_target": "(planned_rows + 1) * reservation",
}


def _frozen_caps(caps: dict) -> dict:
    return validate_caps_record({**deepcopy(caps), "caps_status": "frozen"})


def _planned_rows(manifest: dict) -> dict[str, Counter]:
    counts = {phase: Counter() for phase in PHASES}
    # The protocol's compatibility probe has one row in each of the six lanes.
    counts["compatibility"].update(lane_id(model, effort) for model, effort in lane_order())
    rows = manifest.get("assignments")
    if type(rows) is not list or not rows:
        raise ValueError("the sealed study needs planned assignment rows")
    identities = set()
    for row in rows:
        if type(row) is not dict or row.get("split") not in counts or row["split"] == "compatibility":
            raise ValueError("study assignments must name calibration, smoke, or collection")
        identity = row.get("assignment_id")
        if type(identity) is not str or not identity or identity in identities:
            raise ValueError("study assignment IDs must be nonempty and unique")
        identities.add(identity)
        counts[row["split"]][lane_id(row["model"], row["effort"])] += 1
    return counts


def _ignore_reason(row: dict) -> str | None:
    if row["status"] != "archived":
        return f"not_archived:{row['status']}"
    if row.get("usage_settlement") != "settled":
        return f"usage_settlement:{row.get('usage_settlement')}"
    total = row.get("usage_total_tokens")
    if type(total) is not int or total < 0:
        return "settled_usage_is_not_a_nonnegative_integer"
    elapsed = row.get("elapsed_seconds")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        return "elapsed_seconds_is_not_finite_and_nonnegative"
    return None


def propose_caps(roots: Iterable[Path], *, study_directory: Path, phase: str, protocol: dict,
                 prior_caps: dict | None = None) -> dict:
    """Return a sealed proposal; read evidence only through the live archive verifier.

    Calibration uses compatibility roots. Either smoke or collection selects
    the second proposal, which uses calibration roots and sizes both phases.
    Earlier roots sealed in an input root's consumed ledger must also be inputs.
    Other phase walls are retained from prior_caps, the study, or the first root.
    Every active lane in a target phase gets the slowest lane's wall. The margin
    is 15 seconds per row plus 120 per phase, rounded up to 30 seconds. Dispatch
    waves also cover lanes waiting for a slot when global concurrency is below 6.
    """
    if phase not in ("calibration", "smoke", "collection"):
        raise ValueError("proposal phase must be calibration, smoke, or collection")
    validate_protocol(protocol)
    limits = protocol["caps"]
    concurrency = limits["global_max_concurrency"]
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_GLOBAL_CONCURRENCY:
        raise ValueError("protocol global concurrency must be an integer from 1 to 6")
    if (type(limits["max_tool_requests_per_trial"]) is not int
            or limits["max_tool_requests_per_trial"] != TOOL_REQUEST_CAP):
        raise ValueError("the protocol tool request cap must be 32")
    study_directory = Path(study_directory)
    manifest = live.read_study_manifest(study_directory)
    if manifest.get("protocol_id") != PROTOCOL_ID or (
            "protocol" in manifest and manifest["protocol"] != protocol):
        raise ValueError("study manifest and supplied protocol differ")
    counts = _planned_rows(manifest)
    targets = ("calibration",) if phase == "calibration" else ("smoke", "collection")
    if any(not counts[target] for target in targets):
        raise ValueError("the study has no planned rows for a target phase")

    roots = [Path(root) for root in roots]
    if not roots:
        raise ValueError("at least one verified input root is required")
    plans = [(root, live.read_live_plan(root)) for root in roots]
    by_hash = {plan["seal_hash"]: root for root, plan in plans}
    if len(by_hash) != len(plans):
        raise ValueError("an input root cannot be counted twice")
    source_phase = "compatibility" if phase == "calibration" else "calibration"
    inputs, used, ignored = [], [], []
    for root, plan in plans:
        if plan["phase"] != source_phase:
            raise ValueError(f"a {phase} proposal requires {source_phase} input roots")
        prior = (plan.get("consumed_attempts") or {}).get("prior_roots", [])
        missing = [item["plan_hash"] for item in prior if item["plan_hash"] not in by_hash]
        if missing:
            raise ValueError(f"supply every sealed prior root as an input; missing {missing}")
        report = live.verify_live_root(
            root, prior_roots=[by_hash[item["plan_hash"]] for item in prior],
            study_directory=study_directory if source_phase == "calibration" else None)
        registration = report.get("study_registration")
        if source_phase == "calibration" and (registration or {}).get("state") != "finalized":
            raise ValueError("calibration evidence requires a finalized study registration")
        root_used, root_ignored = [], []
        for lane, lane_report in report["lanes"].items():
            for row in lane_report["entries"]:
                observation = {"plan_hash": report["plan_hash"], "lane_id": lane,
                               **{key: row.get(key) for key in ("attempt_id", "status", "usage_settlement",
                                                               "usage_total_tokens", "elapsed_seconds",
                                                               "tool_request_count")}}
                reason = _ignore_reason(row)
                if reason is None:
                    used.append(observation)
                    root_used.append(row["attempt_id"])
                else:
                    observation["reason"] = reason
                    ignored.append(observation)
                    root_ignored.append({"attempt_id": row["attempt_id"], "reason": reason})
        inputs.append({"root": str(root.resolve()), "plan_hash": report["plan_hash"], "phase": source_phase,
                       "attempt_ids_used": root_used, "attempts_ignored": root_ignored,
                       "implementation_changes": report["implementation_changes"],
                       "consumed_attempt_ledger": report["consumed_attempt_ledger"]})
    if not used:
        raise ValueError("no archived settled attempts with integer usage and valid elapsed time")
    peak_tokens = max(row["usage_total_tokens"] for row in used)
    peak_seconds = max(row["elapsed_seconds"] for row in used)
    # Integer arithmetic avoids float rounding at exact token boundaries.
    stop = max(60000, ((3 * peak_tokens + 9999) // 10000) * 5000)
    reservation = ((5 * stop + 19999) // 20000) * 5000
    wall = max(180, math.ceil(2 * peak_seconds / 30) * 30)
    baseline = validate_caps_record(
        prior_caps if prior_caps is not None else manifest.get("caps", plans[0][1]["caps"]))
    revision_hash = content_hash([inputs, used, ignored, manifest["seal_hash"], protocol, baseline, FORMULA])
    caps = {"kind": CAPS_KIND, "protocol_id": PROTOCOL_ID,
            "revision": f"resource-{targets[0]}-{revision_hash[:16]}",
            "caps_status": "proposed",
            "trial": {"max_trial_wall_seconds": wall, "drain_grace_seconds": 10,
                      "max_tool_requests_per_trial": TOOL_REQUEST_CAP, "trial_observed_token_stop_target": stop,
                      "reserved_tokens_per_trial": reservation},
            "lane_wall_seconds": deepcopy(baseline["lane_wall_seconds"]), "global_max_concurrency": concurrency}
    sizing = {}
    for target in targets:
        planned = counts[target]
        slowest = max(planned.values())
        waves = math.ceil(len(planned) / concurrency)
        raw_wall = slowest * (wall + 10 + PER_ROW_MARGIN_SECONDS) * waves + PHASE_MARGIN_SECONDS
        lane_wall = math.ceil(raw_wall / WALL_QUANTUM_SECONDS) * WALL_QUANTUM_SECONDS
        caps["lane_wall_seconds"][target] = lane_wall
        sizing[target] = {"slowest_lane_planned_rows": slowest, "dispatch_waves": waves,
                          "per_row_margin_seconds": PER_ROW_MARGIN_SECONDS,
                          "phase_margin_seconds": PHASE_MARGIN_SECONDS,
                          "unrounded_wall_seconds": raw_wall, "lanes": {}}
    validated = _frozen_caps(caps)
    for target, sizing_record in sizing.items():
        for lane, count in counts[target].items():
            derived = lane_caps(validated, target, count)
            sizing_record["lanes"][lane] = {"planned_rows": count, "planned_reservations": count * reservation,
                                             "caps": derived}
    return seal({"kind": PROPOSAL_KIND, "protocol_id": PROTOCOL_ID, "status": "proposed",
                 "phase": phase, "target_phases": list(targets), "caps": caps,
                 "study_directory": str(study_directory.resolve()), "study_manifest_hash": manifest["seal_hash"],
                 "protocol_hash": content_hash(protocol), "prior_caps": baseline, "inputs": inputs,
                 "attempts_used": used, "attempts_ignored": ignored, "formula": deepcopy(FORMULA),
                 "measured_peaks": {"usage_total_tokens": peak_tokens, "elapsed_seconds": peak_seconds,
                                    "tool_request_count": max((row["tool_request_count"] or 0) for row in used)},
                 "per_lane_sizing": sizing, "live_model_calls": 0})


def freeze_caps(proposal: dict, approval_text: str) -> dict:
    """Return a separately sealed frozen copy with approval, leaving the proposal intact.

    ``result['caps']`` is the raw frozen caps record accepted by live commands.
    The surrounding record retains approval and the exact original proposal hash.
    """
    check_seal(proposal)
    if (proposal.get("kind") != PROPOSAL_KIND or proposal.get("protocol_id") != PROTOCOL_ID
            or proposal.get("status") != "proposed" or proposal.get("caps", {}).get("caps_status") != "proposed"):
        raise ValueError("freeze-caps requires an unapproved v1.1 resource proposal")
    if type(approval_text) is not str or not approval_text.strip():
        raise ValueError("approval text must be a nonempty string")
    frozen = deepcopy(proposal)
    proposal_hash = frozen.pop("seal_hash")
    frozen["caps"] = _frozen_caps(frozen["caps"])
    frozen["status"] = "frozen"
    frozen["approval"] = {"status": "approved", "text": approval_text, "proposal_hash": proposal_hash,
                          "caps_hash": content_hash(frozen["caps"])}
    return seal(frozen)
