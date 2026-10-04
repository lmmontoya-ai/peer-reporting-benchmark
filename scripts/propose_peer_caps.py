"""Prepare separate smoke and collection limits from verified compatibility usage.

This command never starts a model. Three engineering calls do not establish a
model ranking or a stable resource distribution.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from swarm_auth_bench.peer_reporting.config import MODELS, StudyConfig
from swarm_auth_bench.peer_reporting.fixtures import (
    audit_packet_lengths,
    build_collection_fixtures,
    build_fixture,
)
from swarm_auth_bench.peer_reporting.live import verify_phase
from swarm_auth_bench.peer_reporting.storage import atomic_json, seal


def propose(directory: Path, output: Path, *, prior_phases: tuple[Path, ...] = ()) -> dict:
    report = verify_phase(directory)
    if report["phase"] != "compatibility" or set(report["qualified_models"]) != set(MODELS):
        raise ValueError("all three compatibility records must pass with known usage before proposing budgets")
    prior = [verify_phase(path) for path in prior_phases]
    if any(item["phase"] != "compatibility" for item in prior):
        raise ValueError("prior engineering evidence must be a compatibility phase")
    hashes = [report["plan_hash"], *(item["plan_hash"] for item in prior)]
    if len(set(hashes)) != len(hashes):
        raise ValueError("a phase cannot be counted twice")
    rows = report["entries"]
    peak_tokens = max(row["usage_total_tokens"] for row in rows)
    peak_seconds = max(row["elapsed_seconds"] for row in rows)
    peak_requests = max(row["tool_request_count"] for row in rows)
    stop = max(20000, math.ceil(1.5 * peak_tokens / 5000) * 5000)
    reservation = math.ceil(1.25 * stop / 5000) * 5000
    wall = max(120, math.ceil(2 * peak_seconds / 30) * 30)
    individual = {
        "max_trial_wall_seconds": wall, "drain_grace_seconds": 10,
        "max_tool_requests_per_trial": max(16, 2 * peak_requests),
        "trial_observed_token_stop_target": stop, "reserved_tokens_per_trial": reservation,
    }
    phase_caps = {name: {**individual, "max_concurrency": 1,
                        "collection_observed_token_stop_target": count * reservation,
                        "collection_wall_seconds": count * (wall + 10 + 15) + 120}
                  for name, count in (("smoke", 9), ("collection", 216))}
    fixtures = build_collection_fixtures(173) + [build_fixture(4, 1, split="smoke", seed=173)]
    length_audit = audit_packet_lengths(fixtures)
    midpoint = length_audit["suggested_midrange_target_bytes"]
    target = min({math.floor(midpoint), math.ceil(midpoint)}, key=lambda value: (
        max(abs(size - value) / value for size in length_audit["packet_bytes"].values()), value))
    config = StudyConfig(collection_revision="compact-v3-resource-candidate-1", seed=173,
                         phase_caps=phase_caps, packet_byte_target=target)
    proposal = {
        "kind": "peer_reporting_resource_proposal", "status": "candidate_collection_gates_not_passed",
        "basis_plan_hash": report["plan_hash"], "basis": report["resource_observations"],
        "qualification_call_count": report["live_model_call_starts"],
        "prior_engineering_phases": [{key: item[key] for key in
                                       ("plan_hash", "live_model_call_starts", "ledger", "halted")}
                                      for item in prior],
        "total_engineering_call_starts": report["live_model_call_starts"] + sum(
            item["live_model_call_starts"] for item in prior),
        "formula": {
            "trial_token_stop": "max(20000, ceil(1.5 * largest reported qualification usage / 5000) * 5000)",
            "reservation": "ceil(1.25 * trial_token_stop / 5000) * 5000",
            "trial_wall": "max(120, ceil(2 * largest observed elapsed seconds / 30) * 30)",
            "tool_requests": "max(16, 2 * largest observed qualification request count)",
            "phase_token_stop": "number of assignments * reservation",
            "phase_wall": "number of assignments * (trial_wall + 10 drain + 15 preflight allowance) + 120",
        },
        "phase_caps": phase_caps, "config": config.to_dict(),
        "packet_length_audit": audit_packet_lengths(fixtures, target),
        "limitations": [
            "One engineering call per model; no statistical resource guarantee.",
            "Tool checks differ from the observational benchmark task.",
            "Reporting rates were not used to choose the limits.",
            "Observed token stops can overshoot and do not impose a hard provider billing cap.",
            "Both phase limits must freeze before smoke; later changes need a new plan and fresh smoke.",
            "The packet audit uses the approved protocol tolerance. Other collection gates remain required.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "resource-proposal.json", seal(proposal))
    atomic_json(output / "study-config.json", config.to_dict())
    return proposal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prior-phase", action="append", default=[], type=Path,
                        help="retain earlier engineering failures and unresolved reservations in the proposal")
    args = parser.parse_args()
    proposal = propose(args.directory, args.output, prior_phases=tuple(args.prior_phase))
    print({"output": str(args.output), "phase_caps": proposal["phase_caps"], "status": proposal["status"]})


if __name__ == "__main__":
    main()
