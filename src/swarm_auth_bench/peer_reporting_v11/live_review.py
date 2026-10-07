"""Read retained v1.1 lane evidence into scorer inputs, without rewriting any source artifact.

Each archived primary attempt becomes one attempt in the shape produced by P1's
``peer_reporting.live_review`` (``fixture``, ``tool_requests``, ``observer_outputs``,
``task_submissions``, ``accepted_reports``, ``termination_kind``,
``exposure_confirmed``, ``eligible``, ...) with these v1.1 additions:

- ``model``, ``effort``, ``world_mode``, ``prompt_condition``, and the descriptive
  assignment labels (``phase``, ``lane_id``, ``split``, ``arm``, ``template_id``,
  ``level``, ``variant``, ``near_miss_type``, ``planned_order``);
- ``report_attempts``: every ``report_incident`` request, stored or not, with its
  raw arguments, result, transport ``call_id``, ``world_arrival_seq``, and
  ``duplicate_of_arrival_seq`` (set on a duplicate transport call), for
  ``focal_intent`` and the unavailable report store;
- ``usage`` (settled and observed tokens, and the settlement label, such as
  ``bounded_by_reservation``) and ``elapsed_seconds``;
- ``excluded_from_analysis``: true for a failed attempt that an approved
  amendment accepted, for a ``provider_unavailable`` attempt (a capacity
  refusal with reconciled tool work and cleanup, v1.2 section 8.1, A3), and for a
  ``provider_stalled`` attempt (packet delivery confirmed, then no model event
  until the trial wall, revision 4); such an attempt is also not ``eligible``. It is consumed and exported, not quarantined, and its index
  row names the ``exclusion_reason``. Its score stays on its row, but the
  export's summary leaves it out of every count and cell (spec 12); the index
  lists the excluded rows in ``analysis_exclusions`` and counts them in
  ``analysis_exclusion_count``.

No model, repair, resume, or admission operation is performed. The export needs
the study directory in which the root is registered and the root at its
registered path; it rechecks the root against the study's start ledger and its
consumed-attempt ledger against its prior roots, and records the study's registry
listing and amendments. It applies verify's authorization-evidence check to every
started row: a row whose retained authorization record is missing, corrupt, or
mismatched is ``quarantined_authorization`` and is not scored, while other valid
rows still export. The index carries ``review_plan_hash`` (null outside
collection) and a calibration root's ``selected_arms``, which the export
rechecks against the sealed lane plans. Compatibility roots are engineering
checks and are not exported.
Every planned row is kept; unrun, incomplete, and quarantined rows have no
attempt and never become negatives.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Callable

from ..events import content_hash
from ..long_events import iter_events
from ..peer_reporting.live import UNSTARTED, _plain
from ..peer_reporting.live_archive import verify_archived_index
from ..peer_reporting.live_runtime import _text_item
from ..peer_reporting.storage import atomic_json, read_sealed, safe_child, seal
from .bundle import ProtocolBundle, load_bundle
from .lanes import PROVIDER_PAUSE_CLASSIFICATIONS
from .live import (
    CONFIGURATION_CHECKS,
    _row_valid,
    authorization_error,
    check_abandoned_root,
    check_arm_selection,
    check_retained_review_plan,
    check_start_claims,
    evaluate_transport,
    journaled_authorizations,
    lane_journals,
    planned_arms,
    read_live_plan,
    read_root_fixture,
    read_study_manifest,
    root_registration,
    start_authorization_error,
    study_amendments,
    study_registry_listing,
    verify_consumed_ledger,
)
from .phase import _PhaseState
from .review_plan import pressure_review_selection

ADAPTER_VERSION = "peer-reporting-v11-live-review-v1"
EXPORT_KIND = "peer_reporting_v11_live_review_export"
_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError)
ROW_LABELS = ("split", "arm", "template_id", "level", "variant", "near_miss_type", "planned_order")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _labels(entry: dict, phase: str, lane: str) -> dict:
    return {"phase": phase, "lane_id": lane, "model": entry["model"], "effort": entry["reasoning_effort"],
            "world_mode": entry["world_mode"], "prompt_condition": entry["prompt_condition"],
            **{key: entry[key] for key in ROW_LABELS},
            **({key: entry[key] for key in ("difficulty", "block", "prevalence_k", "post_condition")}
               if entry["level"] == "S" else {}),
            **({key: entry[key] for key in ("block", "prevalence_k", "visibility", "pressure")}
               if entry["level"] == "P" else {})}


def pressure_events(world: dict) -> list[dict]:
    """C5: complete copies of P notices, reactions and held-answer events, in world order."""
    return [deepcopy(event) for event in world["events"]
            if event["kind"] in ("pressure_reactions_stored", "task_answer_held")
            or (event["kind"] == "public_message_stored"
                and event["data"]["message"]["source_kind"] in ("system_notice", "scripted_peer_reaction"))]


def report_attempts(tool_requests: list[dict]) -> list[dict]:
    """Every report_incident request in arrival order, whether or not the world stored it.

    A duplicate transport call carries ``duplicate_of_arrival_seq`` and receives
    the first call's result; count reports by distinct ``report_id``.
    """
    return [{"arrival_seq": request["arrival_seq"], "call_id": request.get("call_id"),
             "world_arrival_seq": request.get("world_arrival_seq"),
             "duplicate_of_arrival_seq": request.get("duplicate_of_arrival_seq"),
             "arguments": deepcopy(request.get("arguments")),
             "admitted": request.get("admitted"), "result": deepcopy(request.get("result")),
             "stored": (request.get("result") or {}).get("status") == "stored"}
            for request in tool_requests if request.get("tool") == "report_incident"]


def normalize_attempt(payload: dict, fixture: dict, entry: dict, attempt_dir: Path, *, phase: str, lane: str,
                      bundle: ProtocolBundle) -> dict:
    result = payload["observer_result"]
    settlement = payload["orchestrator"]["usage_settlement"]
    base = {
        "adapter_version": ADAPTER_VERSION, "execution_kind": "live_model",
        "assignment_id": entry["entry_id"], "attempt_id": payload["attempt_id"], "primary": True,
        "attempt_number": 1, "fixture": fixture, "source_attempt_hash": content_hash(payload),
        "source_plan_hash": payload["plan_hash"], **_labels(entry, phase, lane),
        **({"pressure_events": []} if fixture["truth"]["level"] == "P" else {}),
    }
    if result is None:
        return {**base, "eligible": False, "exposure_confirmed": False,
                "termination_kind": "infrastructure_incomplete", "accepted_reports": [], "report_attempts": [],
                "observer_outputs": [], "observed_peer_messages": [], "task_submissions": [],
                "tool_requests": [], "tool_receipts": [], "model_execution_confirmed": False,
                "usage": {"total_tokens": settlement["actual_tokens"], "observed_total_tokens": None,
                          "settlement": settlement["status"]},
                "elapsed_seconds": None, "excluded_from_analysis": False}
    _require(type(result) is dict, "observer result is not an object")
    _require(result["attempt_id"] == payload["attempt_id"]
             and result["instructions_hash"] == content_hash(entry["instructions"])
             and result["instructions_and_roles_hash"] == entry["instructions_and_roles_hash"]
             and result["packet_sha256"] == hashlib.sha256(fixture["packet"].encode("utf-8")).hexdigest()
             and result["reasoning_effort"] == entry["reasoning_effort"]
             and result["world_mode"] == entry["world_mode"],
             "live result differs from the sealed assignment inputs")
    world = bundle.audit_state(attempt_dir / "world", result["world_checkpoint"])
    _require(world == result["world_state"] and world["trial_id"] == payload["attempt_id"],
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
                 and _text_item(item) == fixture["packet"], "initial packet receipt mismatch")
    check = evaluate_transport(result, fixture=fixture, entry=entry, preflight=payload["preflight"], bundle=bundle,
                               orchestrator_failures=payload["orchestrator"]["evidence_failures"],
                               observer_error=payload["observer_error"], usage_settlement=settlement["status"],
                               stop_reasons=payload["orchestrator"]["collection_stop_reasons"])
    _require(check == payload["check"], "archived transport classification differs from its evidence")
    # Spec 10 (revisions 3 and 4): a capacity refusal or a silent stall is consumed and exported, but it is no
    # behavioral opportunity.
    provider_pause = check.get("classification") in PROVIDER_PAUSE_CLASSIFICATIONS
    _require(provider_pause == (payload["orchestrator"].get("provider_pause") is not None),
             "a provider pause is sealed exactly for a provider_unavailable or provider_stalled attempt")
    eligible = (all(check["checks"][key] for key in CONFIGURATION_CHECKS if key != "tools_registered")
                and not provider_pause)
    # Later infrastructure failures close the opportunity incompletely; they do
    # not erase a known positive established before that failure.
    termination = result["termination_kind"]
    if (payload["orchestrator"]["evidence_failures"] or not result["runtime_closed"]
            or not result["queue_reconciled"] or result["infrastructure_failures"]):
        termination = "infrastructure_incomplete"
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
        if original["source_kind"] in ("system_notice", "scripted_peer_reaction"):
            continue
        output = deepcopy(original)
        output.update(sequence=original["arrival_event_seq"], controller_sequence=original["arrival_event_seq"],
                      reply_to=None, audience="simulated_task_owner", delivery_status=original["delivery_evidence"])
        outputs.append(output)
    outputs.sort(key=lambda output: (output["controller_sequence"], output["elapsed_seconds"]))
    return {**base, "eligible": eligible, "exposure_confirmed": exposure, "termination_kind": termination,
            **({"pressure_events": pressure_events(world)} if fixture["truth"]["level"] == "P" else {}),
            **({"overload_stage": result["overload_stage"]} if "overload_stage" in result else {}),
            "accepted_reports": [{"report_id": report["report_id"], "payload": deepcopy(report["report"])}
                                 for report in world["reports"]],
            "report_attempts": report_attempts(result["tool_requests"]),
            "observer_outputs": outputs, "observed_peer_messages": replies,
            "task_submissions": [deepcopy(answer["answer"]) for answer in world["task_answers"]],
            "tool_requests": deepcopy(result["tool_requests"]), "tool_receipts": deepcopy(result["tool_receipts"]),
            "model_execution_confirmed": result["execution_kind"] == "live_model",
            "usage": {"total_tokens": settlement["actual_tokens"],
                      "observed_total_tokens": (result.get("usage") or {}).get("observed_total_tokens"),
                      "settlement": settlement["status"]},
            "elapsed_seconds": result.get("elapsed_seconds"), "excluded_from_analysis": provider_pause}


def _read_attempt(state: _PhaseState, entry: dict, fixture: dict, *, phase: str, bundle: ProtocolBundle
                  ) -> tuple[str, dict | None]:
    indexed = state.index["entries"][entry["entry_id"]]
    starts = [record for record in state.journal.of_kind("attempt_started")
              if record["data"]["attempt_id"] == entry["attempt_id"]]
    archives = [record for record in state.journal.of_kind("attempt_archived")
                if record["data"]["attempt_id"] == entry["attempt_id"]]
    if not starts:
        _require(indexed["status"] in UNSTARTED and indexed["attempt"] is None,
                 "attempt status has no journaled start")
        return indexed["status"], None
    if indexed["status"] != "archived":
        _require(not archives, "archived attempt is missing from the index; explicit reconciliation is required")
        return "incomplete_interrupted", None
    _require(len(starts) == 1 and len(archives) == 1, "archived attempt has no unique start and archive")
    attempt_dir = safe_child(state.directory, f"attempts/{entry['attempt_id']}")
    payload = _plain(read_sealed(attempt_dir / "attempt.json"))
    _require(payload["plan_hash"] == state.plan_hash and payload["attempt_id"] == entry["attempt_id"]
             and payload["entry_id"] == entry["entry_id"] and payload["model"] == entry["model"]
             and payload["reasoning_effort"] == entry["reasoning_effort"]
             and payload["world_mode"] == entry["world_mode"]
             and payload["prompt_condition"] == entry["prompt_condition"]
             and payload["phase"] == phase and payload["primary"] is True and payload["attempt_number"] == 1
             and payload["reservation_id"] == starts[0]["data"]["reservation_id"]
             and payload["started_journal_seq"] == starts[0]["sequence"]
             and archives[0]["data"]["entry_id"] == entry["entry_id"]
             and payload["instructions_hash"] == content_hash(entry["instructions"])
             and payload["authorization_hash"] == starts[0]["data"].get("authorization_hash")
             and payload["fixture_hash"] == content_hash(fixture), "attempt differs from the sealed primary assignment")
    verify_archived_index(payload, indexed["attempt"], starts[0], archives[0], state.journal.records)
    sink = payload["orchestrator"]["sink_checkpoint"]
    list(iter_events(attempt_dir / "orchestrator-events" / "events.jsonl",
                     expected_count=sink["count"], expected_hash=sink["final_hash"]))
    return "archived", normalize_attempt(payload, fixture, entry, attempt_dir, phase=phase,
                                         lane=state.plan["lane_id"], bundle=bundle)


def _exclude(row: dict, entry: dict, state: _PhaseState, hashes: list[str], errors: list[str]) -> None:
    """Mark a failed attempt that an amendment accepted; refuse an amendment of an unstarted or valid attempt."""
    indexed = state.index["entries"][entry["entry_id"]]
    attempt = indexed["attempt"] or {}
    summary = {"status": indexed["status"], "check_passed": attempt.get("check_passed"),
               "usage_settlement": attempt.get("usage_settlement"),
               "usage_total_tokens": attempt.get("usage_total_tokens")}
    if indexed["status"] in UNSTARTED or _row_valid(summary):
        errors.append(f"amendment {hashes} accepts {entry['attempt_id']}, which is not a failed attempt of this root")
        return
    row.update(excluded_from_analysis=True, amendment_hashes=hashes)
    row.setdefault("exclusion_reason", "accepted_by_amendment")
    if row["attempt"] is not None:
        row["attempt"].update(excluded_from_analysis=True, eligible=False)


def inspect_live_root(directory: Path, *, bundle: ProtocolBundle | None = None,
                      scorer: Callable[[dict], dict] | None = None, amendments: list[dict] | tuple = ()) -> dict:
    """Return every planned row; verified archived rows carry their attempt and optional score.

    Every started row carries its journaled ``authorization_hash`` and the shared
    authorization check's ``authorization_error``; an archived row with an error
    is ``quarantined_authorization`` and is never scored. Failed attempts that
    one of ``amendments`` accepts are marked ``excluded_from_analysis`` before scoring.
    """
    bundle = bundle or load_bundle()
    accepted: dict[str, list[str]] = {}
    for amendment in amendments:
        for attempt_id in amendment["attempt_ids"]:
            accepted.setdefault(attempt_id, []).append(amendment["seal_hash"])
    amendment_errors: list[str] = []
    directory = Path(directory)
    plan = read_live_plan(directory)
    _require(plan["phase"] != "compatibility",
             "compatibility attempts are engineering checks, not behavioral observations")
    rows: list[dict] = []
    lane_errors: dict[str, str] = {}
    authorizations: dict[str, str | None] = {}
    for lane in plan["lanes"]:
        try:
            state = _PhaseState(safe_child(directory, lane["path"]), bundle=bundle)
        except _ERRORS as error:
            lane_errors[lane["lane_id"]] = str(error)
            state = None
        if state is not None:
            try:
                _require(state.plan_hash == lane["plan_hash"], "lane plan differs from the sealed live plan")
                state.verify_budget_history()
            except _ERRORS as error:
                lane_errors[lane["lane_id"]] = str(error)
                state.journal.close()
                state = None
        if state is None:
            lane_plan = read_sealed(safe_child(directory, f"{lane['path']}/phase-plan.json"))
            for entry in lane_plan["planned_order"]:
                rows.append({"assignment_id": entry["entry_id"], **_labels(entry, plan["phase"], lane["lane_id"]),
                             "status": "quarantined_lane", "attempt": None, "score": None,
                             "evidence_error": lane_errors[lane["lane_id"]], "score_error": None,
                             "excluded_from_analysis": False, "authorization_hash": None,
                             "authorization_error": None})
            continue
        try:
            for entry in state.plan["planned_order"]:
                row = {"assignment_id": entry["entry_id"], **_labels(entry, plan["phase"], lane["lane_id"]),
                       "status": "unrun", "attempt": None, "score": None, "evidence_error": None,
                       "score_error": None, "excluded_from_analysis": False, "authorization_hash": None,
                       "authorization_error": None}
                starts = [record for record in state.journal.of_kind("attempt_started")
                          if record["data"]["attempt_id"] == entry["attempt_id"]]
                if starts:  # spec 10: the same authorization-evidence check as verify
                    row["authorization_hash"] = starts[0]["data"].get("authorization_hash")
                    row["authorization_error"] = start_authorization_error(directory, plan, starts[0],
                                                                           authorizations)
                try:
                    fixture = read_root_fixture(directory, plan, entry["fixture_id"])
                    row["status"], row["attempt"] = _read_attempt(state, entry, fixture, phase=plan["phase"],
                                                                  bundle=bundle)
                except _ERRORS as error:
                    row.update(status="quarantined_attempt", attempt=None, evidence_error=str(error))
                if row["attempt"] is not None and row["authorization_error"] is not None:
                    row.update(status="quarantined_authorization", attempt=None,
                               evidence_error=row["authorization_error"])
                if row["attempt"] is not None and row["attempt"]["excluded_from_analysis"]:
                    # A pause classification requires the same termination kind, so it names the reason.
                    row.update(excluded_from_analysis=True, exclusion_reason=row["attempt"]["termination_kind"])
                if row["attempt"] is not None and "overload_stage" in row["attempt"]:
                    row["overload_stage"] = row["attempt"]["overload_stage"]
                if entry["attempt_id"] in accepted:
                    _exclude(row, entry, state, sorted(accepted[entry["attempt_id"]]), amendment_errors)
                if row["attempt"] is not None and scorer is not None:
                    try:
                        row["score"] = scorer(row["attempt"])
                    except _ERRORS as error:
                        row["score_error"] = str(error)
                rows.append(row)
        finally:
            state.journal.close()
    rows.sort(key=lambda row: row["planned_order"])
    return {"adapter_version": ADAPTER_VERSION, "phase": plan["phase"], "plan_hash": plan["seal_hash"],
            "rows": rows, "lane_errors": lane_errors, "amendment_errors": amendment_errors,
            "authorization_evidence": dict(sorted(authorizations.items())),
            "status_counts": dict(Counter(row["status"] for row in rows)),
            "verified_model_observations": sum(bool(row["attempt"] and row["attempt"]["model_execution_confirmed"])
                                               for row in rows),
            "planned_count": plan["maximum_live_calls"]}


def export_live_review(directory: Path, output: Path, *, study_directory: Path | None = None,
                       prior_roots: list[Path] | tuple = (), bundle: ProtocolBundle | None = None,
                       scorer: Callable[[dict], dict] | None = None,
                       summarize: Callable[[list[dict]], dict] | None = None) -> dict:
    """Write a fresh researcher export: one sealed attempt per archived row and a sealed index.

    ``study_directory`` is the study directory in which the root is registered,
    and the root must sit at its registered path and agree with the study's
    start ledger. ``prior_roots`` must be exactly the prior roots sealed in the
    plan's consumed-attempt ledger; the export refuses if an assignment ran in
    two roots. The index carries the study's registry listing and amendments,
    every journaled authorization with its evidence check, and ``review_plan_hash``.
    """
    directory, output = Path(directory), Path(output)
    _require(not output.resolve().is_relative_to(directory.resolve())
             and not directory.resolve().is_relative_to(output.resolve()),
             "export must be outside the source live root")
    bundle = bundle or load_bundle()
    plan = read_live_plan(directory)
    _require(plan["phase"] != "compatibility",
             "compatibility attempts are engineering checks, not behavioral observations")
    registration = root_registration(study_directory, plan, directory=directory, require_finalized=False)
    check_abandoned_root(directory, registration)
    journals = lane_journals(directory, plan)
    from .ledger_repair import verify_root_ledger_repairs

    ledger_repairs = verify_root_ledger_repairs(directory, plan, journals)
    start_claims = check_start_claims(study_directory, plan, directory, journals)
    ledger = verify_consumed_ledger(directory, plan, prior_roots, bundle=bundle, study_directory=study_directory)
    selected_arms = check_arm_selection(plan, planned_arms(directory, plan))
    review_plan_hash = check_retained_review_plan(directory, plan) if plan["phase"] == "collection" else None
    amendments = study_amendments(study_directory) if plan["phase"] == "smoke" else []
    data = inspect_live_root(directory, bundle=bundle, scorer=scorer, amendments=amendments)
    _require(not data["amendment_errors"], "; ".join(data["amendment_errors"]))
    evidence = data["authorization_evidence"]
    authorizations = {value: evidence[value] if value in evidence else authorization_error(directory, plan, value)
                      for value in journaled_authorizations(journals)}
    output.mkdir(parents=True, exist_ok=False)
    (output / "attempts").mkdir()
    index = []
    for row in data["rows"]:
        visible = {key: value for key, value in row.items() if key != "attempt"}
        if row["attempt"] is not None:
            relative = f"attempts/{row['assignment_id']}.json"
            atomic_json(safe_child(output, relative), seal(row["attempt"]))
            visible.update(attempt_path=relative, attempt_hash=content_hash(row["attempt"]))
        index.append(visible)
    # Spec 12: rows excluded from analysis keep their scores but stay out of the summary's counts and cells.
    scored = [row["score"] for row in data["rows"] if row["score"] is not None and not row["excluded_from_analysis"]]
    summary = summarize(scored) if summarize is not None else None
    pressure_selection = pressure_review_selection(scored) if any(record.get("level") == "P" for record in scored) else None
    exclusions = sorted(row["assignment_id"] for row in data["rows"] if row["excluded_from_analysis"])
    atomic_json(output / "index.json", seal({
        "kind": EXPORT_KIND, "adapter_version": ADAPTER_VERSION, "phase": data["phase"],
        "source_plan_hash": data["plan_hash"], "planned_count": data["planned_count"], "rows": index,
        "status_counts": data["status_counts"], "lane_errors": data["lane_errors"],
        "verified_model_observations": data["verified_model_observations"],
        "consumed_attempt_ledger": ledger, "scored": scorer is not None, "summary": summary,
        "semantic_review": "pending_human_review_of_free_text", "study_registration": registration,
        "study_registry": study_registry_listing(study_directory), "start_claims": start_claims,
        "authorization_evidence": authorizations, "review_plan_hash": review_plan_hash,
        "selected_arms": selected_arms,
        **({"pressure_review_selection": pressure_selection} if pressure_selection is not None else {}),
        # Review packets recompute the frozen plan against the sealed manifest this root was built from.
        "study_manifest": read_study_manifest(study_directory),
        "analysis_exclusions": exclusions, "analysis_exclusion_count": len(exclusions),
        "ledger_repairs": ledger_repairs,
        "declared_deviations": [{"kind": "ledger_repaired", "lane_id": repair["lane_id"],
                                 "repair_hash": repair["seal_hash"]} for repair in ledger_repairs],
    }))
    return {"output": str(output), "phase": data["phase"], "assignments": len(index),
            "attempts": sum("attempt_path" in row for row in index), "status_counts": data["status_counts"],
            "verified_model_observations": data["verified_model_observations"], "lane_errors": data["lane_errors"],
            "scored": scorer is not None, "ledger_repairs": ledger_repairs}
