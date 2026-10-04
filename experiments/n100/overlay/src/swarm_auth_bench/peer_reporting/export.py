"""Escape all evidence text and quarantine individual corrupt attempt archives."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

from ..events import content_hash
from .collection import verify_collection
from .runner import verify_attempt
from .score import score_attempt
from .storage import atomic_json, read_sealed, seal

STYLE = """
:root{font-family:system-ui,sans-serif;color:#183043;background:#f7fafc}body{margin:0}
main{max-width:1280px;margin:auto;padding:24px}h1{font-size:2rem}h2{margin-top:32px}
p,li{line-height:1.6}a{color:#075e96}code{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;min-width:900px}
td,th{padding:12px;text-align:left;border-bottom:1px solid #cad7e2;vertical-align:top}
th{background:#e9f0f6}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;
background:#eaf1f6;padding:16px;border-radius:6px;font:13px/1.6 ui-monospace,monospace}
.notice{border-left:4px solid #ad740d;padding:8px 18px;background:#fff6e5}.muted{color:#566778}
select{font:inherit;min-height:44px;padding:8px;margin:8px}label{display:inline-block}
@media(max-width:600px){main{padding:16px}h1{font-size:1.6rem}}
"""


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><main>{body}</main></body></html>")


def _json(value) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2))


def inspect_collection(directory: Path) -> dict:
    verification = verify_collection(directory)
    manifest = read_sealed(directory / "collection-manifest.json")
    index = read_sealed(directory / "collection-index.json")
    rows = []
    for assignment in manifest["assignments"]:
        row = {key: assignment[key] for key in (
            "assignment_id", "split", "N", "K", "block", "variant", "model", "prompt_condition",
        )}
        entry = index["assignments"][row["assignment_id"]]
        row.update(status=entry["status"], offline_examples=[], model_observation=False,
                   private_shared_outcome=None, private_focal_outcome=None)
        for reference in entry["offline_examples"]:
            try:
                if not isinstance(reference, dict):
                    raise ValueError("offline example reference must be an object")
                attempt = verify_attempt(directory, reference)
                if attempt["assignment_id"] != row["assignment_id"]:
                    raise ValueError("attempt belongs to another assignment")
                score = score_attempt(attempt)
                row["offline_examples"].append({"status": "verified", "reference": reference,
                                        "attempt": attempt, "score": score})
            except (OSError, ValueError, KeyError, TypeError) as error:
                row["offline_examples"].append({"status": "quarantined", "reason": str(error),
                                        "attempt_id": reference.get("attempt_id")
                                        if isinstance(reference, dict) else None})
        rows.append(row)
    return {"verification": verification, "rows": rows, "live_model_observations": 0,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "offline_example_count": sum(len(row["offline_examples"]) for row in rows),
        "offline_status_counts": dict(Counter(example["status"] for row in rows
                                               for example in row["offline_examples"]))}


def export_collection(directory: Path, output: Path) -> dict:
    directory, output = Path(directory), Path(output)
    data = inspect_collection(directory)
    output.mkdir(parents=True, exist_ok=False)
    table_rows = []
    public_rows = []
    example_links = []
    for row in data["rows"]:
        links = []
        for item in row["offline_examples"]:
            if item["status"] != "verified":
                links.append("Quarantined: " + html.escape(item["reason"]))
                continue
            attempt, score = item["attempt"], item["score"]
            name = content_hash(attempt["attempt_id"]) + ".html"
            body = ("<p><a href='index.html'>All planned assignments</a></p><h1>Authored offline replay</h1>"
                    "<p class='notice'>This trace was written by software from an authored script. "
                    "It contains no model response and no live inference. Semantic labels await review.</p>"
                    f"<p>Termination: {html.escape(attempt['termination_kind'])}</p>")
            for heading, value in [
                ("Public room and trusted records", attempt["fixture"]["public"]),
                ("Accepted private reports", attempt["accepted_reports"]),
                ("Observer messages and final answer", attempt["observer_outputs"]),
                ("Requested scripted peer replies", attempt["observed_peer_messages"]),
                ("Task submissions", attempt["task_submissions"]),
                ("Mechanical scores and pending semantic labels", score),
                ("Tool request and storage trace", attempt["tool_requests"]),
                ("Controller truth, withheld during the trial", attempt["fixture"]["truth"]),
            ]:
                body += f"<h2>{heading}</h2><pre>{_json(value)}</pre>"
            (output / name).write_text(_page("Authored offline replay", body), encoding="utf-8")
            links.append(f"<a href='{name}'>Inspect offline trace</a>")
            example_links.append(f"<a href='{name}'>Open authored example {len(example_links) + 1}</a>")
        values = [row["split"], row["N"], row["K"] if row["K"] is not None else "control",
                  row["block"], row["variant"], row["model"], row["prompt_condition"], row["status"],
                  "Unrun / unknown", "Unrun / unknown"]
        table_rows.append("<tr data-model='" + html.escape(row["model"], quote=True) + "' data-status='"
                          + html.escape(row["status"], quote=True) + "'>"
                          + "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
                          + "<td>" + ("<br>".join(links) or "No execution") + "</td></tr>")
        public_rows.append({key: value for key, value in row.items() if key != "offline_examples"})
    filters = ""
    for key in ("model", "status"):
        values = sorted({row[key] for row in data["rows"]})
        filters += f"<label>{key.title()} <select id='{key}'><option value=''>All</option>" + "".join(
            f"<option>{html.escape(value)}</option>" for value in values) + "</select></label>"
    body = ("<h1>Peer-reporting offline implementation</h1>"
            "<p class='notice'>54 collection assignments and 9 separate smoke assignments are planned. "
            "No live benchmark model has run. Authored replays test the software only.</p>"
            "<p>The shared-incident and fixed-operation columns below refer to model observations. "
            "Offline examples do not fill these columns. Corrupt examples have a quarantine notice.</p>"
            f"<p>Assignment status counts: {html.escape(str(data['status_counts']))}. "
            f"Authored offline examples: {data['offline_example_count']}.</p>"
            + ("<p>" + " · ".join(example_links) + "</p>" if example_links else "")
            + f"<details><summary>Collection readiness</summary><pre>{_json(data['verification'])}</pre></details>"
            + filters + "<div class='scroll'><table><thead><tr>"
            + "".join(f"<th>{column}</th>" for column in ["Split", "N", "K", "Block", "Variant", "Target model",
                                                        "Prompt", "Status", "Shared incident", "Fixed operation",
                                                        "Evidence"])
            + "</tr></thead><tbody>" + "".join(table_rows) + "</tbody></table></div>"
            "<script>const filters=['model','status']; function filterRows(){"
            "document.querySelectorAll('tbody tr').forEach(row=>{row.hidden=filters.some(key=>{"
            "const value=document.getElementById(key).value; return value&&row.dataset[key]!==value;});});}"
            "filters.forEach(key=>document.getElementById(key).addEventListener('change',filterRows));</script>")
    (output / "index.html").write_text(_page("Peer-reporting offline implementation", body), encoding="utf-8")
    atomic_json(output / "index.json", seal({"kind": "offline_implementation_preview",
                                            "live_model_observations": 0, "assignments": public_rows}))
    return {"output": str(output), "assignments": len(public_rows), "live_model_observations": 0,
            "status_counts": data["status_counts"], "offline_example_count": data["offline_example_count"],
            "offline_status_counts": data["offline_status_counts"]}
