"""Read retained live evidence into scoring and separately masked review artifacts.

No model, repair, resume, or admission operation is performed. Source artifacts
are never rewritten. The phase reader may acquire its existing budget file lock.
Researcher exports and private bindings must not be given to blinded reviewers;
only files inside the export's ``reviewer`` directory are reviewer packets.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from ..events import content_hash
from ..long_events import iter_events
from . import live
from .collection import verify_collection
from .config import MODELS, StudyConfig
from .export import _page
from .live_archive import verify_archived_index
from .prompts import MONITORING_BLOCKS
from .score import build_review_bundle, score_attempt
from .storage import atomic_json, read_sealed, safe_child, seal
from .world import audit_state

ADAPTER_VERSION = "peer-reporting-live-review-v1"
_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError)
_ROW_KEYS = ("assignment_id", "split", "N", "K", "block", "variant", "model", "prompt_condition",
             "planned_order", "round")


def _plain(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "seal_hash"}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _shared_phase(directory: Path, manifest: dict, split: str):
    state = live._PhaseState(directory / f"live-{split}")
    try:
        caps = StudyConfig.from_dict(manifest["config"]).caps_for(split)
        expected = live.build_collection_phase_plan(directory, split, caps, state.plan["gate_evidence"])
        _require(live._comparable(state.plan) == live._comparable(expected),
                 "live phase does not match the sealed collection assignments and caps")
        state.verify_budget_history()
        starts = state.journal.of_kind("attempt_started")
        entries = {entry["attempt_id"]: entry for entry in state.plan["planned_order"]}
        _require(len(starts) <= state.plan["maximum_live_calls"], "phase exceeds its primary attempt limit")
        _require(len({record["data"]["attempt_id"] for record in starts}) == len(starts),
                 "phase repeats a primary attempt")
        for number, record in enumerate(starts, 1):
            data = record["data"]
            entry = entries.get(data["attempt_id"])
            _require(entry is not None and data["entry_id"] == entry["entry_id"]
                     and data["model"] == entry["model"] and data["call_start_number"] == number,
                     "journaled start differs from the phase allocation")
        archived = state.journal.of_kind("attempt_archived")
        _require(len({record["data"]["attempt_id"] for record in archived}) == len(archived),
                 "phase repeats an archived primary attempt")
        _require(all(record["data"]["attempt_id"] in {start["data"]["attempt_id"] for start in starts}
                     for record in archived), "phase archive has no journaled start")
        return state
    except BaseException:
        state.journal.close()
        raise


def _normalize(payload: dict, fixture: dict, row: dict, attempt_dir: Path) -> dict:
    result = payload["observer_result"]
    base = {
        "adapter_version": ADAPTER_VERSION, "execution_kind": "live_model",
        "assignment_id": row["assignment_id"], "attempt_id": payload["attempt_id"],
        "primary": True, "attempt_number": 1, "fixture": fixture,
        "source_attempt_hash": content_hash(payload), "source_plan_hash": payload["plan_hash"],
    }
    if result is None:
        return {**base, "eligible": False, "exposure_confirmed": False,
                "termination_kind": "infrastructure_incomplete", "accepted_reports": [],
                "observer_outputs": [], "observed_peer_messages": [], "task_submissions": [],
                "tool_requests": [], "tool_receipts": [], "model_execution_confirmed": False}
    _require(type(result) is dict, "observer result is not an object")
    _require(result["attempt_id"] == payload["attempt_id"]
             and result["instructions_hash"] == content_hash(row["instructions"])
             and result["instructions_and_roles_hash"] == row["input_identity"]["instructions_and_roles_hash"]
             and result["packet_sha256"] == fixture["public_hash"],
             "live result differs from the sealed assignment inputs")
    world = audit_state(attempt_dir / "world", result["world_checkpoint"])
    _require(world == result["world_state"] and world["trial_id"] == payload["attempt_id"]
             and world["packet_sha256"] == fixture["public_hash"],
             "archived world differs from durable assignment evidence")
    checkpoint = result["controller_checkpoint"]
    events = list(iter_events(attempt_dir / "runtime-log" / "events.jsonl",
                              expected_count=checkpoint["event_count"], expected_hash=checkpoint["final_hash"]))
    retained_events = [event["data"]["adapter_event"] for event in events]
    _require(retained_events == result["events"], "archived controller events differ from durable evidence")
    _require(all(event["run_id"] == payload["attempt_id"] for event in events),
             "controller log belongs to another attempt")
    _require([event["event"] for event in retained_events if event["kind"] == "runtime_event"]
             == result["raw_events"], "raw runtime evidence differs from controller events")
    receipt = result["initial_receipt"]
    confirmed = [event["receipt"] for event in retained_events
                 if event["kind"] == "initial_packet_delivery_confirmed"]
    _require(confirmed == ([receipt] if receipt is not None else []),
             "initial receipt is not retained in the controller evidence")
    exposure = result["exposure_confirmed"] is True and world["exposure_confirmed"] is True and receipt is not None
    if receipt is not None:
        raw = receipt["raw"]["params"]
        item = raw["item"]
        _require(receipt["packet_hash"] == content_hash(fixture["packet"])
                 and receipt["raw"]["method"] == "item/completed"
                 and raw["threadId"] == receipt["thread_id"] and raw["turnId"] == receipt["turn_id"]
                 and item["id"] == receipt["item_id"] and item["type"] == "userMessage"
                 and item.get("status", "completed") == "completed"
                 and live.live_runtime._text_item(item) == fixture["packet"], "initial packet receipt mismatch")
    check = live.evaluate_transport(result, fixture=fixture, model=row["model"], attempt_id=payload["attempt_id"],
                                    preflight=payload["preflight"],
                                    orchestrator_failures=payload["orchestrator"]["evidence_failures"],
                                    observer_error=payload["observer_error"])
    _require(check == payload["check"], "archived transport classification differs from its evidence")
    eligible = all(check["checks"][key] for key in live.CONFIGURATION_CHECKS if key != "tools_registered")
    # Later infrastructure failures close the opportunity incompletely; they do
    # not erase a known positive established before that failure.
    termination = result["termination_kind"]
    if (payload["orchestrator"]["evidence_failures"] or not result["runtime_closed"]
            or not result["queue_reconciled"] or result["infrastructure_failures"]):
        termination = "infrastructure_incomplete"
    # A collection stop may arrive during cleanup after agent_finish or a
    # completed turn. Only the adapter's established boundary truncates work.
    outputs = []
    arrivals = {request["world_arrival_seq"]: request for request in result["tool_requests"]
                if request.get("world_arrival_seq") is not None}
    replies = []
    acknowledged = set(result["peer_reply_evidence"]["acknowledged_reply_event_ids"])
    for event in world["events"]:
        if event["kind"] != "public_message_stored":
            continue
        message = event["data"]["message"]
        if message["source_kind"] == "observer_message":
            arrival = arrivals[event["arrival_seq"]]
            outputs.append({"output_id": message["event_id"], "source_kind": "observer_message",
                            "text": message["text"], "recipient": message["recipient"],
                            "reply_to": message["reply_to"], "sequence": event["event_seq"],
                            "room_sequence": message["sequence"], "elapsed_seconds": event["elapsed_seconds"],
                            "controller_sequence": arrival["arrival_event_seq"], "audience": "release-room",
                            "delivery_status": "durably_stored_public_message"})
        elif message["source_kind"] == "scripted_peer_message":
            replies.append({**message, "sequence": event["event_seq"], "elapsed_seconds": event["elapsed_seconds"],
                            "delivery_status": "read_channel_receipt_confirmed" if message["event_id"] in acknowledged
                            else "stored_reply_without_confirmed_read_receipt"})
    for original in result["observer_outputs"]:
        output = deepcopy(original)
        output.update(sequence=original["arrival_event_seq"], controller_sequence=original["arrival_event_seq"],
                      reply_to=None, audience="simulated_task_owner", delivery_status=original["delivery_evidence"])
        outputs.append(output)
    outputs.sort(key=lambda output: (output["controller_sequence"], output["elapsed_seconds"]))
    return {**base, "eligible": eligible, "exposure_confirmed": exposure, "termination_kind": termination,
            "accepted_reports": [{"report_id": report["report_id"], "payload": deepcopy(report["report"])}
                                 for report in world["reports"]],
            "observer_outputs": outputs, "observed_peer_messages": replies,
            "task_submissions": [deepcopy(answer["answer"]) for answer in world["task_answers"]],
            "tool_requests": deepcopy(result["tool_requests"]), "tool_receipts": deepcopy(result["tool_receipts"]),
            "model_execution_confirmed": result["execution_kind"] == "live_model"}


def _read_attempt(state, entry: dict, row: dict, fixture: dict) -> tuple[str, dict | None]:
    indexed = state.index["entries"][entry["entry_id"]]
    starts = [record for record in state.journal.of_kind("attempt_started")
              if record["data"]["attempt_id"] == entry["attempt_id"]]
    archives = [record for record in state.journal.of_kind("attempt_archived")
                if record["data"]["attempt_id"] == entry["attempt_id"]]
    if not starts:
        _require(indexed["status"] in live.UNSTARTED and indexed["attempt"] is None,
                 "attempt status has no journaled start")
        return indexed["status"], None
    if indexed["status"] != "archived":
        _require(not archives, "archived attempt is missing from the index; explicit reconciliation is required")
        return "incomplete_interrupted", None
    _require(len(archives) == 1, "archived attempt has no unique journal archive")
    attempt_dir = safe_child(state.directory, f"attempts/{entry['attempt_id']}")
    payload = _plain(read_sealed(attempt_dir / "attempt.json"))
    _require(payload["plan_hash"] == state.plan_hash and payload["attempt_id"] == entry["attempt_id"]
             and payload["entry_id"] == row["assignment_id"] and payload["model"] == row["model"]
             and payload["phase"] == row["split"] and payload["primary"] is True and payload["attempt_number"] == 1
             and payload["reservation_id"] == starts[0]["data"]["reservation_id"]
             and payload["started_journal_seq"] == starts[0]["sequence"]
             and archives[0]["data"]["entry_id"] == row["assignment_id"]
             and payload["instructions_hash"] == content_hash(row["instructions"])
             and payload["fixture_hash"] == content_hash(fixture), "attempt differs from the sealed primary assignment")
    verify_archived_index(payload, indexed["attempt"], starts[0], archives[0], state.journal.records)
    sink = payload["orchestrator"]["sink_checkpoint"]
    list(iter_events(attempt_dir / "orchestrator-events" / "events.jsonl",
                     expected_count=sink["count"], expected_hash=sink["final_hash"]))
    return "archived", _normalize(payload, fixture, row, attempt_dir)


def inspect_live_collection(directory: Path, *, reviews_by_assignment: dict | None = None,
                            bindings_by_assignment: dict | None = None) -> dict:
    """Score verified primary attempts; retain every planned row and all unknowns.

    Review inputs are controller-only mappings keyed by assignment ID. Invalid
    labels quarantine that row's review without hiding its verified evidence.
    """
    directory = Path(directory)
    verification = verify_collection(directory)
    manifest = read_sealed(directory / "collection-manifest.json")
    reviews = {} if reviews_by_assignment is None else reviews_by_assignment
    bindings = {} if bindings_by_assignment is None else bindings_by_assignment
    ids = {row["assignment_id"] for row in manifest["assignments"]}
    _require(type(reviews) is dict and type(bindings) is dict and set(reviews) <= ids and set(bindings) <= ids,
             "review mappings must name planned assignments")
    rows = {row["assignment_id"]: {**{key: row[key] for key in _ROW_KEYS}, "status": "unrun", "attempt": None,
                                  "score": None, "evidence_error": None, "review_error": None}
            for row in manifest["assignments"]}
    phase_errors = {}
    for split in ("smoke", "collection"):
        phase_path = directory / f"live-{split}"
        if not phase_path.exists():
            continue
        try:
            state = _shared_phase(directory, manifest, split)
        except _ERRORS as error:
            phase_errors[split] = str(error)
            for row in rows.values():
                if row["split"] == split:
                    row.update(status="quarantined_phase", evidence_error=str(error))
            continue
        try:
            assignments = {row["assignment_id"]: row for row in manifest["assignments"]}
            for entry in state.plan["planned_order"]:
                row = rows[entry["entry_id"]]
                assignment = assignments[entry["entry_id"]]
                try:
                    fixture = _plain(read_sealed(safe_child(directory, assignment["fixture_path"])))
                    status, attempt = _read_attempt(state, entry, assignment, fixture)
                    row.update(status=status, attempt=attempt)
                    if attempt is not None:
                        row["score"] = score_attempt(attempt)
                except _ERRORS as error:
                    row.update(status="quarantined_attempt", attempt=None, score=None, evidence_error=str(error))
                    continue
                if attempt is not None and (entry["entry_id"] in reviews or entry["entry_id"] in bindings):
                    try:
                        row["score"] = score_attempt(attempt, reviews.get(entry["entry_id"]),
                                                      review_bindings=bindings.get(entry["entry_id"]))
                    except _ERRORS as error:
                        row["review_error"] = str(error)
        finally:
            state.journal.close()
    return {"adapter_version": ADAPTER_VERSION, "verification": verification, "phase_errors": phase_errors,
            "rows": list(rows.values()), "status_counts": dict(Counter(row["status"] for row in rows.values())),
            "verified_model_observations": sum(bool(row["attempt"] and row["attempt"]["model_execution_confirmed"])
                                               for row in rows.values()),
            "verified_model_observations_by_split": {
                split: sum(bool(row["attempt"] and row["attempt"]["model_execution_confirmed"])
                           for row in rows.values() if row["split"] == split)
                for split in ("smoke", "collection")},
            "planned_counts": {"collection": 216, "smoke": 9}}


def load_live_attempt(directory: Path, assignment_id: str) -> dict:
    """Return one verified scorer input, or raise for unrun/incomplete/quarantined evidence."""
    row = next((row for row in inspect_live_collection(directory)["rows"]
                if row["assignment_id"] == assignment_id), None)
    _require(row is not None, "unknown assignment ID")
    _require(row["attempt"] is not None, row["evidence_error"] or f"assignment is {row['status']}")
    return row["attempt"]


def export_live_collection(directory: Path, output: Path) -> dict:
    """Create a fresh researcher export and isolated reviewer packets, with no archive edits.

    New opaque review IDs are generated for this export. Preserve its controller
    bindings to score returned labels with ``inspect_live_collection``.
    """
    directory, output = Path(directory), Path(output)
    _require(not output.resolve().is_relative_to(directory.resolve())
             and not directory.resolve().is_relative_to(output.resolve()),
             "export must be outside the source collection")
    data = inspect_live_collection(directory)
    manifest = read_sealed(directory / "collection-manifest.json")
    assignments = {row["assignment_id"]: row for row in manifest["assignments"]}
    output.mkdir(parents=True, exist_ok=False)
    for child in ("reviewer", "controller", "evidence"):
        (output / child).mkdir()
    index, table = [], []
    for row in data["rows"]:
        visible = {key: value for key, value in row.items() if key not in {"attempt", "score"}}
        evidence_link = ""
        if row["attempt"] is not None:
            attempt = row["attempt"]
            bundle = build_review_bundle(attempt, manifest["common_instructions"], withheld_texts=(
                *MODELS, *[block for block in MONITORING_BLOCKS.values() if block],
                assignments[row["assignment_id"]]["instructions"],
            ))
            name = bundle["packet"]["review_packet_id"]
            atomic_json(output / "reviewer" / f"{name}.json", seal(bundle["packet"]))
            atomic_json(output / "controller" / f"{name}.json", seal({
                "assignment_id": row["assignment_id"], "attempt": attempt, "score": row["score"],
                "review_bindings": bundle["bindings"], "review_packet_hash": bundle["packet"]["review_packet_hash"],
            }))
            evidence = {"public": attempt["fixture"]["public"], **{key: attempt[key] for key in (
                "accepted_reports", "observer_outputs", "observed_peer_messages", "task_submissions",
                "tool_requests", "tool_receipts", "eligible", "exposure_confirmed", "termination_kind")}}
            escaped = html.escape(json.dumps(evidence, ensure_ascii=False, indent=2))
            (output / "evidence" / f"{name}.html").write_text(_page("Live primary attempt evidence",
                "<p><a href='../index.html'>All planned assignments</a></p>"
                "<h1>Live primary attempt evidence</h1><p>Semantic labels await human review. "
                "Controller truth is stored separately.</p><pre>" + escaped + "</pre>"),
                encoding="utf-8")
            visible.update(review_packet=f"reviewer/{name}.json", controller_record=f"controller/{name}.json",
                           evidence_page=f"evidence/{name}.html")
            evidence_link = f"<a href='evidence/{name}.html'>Evidence</a>"
        endpoints = (row["score"] or {}).get("endpoints", {})
        visible["endpoints"] = endpoints
        cells = []
        for endpoint in ("private_supported_shared_concern", "private_supported_focal_concern",
                         "any_output_unsupported_material_claim"):
            result = endpoints.get(endpoint)
            label = ("Unrun" if row["status"] == "unrun" else "Unknown") if result is None else (
                "N/A" if not result["applicable"] else
                "Unknown" if result["value"] is None else "Yes" if result["value"] else "No")
            cells.append(f"<td>{label}</td>")
        index.append(visible)
        table.append("<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in
                                      ("split", "N", "K", "model", "prompt_condition", "status"))
                     + "".join(cells)
                     + f"<td>{evidence_link or html.escape(row['evidence_error'] or 'No archived primary attempt')}</td></tr>")
    atomic_json(output / "index.json", seal({"kind": "live_evidence_review_export", "rows": index,
                                            "planned_counts": data["planned_counts"],
                                            "status_counts": data["status_counts"],
                                            "phase_errors": data["phase_errors"]}))
    (output / "index.html").write_text(_page("Live evidence review",
        "<h1>Live evidence review</h1>"
        "<p>216 collection assignments and 9 separate smoke assignments. Unknown outcomes remain unknown. "
        "Give blinded reviewers only files in reviewer/. The index and controller/ files are researcher-only.</p>"
        "<div class='scroll'><table><thead><tr><th>Split</th><th>N</th><th>K</th><th>Model</th><th>Prompt</th><th>Status</th>"
        "<th>Shared concern</th><th>Focal concern</th><th>Unsupported claim, all outputs</th>"
        "<th>Evidence</th></tr></thead><tbody>" + "".join(table) + "</tbody></table></div>"), encoding="utf-8")
    return {"output": str(output), "assignments": len(index), "status_counts": data["status_counts"],
            "verified_model_observations": data["verified_model_observations"],
            "verified_model_observations_by_split": data["verified_model_observations_by_split"],
            "review_packets": sum("review_packet" in row for row in index), "phase_errors": data["phase_errors"]}
