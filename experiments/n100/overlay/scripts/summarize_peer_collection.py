"""Export factual, verified peer-reporting observations without contacting a provider.

Run with the repository environment: python scripts/summarize_peer_collection.py
COLLECTION --output NEW_DIRECTORY. Source archives are never rewritten. The existing
verifier may acquire its budget lock. Use a closed, fetched collection snapshot.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.live_archive import attempt_summary
from swarm_auth_bench.peer_reporting.live_review import inspect_live_collection
from swarm_auth_bench.peer_reporting.score import aggregate_bounds
from swarm_auth_bench.peer_reporting.storage import read_sealed, safe_child

DIMENSIONS = ("split", "model", "prompt_condition", "N", "K", "block", "variant")
ENDPOINTS = (
    "private_supported_shared_concern", "private_supported_focal_concern",
    "private_unsupported_material_claim", "any_output_unsupported_material_claim",
    "control_private_supported_concern", "response_fully_accurate",
)
LIMITS = (
    "Mechanical attempts and accepted reports do not establish justified reporting.",
    "Semantic accuracy requires bound final human labels; pending labels remain unknown.",
    "Smoke is separate from the 54-assignment N100 collection. No model ranking is computed.",
    "Two finite fixture blocks change declared stimulus features. N changes identity repetition and roster length. K=0 controls remove incident evidence.",
    "The N100 extension does not establish costly whistleblowing, resistance to peer pressure, or diffusion of responsibility.",
)


def summarize(inspected: dict, resources: dict | None = None) -> dict:
    """Pure transformation of the verifier's results, also usable by synthetic tests."""
    resources = resources or {}
    rows = []
    for original in inspected["rows"]:
        attempt, score = original["attempt"], original["score"]
        row = {key: original[key] for key in ("assignment_id", "planned_order", *DIMENSIONS)}
        row.update(status=original["status"], evidence_error=original["evidence_error"],
                   review_error=original["review_error"], attempt_id=None,
                   verified_model_observation=False, eligible=None, exposure_confirmed=None,
                   termination_kind=None, valid_close=None, report_attempt_count=None,
                   accepted_report_count=None, task_submission_count=None, task_outcome=None,
                   pending_output_count=None, final_human_output_count=None, resource=None,
                   scorer_version=None, rubric_version=None,
                   endpoints={name: {"value": None, "reason": "no_verified_attempt",
                                     "applicable": None, "conditional_denominator": False}
                              for name in ENDPOINTS})
        positive = original["variant"] == "main" and (original["K"] or 0) > 0
        for name in ("private_supported_shared_concern", "private_supported_focal_concern"):
            row["endpoints"][name]["applicable"] = positive
        row["endpoints"]["control_private_supported_concern"]["applicable"] = not positive
        for endpoint in row["endpoints"].values():
            if endpoint["applicable"] is False:
                endpoint["reason"] = "not_applicable"
        if attempt is not None:
            requests = attempt["tool_requests"]
            row.update(attempt_id=attempt["attempt_id"],
                       verified_model_observation=attempt["model_execution_confirmed"],
                       eligible=attempt["eligible"], exposure_confirmed=attempt["exposure_confirmed"],
                       termination_kind=attempt["termination_kind"],
                       valid_close=score["termination"]["valid_close"],
                       scorer_version=score.get("scorer_version"), rubric_version=score.get("rubric_version"),
                       report_attempt_count=sum(str(item.get("tool", "")).split(".")[-1]
                                                == "report_incident" for item in requests),
                       accepted_report_count=score["mechanical"]["accepted_report_count"],
                       task_submission_count=score["task"]["submission_count"],
                       task_outcome=score["task"]["outcome"],
                       pending_output_count=len(score["review_pending_output_ids"]),
                       final_human_output_count=sum(item["review_status"] == "final_human"
                                                    for item in score["outputs"]),
                       endpoints={name: score["endpoints"][name] for name in ENDPOINTS},
                       resource=resources.get(original["assignment_id"]))
        rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in DIMENSIONS)].append(row)
    cells = []
    for key, subset in groups.items():
        cell = dict(zip(DIMENSIONS, key))
        cell.update(planned=len(subset), status_counts=dict(Counter(row["status"] for row in subset)),
                    verified_model_observations=sum(row["verified_model_observation"] for row in subset),
                    valid_close_count=sum(row["valid_close"] is True for row in subset),
                    termination_counts=dict(Counter(row["termination_kind"] or "unknown" for row in subset)),
                    endpoints={})
        for name in ("report_attempt_count", "accepted_report_count", "task_submission_count",
                     "pending_output_count", "final_human_output_count"):
            known = [row[name] for row in subset if row[name] is not None]
            cell[name] = {"known_sum": sum(known), "known_assignments": len(known),
                          "unknown_assignments": len(subset) - len(known)}
        cell["task"] = aggregate_bounds([row["task_outcome"] for row in subset])
        for name in ENDPOINTS:
            values = [row["endpoints"][name] for row in subset]
            applicable = [item for item in values if item["applicable"] is not False]
            conditional = [item["value"] for item in applicable if item["conditional_denominator"]]
            cell["endpoints"][name] = {
                "all_assigned": aggregate_bounds([item["value"] for item in applicable]),
                "not_applicable": len(values) - len(applicable),
                "conditional_resolved": aggregate_bounds(conditional),
            }
        cells.append(cell)
    return {"kind": "verified_peer_collection_factual_summary_v1", "limitations": list(LIMITS),
            "verification": inspected["verification"], "phase_errors": inspected["phase_errors"],
            "planned_counts": dict(Counter(row["split"] for row in rows)),
            "status_counts_by_split": {split: dict(Counter(row["status"] for row in rows
                                                           if row["split"] == split))
                                       for split in ("smoke", "collection")},
            "rows": rows, "cells": cells}


def verified_resources(directory: Path, inspected: dict) -> dict:
    """Attach only payloads whose hashes match the independently verified attempt."""
    resources = {}
    for row in inspected["rows"]:
        attempt = row["attempt"]
        if attempt is None:
            continue
        path = safe_child(directory, f"live-{row['split']}/attempts/{attempt['attempt_id']}/attempt.json")
        payload = {key: value for key, value in read_sealed(path).items() if key != "seal_hash"}
        if content_hash(payload) != attempt["source_attempt_hash"]:
            raise ValueError("archive changed after verification; use a closed snapshot")
        archived = attempt_summary(payload)
        resources[row["assignment_id"]] = {key: archived[key] for key in (
            "usage_total_tokens", "usage_settlement", "observed_total_tokens", "elapsed_seconds",
            "drain_elapsed_seconds", "tool_request_count", "collection_stop_reasons", "cleanup_confirmed")}
    return resources


def _csv_rows(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        flat = {key: value for key, value in row.items() if key not in {"resource", "endpoints"}}
        for name, endpoint in row["endpoints"].items():
            flat[name] = endpoint["value"]
            flat[name + "_reason"] = endpoint["reason"]
            flat[name + "_applicable"] = endpoint["applicable"]
            flat[name + "_conditional_denominator"] = endpoint["conditional_denominator"]
        for name in ("usage_total_tokens", "usage_settlement", "observed_total_tokens", "elapsed_seconds",
                     "drain_elapsed_seconds", "tool_request_count", "collection_stop_reasons", "cleanup_confirmed"):
            value = (row["resource"] or {}).get(name)
            flat[name] = json.dumps(value) if isinstance(value, (list, dict)) else value
        result.append(flat)
    return result


def write_summary(data: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    flat = _csv_rows(data["rows"])
    with (output / "assignments.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    columns = ("assignment_id", *DIMENSIONS, "status", "report_attempt_count", "accepted_report_count", "task_outcome",
               "termination_kind", "pending_output_count", "private_supported_shared_concern",
               "private_supported_focal_concern", "any_output_unsupported_material_claim",
               "usage_total_tokens", "usage_settlement", "elapsed_seconds", "evidence_error", "review_error")
    def esc(value):
        return html.escape("unknown" if value is None else str(value), quote=True)
    body = "<!doctype html><html lang='en'><meta charset='utf-8'><title>Peer collection observations</title>"
    body += "<style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse;font-size:13px}"
    body += "th,td{padding:.5rem;border:1px solid #bbb}th{background:#eee}pre{white-space:pre-wrap}</style>"
    body += "<h1>Peer collection observations</h1><p>Researcher summary. Unknown values are not zero. "
    body += "Blank CSV fields mean unknown or unavailable; endpoint applicability has its own column.</p><ul>"
    body += "".join("<li>" + esc(item) + "</li>" for item in data["limitations"]) + "</ul>"
    body += "<p><a href='summary.json'>Full JSON, cell bounds and resources</a> | "
    body += "<a href='assignments.csv'>All assignment rows in CSV</a></p>"
    for split in ("smoke", "collection"):
        body += f"<h2>{esc(split)} ({data['planned_counts'].get(split, 0)} planned)</h2>"
        body += "<p>" + esc(data["status_counts_by_split"][split]) + "</p><div style='overflow:auto'><table>"
        body += "<thead><tr>" + "".join("<th>" + esc(key) + "</th>" for key in columns) + "</tr></thead><tbody>"
        for row in flat:
            if row["split"] == split:
                body += "<tr>" + "".join("<td>" + esc(row[key]) + "</td>" for key in columns) + "</tr>"
        body += "</tbody></table></div>"
    body += "<h2>Verification and phase errors</h2><pre>" + esc(json.dumps(
        {key: data[key] for key in ("verification", "phase_errors")}, indent=2)) + "</pre></html>"
    (output / "index.html").write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, help="Private JSON mapping assignment IDs to bound review payloads")
    parser.add_argument("--bindings", type=Path, help="Private JSON mapping assignment IDs to review bindings")
    args = parser.parse_args(argv)
    source, output = args.collection.resolve(), args.output.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("output must be outside the source collection")
    if output.exists():
        parser.error("output must be a new directory")
    if bool(args.reviews) != bool(args.bindings):
        parser.error("reviews and bindings must be supplied together")
    inspected = inspect_live_collection(source,
        reviews_by_assignment=json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else None,
        bindings_by_assignment=json.loads(args.bindings.read_text(encoding="utf-8")) if args.bindings else None)
    data = summarize(inspected, verified_resources(source, inspected))
    write_summary(data, output)
    print(json.dumps({"output": str(output), "planned_counts": data["planned_counts"],
                      "status_counts_by_split": data["status_counts_by_split"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
