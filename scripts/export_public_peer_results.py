"""Project private factual exports into a new public demo tree, without inference.

Reviewer packets are inputs only. Their opaque IDs, hashes, templates and source
filenames are never published. Public evidence is an independently named,
redacted researcher copy and cannot be used as a bound blinded review packet.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from swarm_auth_bench.peer_reporting.storage import read_sealed

ENDPOINTS = (
    "private_supported_shared_concern", "private_supported_focal_concern", "private_unsupported_material_claim",
    "any_output_unsupported_material_claim", "control_private_supported_concern", "response_fully_accurate",
)
ROW_FIELDS = (
    "assignment_id", "planned_order", "split", "model", "prompt_condition", "N", "K", "block", "variant",
    "status", "verified_model_observation", "eligible", "exposure_confirmed", "termination_kind", "valid_close",
    "report_attempt_count", "accepted_report_count", "task_submission_count", "task_outcome", "pending_output_count",
    "final_human_output_count", "scorer_version", "rubric_version",
)
RESOURCE_FIELDS = (
    "usage_total_tokens", "usage_settlement", "observed_total_tokens", "elapsed_seconds", "drain_elapsed_seconds",
    "tool_request_count", "cleanup_confirmed",
)
CONTEXT_FIELDS = ("addressed_to", "recipient", "reply_to", "sequence", "delivery_status")
REPORT_FIELDS = (
    "request_id", "incident_ref", "actor_ids", "action", "action_stage", "authorization_state", "certainty",
    "evidence_ids", "summary",
)
FORBIDDEN_KEYS = frozenset({
    "truth", "violating_actor_ids", "verified_violator_count", "participant_count", "curve_K", "focal_operation_id",
    "expected_task", "unverified_claim", "monitoring_condition", "prompt_condition", "review_bindings",
    "label_template", "controller_checkpoint", "world_checkpoint", "raw_events", "runtime_metadata",
    "thread_id", "threadId", "turn_id", "turnId", "auth_file", "credentials", "api_key", "access_token",
    "refresh_token", "authorization_header", "private_key",
})
REDACTIONS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("review_id", re.compile(r"(?:output|ref|packet)-[0-9a-fA-F]{32}")),
    ("provider_id", re.compile(r"\b(?:msg_[a-zA-Z0-9_-]{12,}|(?:thread|turn|call|item)[_-][a-zA-Z0-9_-]{12,})\b")),
    ("opaque_identifier", re.compile(r"\b(?:[0-9a-fA-F]{32,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b")),
    ("url", re.compile(r"\b(?:https?|wss?|file)://[^\s\"'<>]+", re.I)),
    ("windows_path", re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\r\n\"'<>]+")),
    ("unix_path", re.compile(r"(?<![\w/])/(?:home|root|tmp|var|mnt|workspace|research|Users|opt)(?:/[^\s\"'<>]*)?")),
    ("network_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")),
    ("private_hostname", re.compile(r"\b[a-zA-Z0-9_.-]+\.(?:local|ts\.net|internal)(?::\d+)?\b", re.I)),
    ("localhost", re.compile(r"\blocalhost(?::\d+)?\b", re.I)),
    ("secret", re.compile(r"\b(?:sk-[a-zA-Z0-9_-]{12,}|Bearer\s+[a-zA-Z0-9._-]{12,})\b", re.I)),
    ("service_token", re.compile(r"\b(?:hf_[a-zA-Z0-9]{12,}|gh[pousr]_[a-zA-Z0-9]{12,}|github_pat_[a-zA-Z0-9_]{12,})\b")),
    ("email", re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")),
    ("secret_assignment", re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|credential)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+", re.I)),
)


class PublicRedactor:
    """Consistent assignment-local replacements; mapping is never written."""

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self.counts: Counter = Counter()

    def text(self, text: str) -> str:
        for category, pattern in REDACTIONS:
            def replace(match: re.Match, category: str = category) -> str:
                original = match.group()
                if original not in self.mapping:
                    self.mapping[original] = f"public-redaction-{len(self.mapping) + 1:04d}"
                self.counts[category] += 1
                return self.mapping[original]
            text = pattern.sub(replace, text)
        return text

    def value(self, value: Any) -> Any:
        if type(value) is str:
            return self.text(value)
        if type(value) is list:
            return [self.value(item) for item in value]
        if type(value) is dict:
            if set(value) & FORBIDDEN_KEYS:
                raise ValueError("delivered public evidence contains a forbidden controller or credential field")
            return {self.text(key): self.value(item) for key, item in value.items()}
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise ValueError("public projection requires finite JSON values")


def _enum(value: Any, allowed: tuple, field: str) -> Any:
    if value not in allowed:
        raise ValueError(f"unexpected public {field}")
    return value


def _numeric(value: Any, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
        raise ValueError("public count/resource must be a finite nonnegative number")
    return value


def project_row(row: dict) -> dict:
    public = {key: row[key] for key in ROW_FIELDS}
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", public["assignment_id"]):
        raise ValueError("invalid benchmark assignment ID")
    for key, allowed in (
        ("split", ("collection", "smoke")), ("model", ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")),
        ("prompt_condition", ("none", "soft", "active")),
        ("variant", ("main", "matching_principal_approval", "unverified_accusation")),
        ("status", ("archived",)), ("termination_kind", ("natural_end", "per_trial_limit", "collection_forced_truncation", "infrastructure_incomplete")),
        ("scorer_version", ("peer-reporting-score-v4",)), ("rubric_version", ("peer-reporting-rubric-v2",)),
    ):
        _enum(public[key], allowed, key)
    for key in ("planned_order", "N", "K", "block", "report_attempt_count", "accepted_report_count",
                "task_submission_count", "pending_output_count", "final_human_output_count"):
        _numeric(public[key], nullable=key == "K")
    for key in ("verified_model_observation", "eligible", "exposure_confirmed", "valid_close", "task_outcome"):
        if public[key] is not None and type(public[key]) is not bool:
            raise ValueError("public factual flags must be booleans or null")
    public["evidence_error_present"] = row.get("evidence_error") is not None
    public["review_error_present"] = row.get("review_error") is not None
    resource = row["resource"]
    public["resource"] = {key: resource[key] for key in RESOURCE_FIELDS}
    _enum(resource["usage_settlement"], ("settled", "unresolved"), "usage settlement")
    for key in RESOURCE_FIELDS:
        if key not in ("usage_settlement", "cleanup_confirmed"):
            _numeric(resource[key], nullable=key == "usage_total_tokens")
    if type(resource["cleanup_confirmed"]) is not bool:
        raise ValueError("cleanup flag must be boolean")
    public["resource"]["collection_stop_reasons"] = [
        _enum(reason, ("collection_wall_limit", "collection_token_limit", "clock_regression"), "stop reason")
        for reason in resource["collection_stop_reasons"]]
    public["endpoints"] = {}
    for name in ENDPOINTS:
        endpoint = row["endpoints"][name]
        projected = {key: endpoint[key] for key in ("applicable", "value", "reason", "conditional_denominator")}
        for key in ("applicable", "conditional_denominator"):
            if type(projected[key]) is not bool:
                raise ValueError("endpoint flags must be booleans")
        if projected["value"] is not None and type(projected["value"]) is not bool:
            raise ValueError("endpoint value must be boolean or unknown")
        _enum(projected["reason"], ("not_applicable", "review_pending", "valid_close_all_outputs_reviewed_negative",
                                  "reviewed_qualifying_output", "ineligible_configuration", "exposure_unverified",
                                  "collection_forced_truncation", "infrastructure_incomplete"), "endpoint reason")
        public["endpoints"][name] = projected
    return public


def project_evidence(packet: dict, assignment_id: str) -> dict:
    redactor = PublicRedactor()
    # Allocate public output IDs before inspecting content, preserving references
    # across public messages, reports, and stored replies without exposing reviews.
    for index, output in enumerate(packet["outputs"], 1):
        redactor.mapping[output["review_output_id"]] = f"public-output-{index:04d}"
    delivered = redactor.value(json.loads(packet["delivered_packet"]))
    public = {"kind": "redacted_public_peer_evidence_v1", "assignment_id": assignment_id,
              "review_use": "Researcher-facing redacted copy. Not a blinded or bound review packet.",
              "common_instructions": redactor.text(packet["common_instructions"]),
              "delivered_packet": json.dumps(delivered, ensure_ascii=False, indent=2),
              "outputs": [], "observed_peer_messages": []}
    for output in packet["outputs"]:
        source = _enum(output["source_kind"], ("private_report", "observer_message", "final_response", "finish_summary"), "output kind")
        projected = {"output_id": redactor.mapping[output["review_output_id"]], "source_kind": source,
                     "audience": redactor.text(output["audience"]),
                     "context": redactor.value({key: output["context"].get(key) for key in CONTEXT_FIELDS})}
        if source == "private_report":
            projected["payload"] = redactor.value({key: output["payload"][key] for key in REPORT_FIELDS})
        else:
            projected["text"] = redactor.text(output["text"])
        public["outputs"].append(projected)
    for message in packet["observed_peer_messages"]:
        public["observed_peer_messages"].append(redactor.value({key: message.get(key) for key in (
            "event_id", "source_kind", "sender_id", "audience", "addressed_to", "recipient", "reply_to", "sequence",
            "delivery_status", "text")}))
    public["redactions"] = {"counts_by_category": dict(sorted(redactor.counts.items())),
                            "caveat": "Identifiers and detected paths, addresses, and credential-like strings are replaced consistently within this assignment. Content is redacted, not verbatim. No replacement mapping is published."}
    return public


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:auto;padding:24px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse}}td,th{{padding:8px;border:1px solid #ccc}}</style>" + body + "</html>")


def project_mechanical_audit(audit: dict, summary_path: Path, rows: list[dict]) -> dict:
    if (audit.get("status") != "mechanical_counts_confirmed" or audit.get("blockers")
            or audit.get("human_semantic_validation") is not False
            or audit.get("provenance", {}).get("summary_sha256") != hashlib.sha256(summary_path.read_bytes()).hexdigest()):
        raise ValueError("mechanical audit must be confirmed, nonsemantic, and bound to the exact summary")
    difference = {key: _numeric(audit["collection_difference_explanation"][key]) for key in (
        "report_requests", "unique_accepted_reports", "difference", "captured_schema_errors", "other_rejections",
        "duplicate_stored_receipts", "action_length_failures", "nonstring_summary_failures")}
    collection = [row for row in rows if row["split"] == "collection"]
    if (difference["report_requests"] != sum(row["report_attempt_count"] for row in collection)
            or difference["unique_accepted_reports"] != sum(row["accepted_report_count"] for row in collection)
            or difference["difference"] != difference["report_requests"] - difference["unique_accepted_reports"]):
        raise ValueError("mechanical audit totals differ from factual rows")
    schema = {split: {name: _numeric(audit["schema_failure_counts_by_split"][split].get(name, 0))
                      for name in ("action_exceeds_100_characters", "summary_is_not_string")}
              for split in ("collection", "smoke")}
    groups = []
    for source in audit["groups"]:
        group = {key: source[key] for key in ("split", "group", "assignments", "attempted_assignments",
                                              "accepted_assignments", "report_requests", "accepted_reports")}
        _enum(group["split"], ("collection", "smoke"), "audit split")
        _enum(group["group"], ("positive_main", "zero_violation_main", "unverified_accusation", "matching_principal_approval"), "audit group")
        for key in ("assignments", "attempted_assignments", "accepted_assignments", "report_requests", "accepted_reports"):
            _numeric(group[key])
        groups.append(group)
    rejected = audit["six_positive_assignments_without_accepted_reports"]
    return {"kind": "mechanical_schema_audit_aggregates_v1", "human_semantic_validation": False,
            "groups": groups, "schema_failure_counts_by_split": schema, "collection_difference_explanation": difference,
            "positive_assignments_without_accepted_reports": {"assignments": len(rejected),
                "report_requests": sum(_numeric(row["report_attempt_count"]) for row in rejected),
                "action_length_failures": sum(_numeric(row["schema_failure_counts"].get("action_exceeds_100_characters", 0)) for row in rejected)},
            "interpretation": "Schema rejection counts describe tool admission, not detection quality or semantic accuracy. No rejected-call references or private archive paths are published."}


def export_public_results(summary_path: Path, collection_export: Path, output: Path,
                          mechanical_audit: Path | None = None) -> dict:
    summary_path, collection_export, output = map(Path, (summary_path, collection_export, output))
    if output.exists():
        raise ValueError("public projection output must be a new directory")
    for source in (summary_path.parent, collection_export):
        if output.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(output.resolve()):
            raise ValueError("public projection must be outside source exports")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("kind") != "verified_peer_collection_factual_summary_v1" or summary.get("phase_errors"):
        raise ValueError("expected complete verified factual summary without phase errors")
    index = read_sealed(collection_export / "index.json")
    if index.get("kind") != "live_evidence_review_export" or index.get("phase_errors"):
        raise ValueError("expected verified live evidence export")
    rows = [project_row(row) for row in summary["rows"]]
    assignments = {row["assignment_id"]: row for row in index["rows"]}
    if len(assignments) != len(rows) or {row["assignment_id"] for row in rows} != set(assignments):
        raise ValueError("summary and evidence assignments differ or repeat")
    # Validate all reviewer inputs before creating any publication output.
    evidence = []
    for row in rows:
        private = assignments[row["assignment_id"]]
        relative = private.get("review_packet", "")
        if not re.fullmatch(r"reviewer/packet-[0-9a-f]{32}\.json", relative):
            raise ValueError("reviewer input must name exactly a masked reviewer packet")
        packet = read_sealed(collection_export / relative)
        evidence.append(project_evidence(packet, row["assignment_id"]))
    limitations = [
        "Mechanical report calls and accepted reports do not establish justified reporting.",
        "Semantic labels await explicit bound human judgments. Unknown labels remain unknown.",
        "Smoke is separate from collection. No semantic model ranking is computed.",
        "Unresolved provider usage stays unknown; retained capacity is not actual billing usage.",
        "Public evidence is a redacted researcher copy with independent IDs, not a blinded review packet.",
        "The original frozen manifest's static live_ready/pending metadata is omitted here; archived execution facts are reported separately.",
    ]
    public_summary = {"kind": summary["kind"], "publication_kind": "redacted_public_peer_results_v1",
                      "limitations": limitations, "planned_counts": {split: sum(r["split"] == split for r in rows) for split in ("collection", "smoke")},
                      "status_counts_by_split": {split: dict(Counter(r["status"] for r in rows if r["split"] == split)) for split in ("collection", "smoke")},
                      "execution_audit": {"archived_assignments": len(rows), "verified_model_observations": sum(r["verified_model_observation"] is True for r in rows),
                                          "cleanup_confirmed_assignments": sum(r["resource"]["cleanup_confirmed"] is True for r in rows),
                                          "unresolved_usage_assignments": sum(r["resource"]["usage_settlement"] == "unresolved" for r in rows),
                                          "final_human_output_count": sum(r["final_human_output_count"] for r in rows)},
                      "rows": rows}
    if mechanical_audit is not None:
        public_summary["mechanical_audit"] = project_mechanical_audit(
            json.loads(Path(mechanical_audit).read_text(encoding="utf-8")), summary_path, rows)
    output.mkdir(parents=True)
    _write_json(output / "results-v1/summary.json", public_summary)
    if "mechanical_audit" in public_summary:
        _write_json(output / "results-v1/mechanical-audit.json", public_summary["mechanical_audit"])
    public_index = []
    for number, (row, record) in enumerate(zip(rows, evidence, strict=True), 1):
        name = f"assignment-{number:04d}"
        _write_json(output / f"collection-v1/evidence/{name}.json", record)
        body = ("<p><a href='../index.html'>All assignments</a></p><h1>Public assignment evidence</h1>"
                f"<p>Model: {html.escape(row['model'])}. Monitoring: {html.escape(row['prompt_condition'])}. N={row['N']}, K={row['K']}.</p>"
                "<p>These are stored actions and statements. Human semantic judgments remain pending. This redacted researcher copy cannot be used for blinded review.</p>"
                f"<p><a href='{name}.json'>Download the exact public evidence JSON</a></p>"
                f"<details open><summary>Common policy</summary><pre>{html.escape(record['common_instructions'])}</pre></details>"
                f"<details open><summary>Delivered evidence</summary><pre>{html.escape(record['delivered_packet'])}</pre></details>"
                "<details open><summary>Observer outputs and stored private reports</summary>")
        for item in record["outputs"]:
            body += (f"<article><h2>{html.escape(item['source_kind'])}: {html.escape(item['output_id'])}</h2>"
                     f"<pre>{html.escape(json.dumps(item['context'], ensure_ascii=False, indent=2))}</pre>"
                     f"<pre>{html.escape(json.dumps(item['payload'], ensure_ascii=False, indent=2) if 'payload' in item else item['text'])}</pre></article>")
        body += "</details><details><summary>Stored peer replies (evidence only)</summary>"
        for item in record["observed_peer_messages"]:
            metadata = {key: value for key, value in item.items() if key != "text"}
            body += (f"<article><pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre>"
                     f"<pre>{html.escape(item['text'])}</pre></article>")
        if not record["observed_peer_messages"]:
            body += "<p>No stored peer replies in this exported evidence.</p>"
        body += f"</details><details><summary>Redactions</summary><pre>{html.escape(json.dumps(record['redactions'], indent=2))}</pre></details>"
        (output / f"collection-v1/evidence/{name}.html").write_text(_page("Public assignment evidence", body), encoding="utf-8")
        public_index.append({**{key: row[key] for key in ("assignment_id", "planned_order", "split", "model", "prompt_condition", "N", "K", "block", "variant", "status")},
                             "evidence_page": f"evidence/{name}.html", "evidence_json": f"evidence/{name}.json"})
    _write_json(output / "collection-v1/index.json", {"kind": "live_evidence_review_export", "publication_kind": "redacted_researcher_projection_v1", "rows": public_index,
                                                     "planned_counts": public_summary["planned_counts"], "status_counts": dict(Counter(r["status"] for r in rows))})
    table = "".join(f"<tr><td>{html.escape(r['split'])}</td><td>{html.escape(r['model'])}</td><td>{html.escape(r['prompt_condition'])}</td><td>{r['N']}</td><td>{r['K']}</td><td><a href='{r['evidence_page']}'>Evidence</a></td></tr>" for r in public_index)
    (output / "collection-v1/index.html").write_text(_page("Public assignment evidence", "<h1>Public assignment evidence</h1><p>Derived researcher copy. Human semantic review is pending. No reviewer/controller maps are published.</p><table><thead><tr><th>Split</th><th>Model</th><th>Prompt</th><th>N</th><th>K</th><th>Evidence</th></tr></thead><tbody>" + table + "</tbody></table>"), encoding="utf-8")
    (output / "results-v1/index.html").write_text(_page("Public factual results", "<h1>Public factual results</h1><p>Mechanical results only. Human semantic review is pending.</p><p><a href='../collection-v1/index.html'>Assignment evidence</a> | <a href='summary.json'>Derived summary JSON</a></p><pre>" + html.escape(json.dumps({key: value for key, value in public_summary.items() if key != "rows"}, indent=2)) + "</pre>"), encoding="utf-8")
    with (output / "results-v1/assignments.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows({key: row[key] for key in ROW_FIELDS} for row in rows)
    files = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(output.rglob("*")) if path.is_file()]
    manifest = {"kind": "public_peer_results_projection_manifest_v1", "file_count": len(files), "files": files,
                "assignment_count": len(rows), "reviewer_packets_included": False, "controller_bindings_included": False,
                "original_review_identifiers_included": False, "provider_calls": 0,
                "redaction_counts_by_category": dict(sum((Counter(item["redactions"]["counts_by_category"]) for item in evidence), Counter()))}
    _write_json(output / "projection-manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--collection-export", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mechanical-audit", type=Path)
    args = parser.parse_args()
    try:
        manifest = export_public_results(args.summary, args.collection_export, args.output, args.mechanical_audit)
        print(json.dumps({"assignment_count": manifest["assignment_count"], "file_count": manifest["file_count"], "provider_calls": 0}))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
