"""Primary runtime recordings, without any interpretation of model text."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if re.search(r"secret|password|authorization|api[_-]?key|access[_-]?token",
                                               key, re.I) else _redact(child))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"<>]+", "Bearer [redacted]", value)
        return re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s\"<>]+",
                      r"\1[redacted]", value)
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
                      record_calls: bool, record_reasoning: bool, attestations: list[dict]) -> dict:
    reasoning, calls, compactions = [], [], []
    seen = set()
    context_tokens, context_window, context_event_index = None, None, None
    waiting_raw: list[dict] = []
    waiting_last: list[dict] = []

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
            if isinstance(last, dict):
                context_tokens = last.get("totalTokens")
                context_event_index = index
            context_window = token_usage.get("modelContextWindow", context_window)
        elif method == "rawResponse/completed" and isinstance(params.get("usage"), dict):
            context_tokens = params["usage"].get("totalTokens")
            context_event_index = index
        is_compaction = (method in {"thread/compacted", "context/compacted", "contextCompacted"}
                         or method in {"item/started", "item/completed"}
                         and item.get("type") == "contextCompaction")
        if is_compaction:
            compactions.append({"raw_event_index": index, "method": method,
                                "context_tokens": params.get("contextTokens", context_tokens),
                                "context_usage_event_index": context_event_index,
                                "context_size_source": ("notification.contextTokens" if "contextTokens" in params
                                                        else "latest_tokenUsage.last.totalTokens"),
                                "model_context_window": context_window, "raw": deepcopy(raw)})
        if not record_calls or method not in {"thread/tokenUsage/updated", "rawResponse/completed"}:
            continue
        identity = _hash([method, params])
        if identity in seen:
            continue
        seen.add(identity)
        if method == "rawResponse/completed":
            response_id = params.get("responseId")
            if any(call.get("response_id") == response_id for call in calls) and response_id is not None:
                continue
            call = waiting_last.pop(0) if waiting_last else None
            if call is None:
                call = {"call_index": len(calls) + 1, "usage_event_index": index,
                        "usage_source": "rawResponse/completed", **usage_fields(params.get("usage"))}
                calls.append(call)
                waiting_raw.append(call)
            elif isinstance(params.get("usage"), dict):
                call.update(usage_event_index=index, usage_source="rawResponse/completed",
                            **usage_fields(params["usage"]))
            metadata = (params.get("usageMetadata") or {}).get("metadata") or {}
            served = params.get("model") or (params.get("response") or {}).get("model")
            if not served and isinstance(metadata, dict):
                served = metadata.get("model")
            call.update(response_id=response_id, response_event_index=index,
                        served_model=served if isinstance(served, str) else None)
        else:
            last = (params.get("tokenUsage") or {}).get("last")
            if not isinstance(last, dict):
                continue
            if waiting_raw:
                call = waiting_raw.pop(0)
                if all(call[key] is None for key in usage_fields(None)):
                    call.update(usage_event_index=index, usage_source="thread/tokenUsage/updated.last",
                                **usage_fields(last))
            else:
                call = {"call_index": len(calls) + 1, "usage_event_index": index,
                        "usage_source": "thread/tokenUsage/updated.last", "response_id": None,
                        "served_model": None, **usage_fields(last)}
                calls.append(call)
                waiting_last.append(call)
    result = {"reasoning_items": reasoning} if record_reasoning and reasoning else {}
    if record_calls or compactions:
        result["runtime_recording"] = {"compaction_events": compactions}
    if record_calls:
        result["runtime_recording"].update(model_calls=calls, manifest_probes=[
            deepcopy(proof["request_recording"]) for proof in attestations if "request_recording" in proof])
    return result
