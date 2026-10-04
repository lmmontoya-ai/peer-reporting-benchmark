"""Derive live index summaries from archived evidence, never from editable labels."""

from __future__ import annotations

from copy import deepcopy

from ..events import content_hash


def attempt_summary(payload: dict) -> dict:
    result = payload.get("observer_result") or {}
    check = payload["check"]
    orchestrator = payload["orchestrator"]
    settlement = orchestrator["usage_settlement"]
    usage = result.get("usage") or {}
    world = result.get("world_state") or {}
    return {
        "attempt_hash": content_hash(payload),
        "termination_kind": result.get("termination_kind", "infrastructure_incomplete"),
        "check_passed": check["passed"], "classification": check.get("classification"),
        "failure_reasons": deepcopy(check["failure_reasons"]),
        "world_checkpoint": deepcopy(result.get("world_checkpoint")),
        "controller_checkpoint": deepcopy(result.get("controller_checkpoint")),
        "sink_checkpoint": deepcopy(orchestrator["sink_checkpoint"]),
        "usage_total_tokens": settlement["actual_tokens"], "usage_settlement": settlement["status"],
        "observed_total_tokens": usage.get("observed_total_tokens"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "drain_elapsed_seconds": result.get("drain_elapsed_seconds"),
        "tool_request_count": len(result.get("tool_requests", world.get("requests") or [])),
        "collection_stop_reasons": deepcopy(orchestrator["collection_stop_reasons"]),
        "observer_error": payload.get("observer_error"),
        "cleanup_confirmed": result.get("runtime_closed") is True and result.get("queue_reconciled") is True,
    }


def verify_archived_index(payload: dict, indexed: dict, started: dict, archived: dict,
                          journal_records: list[dict]) -> None:
    expected_summary = attempt_summary(payload)
    if archived["data"]["summary"] != expected_summary:
        raise ValueError("journaled archive summary differs from its retained payload")
    start = started["data"]
    expected = {
        "attempt_id": start["attempt_id"], "reservation_id": start["reservation_id"],
        "path": f"attempts/{start['attempt_id']}", "started_journal_seq": started["sequence"],
        **expected_summary, "status": "archived",
    }
    reconciliations = [record for record in journal_records if record["kind"] == "usage_reconciled"
                       and record["data"].get("attempt_id") == start["attempt_id"]]
    if reconciliations:
        record = reconciliations[-1]
        expected["usage_reconciliation"] = {
            "total_tokens": record["data"]["total_tokens"], "evidence": record["data"]["evidence"],
            "journal_seq": record["sequence"],
        }
    if indexed != expected:
        raise ValueError("indexed archive labels or checkpoints differ from retained evidence")
