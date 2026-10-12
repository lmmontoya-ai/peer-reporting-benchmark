"""Primary runtime recordings, without any interpretation of model text."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

CREDENTIAL_KEY = r"(?:api[_-]?key|(?:access|refresh)[_-]?token|password|(?:client[_-]?)?secret|authorization)"
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)([\"']?" + CREDENTIAL_KEY + r"[\"']?\s*[=:]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s\"'<>;,}\]]+)"
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if re.search(r"secret|password|authorization|api[_-]?key|(?:access|refresh)[_-]?token",
                                               key, re.I) else _redact(child))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [redacted]", value)

        def replace(match: re.Match) -> str:
            captured = match[2]
            quote = captured[0] if captured[0] in "\"'" else ""
            return match[1] + quote + "[redacted]" + quote

        return CREDENTIAL_ASSIGNMENT.sub(replace, value)
    return value


def probe_record(request: dict) -> dict:
    shape = {key: request[key] for key in ("instructions", "reasoning", "store", "include", "truncation",
                                         "parallel_tool_calls") if key in request}
    if isinstance(request.get("text"), dict) and "verbosity" in request["text"]:
        shape["text"] = {"verbosity": request["text"]["verbosity"]}
    frame = []
    for item in request.get("input", []):
        if not isinstance(item, dict):
            continue
        parts = item.get("content", [])
        if isinstance(parts, list) and any(isinstance(part, dict)
                                          and "<environment_context>" in str(part.get("text", ""))
                                          for part in parts):
            frame.append(deepcopy(item))
    shape, frame = _redact(shape), _redact(frame)
    return {"request_shape": shape, "request_shape_sha256": _hash(shape),
            "environment_frame": frame, "environment_frame_sha256": _hash(frame)}


def attempt_recording(events: list[dict], *, thread_id: str | None, turn_id: str | None,
                      record_calls: bool, record_reasoning: bool, attestations: list[dict],
                      record_compactions: bool = False) -> dict:
    if not (record_calls or record_reasoning or record_compactions):
        return {}
    reasoning, calls, compactions, notifications = [], [], [], []
    responses: dict[str, dict] = {}
    seen_responses = set()
    context_tokens, context_window, context_event_index, context_source = None, None, None, None
    compaction_segment = 0

    def usage_fields(usage: dict | None) -> dict:
        return {key: usage.get(wire) if isinstance(usage, dict) else None
                for key, wire in (("input_tokens", "inputTokens"), ("cached_input_tokens", "cachedInputTokens"),
                                  ("output_tokens", "outputTokens"), ("reasoning_tokens", "reasoningOutputTokens"))}

    for index, event in enumerate(events):
        raw = event.get("raw") or {}
        params, method = raw.get("params") or {}, raw.get("method")
        if (event.get("kind") != "codex_event" or event.get("agent_id") != "observer"
                or params.get("threadId") != thread_id):
            continue
        turn = params.get("turn") or {}
        if params.get("turnId", turn.get("id")) != turn_id:
            continue
        item = params.get("item") or {}
        if record_reasoning:
            items = ([item] if method in {"item/completed", "rawResponse/item/completed"} else
                     turn.get("items", []) if method == "turn/completed" else [])
            for completed in items:
                if isinstance(completed, dict) and completed.get("type") == "reasoning":
                    reasoning.append({"raw_event_index": index, "method": method, "item": deepcopy(completed)})
        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage") or {}
            last = token_usage.get("last")
            if isinstance(last, dict) and last.get("inputTokens") is not None:
                context_tokens = last.get("inputTokens")
                context_event_index = index
                context_source = "thread/tokenUsage/updated.last.inputTokens"
            context_window = token_usage.get("modelContextWindow", context_window)
        elif (method == "rawResponse/completed" and isinstance(params.get("usage"), dict)
              and params["usage"].get("inputTokens") is not None):
            context_tokens = params["usage"].get("inputTokens")
            context_event_index = index
            context_source = "rawResponse/completed.usage.inputTokens"
        is_compaction = (method in {"thread/compacted", "context/compacted", "contextCompacted"}
                         or method in {"item/started", "item/completed"}
                         and item.get("type") == "contextCompaction")
        if is_compaction:
            compaction_segment += 1
            client_context, client_source = None, None
            for source, payload in (("notification.contextTokens", params), ("notification.item.contextTokens", item)):
                if payload.get("contextTokens") is not None:
                    client_context, client_source = payload["contextTokens"], source
                    break
            compactions.append({"raw_event_index": index, "method": method,
                                "context_tokens": client_context if client_source else context_tokens,
                                "context_usage_event_index": None if client_source else context_event_index,
                                "context_size_source": client_source or "last_observed_input_tokens",
                                "context_usage_source": None if client_source else context_source,
                                "model_context_window": context_window, "raw": deepcopy(raw)})
        if not record_calls or method not in {"thread/tokenUsage/updated", "rawResponse/completed"}:
            continue
        if method == "rawResponse/completed":
            response_id = params.get("responseId") or (params.get("response") or {}).get("id")
            if response_id is not None and response_id in seen_responses:
                continue
            if response_id is not None:
                seen_responses.add(response_id)
            has_usage = isinstance(params.get("usage"), dict)
            call = {"call_index": len(calls) + 1, "usage_event_index": index if has_usage else None,
                    "usage_source": "rawResponse/completed" if has_usage else None,
                    **usage_fields(params.get("usage")), "_segment": compaction_segment,
                    "_total_tokens": params["usage"].get("totalTokens") if has_usage else None}
            calls.append(call)
            metadata = (params.get("usageMetadata") or {}).get("metadata") or {}
            served = params.get("model") or (params.get("response") or {}).get("model")
            if not served and isinstance(metadata, dict):
                served = metadata.get("model")
            call.update(response_id=response_id, response_event_index=index,
                        served_model=served if isinstance(served, str) else None)
            if response_id is not None:
                responses[response_id] = call
        else:
            last = (params.get("tokenUsage") or {}).get("last")
            if not isinstance(last, dict):
                continue
            notifications.append({"raw_event_index": index, "response_id": params.get("responseId"),
                                  "context_total_tokens": last.get("totalTokens"),
                                  "model_context_window": token_usage.get("modelContextWindow"),
                                  **usage_fields(last), "_segment": compaction_segment})

    # Only completions create calls. A notification without a response ID can
    # confirm identical usage of a preceding completion in the same compaction
    # segment. It cannot supply missing usage or pair with a future response.
    estimates = []
    for notification in notifications:
        call = responses.get(notification["response_id"])
        if call is None and notification["response_id"] is None:
            matches = [candidate for candidate in calls
                       if candidate["_segment"] == notification["_segment"]
                       and candidate["response_event_index"] < notification["raw_event_index"]
                       and candidate["usage_source"] == "rawResponse/completed"
                       and "usage_notification_event_index" not in candidate
                       and candidate["_total_tokens"] == notification["context_total_tokens"]
                       and all(candidate[key] == notification[key] for key in usage_fields(None))]
            if matches:
                call = min(matches, key=lambda candidate: abs(
                    candidate["response_event_index"] - notification["raw_event_index"]))
        if call is None:
            estimates.append({key: value for key, value in notification.items() if key != "_segment"})
            continue
        call["usage_notification_event_index"] = notification["raw_event_index"]
        if call["usage_source"] is None:
            call.update(usage_event_index=notification["raw_event_index"],
                        usage_source="thread/tokenUsage/updated.last",
                        **{key: notification[key] for key in usage_fields(None)})
    for call in calls:
        call.pop("_segment")
        call.pop("_total_tokens")
    result = {"reasoning_items": reasoning} if record_reasoning and reasoning else {}
    if record_calls or record_reasoning or record_compactions:
        result["runtime_recording"] = {"compaction_events": compactions}
    if record_calls:
        result["runtime_recording"].update(model_calls=calls, context_estimates=estimates, manifest_probes=[
            deepcopy(proof["request_recording"]) for proof in attestations if "request_recording" in proof])
    return result
