"""Render verified engineering compatibility evidence for the private review site."""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from swarm_auth_bench.peer_reporting.export import _json, _page
from swarm_auth_bench.peer_reporting.live import PLAN_FILE, verify_phase
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal


def export(directory: Path, output: Path) -> dict:
    report = verify_phase(directory)
    plan = read_sealed(directory / PLAN_FILE)
    if report["phase"] != "compatibility":
        raise ValueError("this exporter accepts only the separate engineering phase")
    output.mkdir(parents=True, exist_ok=False)
    fixture = read_sealed(directory / plan["fixture_path"])
    rows = []
    for row in report["entries"]:
        link = ""
        if row["status"] == "archived":
            archive = read_sealed(directory / "attempts" / row["attempt_id"] / "attempt.json")
            result = archive["observer_result"] or {}
            state = result.get("world_state") or {}
            sections = [
                ("Engineering checks", archive["check"]),
                ("Observed resources", {key: result.get(key) for key in
                                        ("elapsed_seconds", "drain_elapsed_seconds", "usage")}),
                ("Exact engineering instructions", plan["instructions"]),
                ("Prepared public packet and receipt", {
                    "public_packet": fixture["public"], "initial_receipt": result.get("initial_receipt")}),
                ("Tool requests and responses", result.get("tool_requests", [])),
                ("Verified tool receipts", result.get("tool_receipts", [])),
                ("Public room posts", state.get("messages", [])),
                ("Stored private reports", state.get("reports", [])),
                ("Task submissions", state.get("task_answers", [])),
                ("Ordinary observer outputs", result.get("observer_outputs", [])),
                ("Termination and failures", {key: result.get(key) for key in
                    ("termination_kind", "boundary", "queue_reconciled", "infrastructure_failures")}),
                ("Provider error records", [event.get("raw", {}).get("params", {}).get("error")
                                            for event in result.get("raw_events", [])
                                            if event.get("method") == "error"]),
                ("Input and runtime bindings", {key: result.get(key) for key in
                    ("adapter_version", "requested_model", "reasoning_effort", "served_model_snapshot",
                     "instructions_hash", "instructions_and_roles_hash", "packet_sha256",
                     "tool_manifest_hash", "wire_tool_specs_hash", "preflight")}),
            ]
            body = ("<p><a href='index.html'>All compatibility calls</a></p>"
                    f"<h1>{html.escape(row['model'])}</h1>"
                    "<p class='notice'>This call was told to exercise all six tools. Its private report "
                    "was an interface check. It does not measure willingness to report an incident.</p>")
            for title, value in sections:
                body += f"<h2>{html.escape(title)}</h2><pre>{_json(value)}</pre>"
            name = row["model"] + ".html"
            (output / name).write_text(_page(row["model"] + " compatibility evidence", body), encoding="utf-8")
            link = f"<a href='{name}'>Inspect call</a>"
        usage = row["usage_total_tokens"]
        elapsed = row["elapsed_seconds"]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            row["model"], row["status"], row["classification"] or "Unrun",
            "Unknown" if usage is None else f"{usage:,}",
            "Unknown" if elapsed is None else f"{elapsed:.2f} s", row["tool_request_count"]
        )) + f"<td>{link}</td></tr>")
    body = ("<p><a href='../implementation.html'>Implementation status</a> · "
            "<a href='../source-review.html'>Village source review</a></p>"
            "<h1>Bounded compatibility checks</h1>"
            "<p class='notice'>Three engineering assignments: one per model. These checks test access, "
            "packet delivery, tool use, and resource records. The nine smoke and 216 collection assignments "
            "remain separate.</p>"
            f"<p>Call starts: {report['live_model_call_starts']} / {report['maximum_live_calls']}. "
            f"Qualified models: {html.escape(', '.join(report['qualified_models']) or 'None')}.</p>"
            "<div class='scroll'><table><thead><tr><th>Model</th><th>Status</th><th>Check</th>"
            "<th>Reported tokens</th><th>Elapsed</th><th>Requests</th><th>Evidence</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
            f"<h2>Frozen limits</h2><pre>{_json(plan['caps'])}</pre>"
            f"<h2>Usage ledger</h2><pre>{_json(report['ledger'])}</pre>"
            f"<h2>Phase stop</h2><pre>{_json(report['halted'])}</pre>"
            "<p>Token values are reported usage. No hard provider billing limit is verified. "
            "Model IDs are requested aliases; served model weights are not pinned.</p>"
            f"<h2>Source context</h2><pre>{_json(fixture['provenance'])}</pre>"
            f"<p>Sealed plan: <code>{html.escape(report['plan_hash'])}</code></p>")
    (output / "index.html").write_text(_page("Peer reporting compatibility checks", body), encoding="utf-8")
    atomic_json(output / "resource-observations.json", seal({
        "phase": "compatibility", "plan_hash": report["plan_hash"], "caps": plan["caps"],
        "model_call_starts": report["live_model_call_starts"], "entries": report["entries"],
        "report_propensity_measured": False, "ledger": report["ledger"],
    }))
    return {"output": str(output), "call_starts": report["live_model_call_starts"],
            "qualified_models": report["qualified_models"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(export(args.directory, args.output))


if __name__ == "__main__":
    main()
