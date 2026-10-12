"""Bind queue derived records to durable primary controller and world evidence."""

from copy import deepcopy

from ..events import content_hash
from ..long_events import iter_events
from ..peer_reporting.live_runtime import _text_item
from ..runtime_recording import attempt_recording
from .closure import closure_outputs
from .queue_protocol import result_body


def verify_result(payload, directory, bundle):
    result = payload["observer_result"]
    if result is None:
        return
    checkpoint = result["controller_checkpoint"]
    events = [event["data"]["adapter_event"] for event in iter_events(
        directory / "runtime-log/events.jsonl", expected_count=checkpoint["event_count"],
        expected_hash=checkpoint["final_hash"])]

    def require(condition, message):
        if not condition:
            raise ValueError("queue derived record differs from primary evidence: " + message)

    require(events == result["events"], "controller events")
    raw = [event["event"] for event in events if event["kind"] == "runtime_event"]
    require(raw == result["raw_events"], "runtime events")
    world = bundle.audit_state(directory / "world", result["world_checkpoint"])
    require(world == result["world_state"], "world state")
    require(content_hash(world["queue"]["fixture"]) == payload["fixture_hash"], "world fixture")
    requested = {event["request"]["arrival_seq"]: event["request"] for event in events
                 if event["kind"] == "tool_requested"}
    sent = {event["arrival_seq"]: event["request"] for event in events if event["kind"] == "tool_response_sent"}
    reconciled = [event for event in events if event["kind"] == "observer_attempt_reconciled"]
    require(len(reconciled) == 1 and reconciled[0]["requests"] == result["tool_requests"], "final tool requests")
    require(len(requested) == len(result["tool_requests"]), "tool request count")
    for request in result["tool_requests"]:
        primary = requested[request["arrival_seq"]]
        require(all(primary.get(key) == request.get(key) for key in
                    ("tool", "arguments", "raw", "call_id", "admitted", "arrival_event_seq")), "tool request")
        if request["arrival_seq"] in sent:
            require(sent[request["arrival_seq"]] == request, "sent tool request")
        if request.get("world_arrival_seq") is not None:
            original = world["requests"][request["world_arrival_seq"] - 1]
            require(original["name"] == request["tool"] and original["arguments"] == request["arguments"]
                    and original["result"] == request["result"], "tool result")
        if request.get("response_sent"):
            import json

            from .queue_schema import serialized_bytes

            require(json.loads(request["wire_text"]) == request["result"], "wire result")
            if request.get("world_arrival_seq") is not None:
                require(request["wire_text"].encode("utf-8") == serialized_bytes(original["result"]), "wire order")
    require([event["receipt"] for event in events if event["kind"] == "tool_result_delivery_confirmed"]
            == result["tool_receipts"], "tool receipts")
    from .live_runtime import _Controller

    requests = {request["arrival_seq"]: request for request in result["tool_requests"]}
    for receipt in result["tool_receipts"]:
        require(any(event.get("raw") == receipt["raw"] for event in raw), "raw tool receipt")
        require(_Controller._receipt_matches(receipt["raw"]["params"]["item"], requests[receipt["arrival_seq"]]),
                "delivered tool text")
    require(len(reconciled) == 1 and reconciled[0]["usage"] == result["usage"]
            and reconciled[0]["termination_kind"] == result["termination_kind"]
            and reconciled[0]["failures"] == result["infrastructure_failures"]
            and reconciled[0]["queue_reconciled"] == result["queue_reconciled"]
            and reconciled[0]["runtime_closed"] == result["runtime_closed"], "termination or usage")
    outputs = [event["outputs"] for event in events if event["kind"] == "observer_outputs_recorded"]
    require(outputs == [result["observer_outputs"]], "observer outputs")
    texts, deltas = {}, {}
    for event in raw:
        notification = event.get("raw") or {}
        params = notification.get("params") or {}
        item = params.get("item") or {}
        if notification.get("method") == "item/completed" and item.get("type") == "agentMessage":
            texts[item["id"]] = _text_item(item)
        if notification.get("method") == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
            identifier = params.get("itemId")
            deltas[identifier] = deltas.get(identifier, "") + params["delta"]
    for output in result["observer_outputs"]:
        if output["source_kind"] == "finish_summary":
            require(any(request["name"] == "agent_finish" and request["arguments"].get("summary") == output["text"]
                        for request in world["requests"]), "stored finish summary")
        elif output["output_id"] in texts or output["output_id"] in deltas:
            require(output["text"] == texts.get(output["output_id"], deltas.get(output["output_id"])), "raw assistant text")
        else:
            require(output["delivery_evidence"] == "turn_result_text_unattributed_to_item", "assistant source")
    boundaries = [event for event in events if event["kind"] == "admission_closed"]
    if boundaries:
        require(all(result["boundary"].get(key) == boundaries[0][key] for key in
                    ("termination_kind", "reason", "last_admitted_arrival_seq", "elapsed_seconds")), "boundary")
    receipt = result.get("initial_receipt")
    recording = attempt_recording(raw, thread_id=receipt["thread_id"] if receipt else None,
                                  turn_id=receipt["turn_id"] if receipt else None,
                                  record_calls=True, record_reasoning=True, record_compactions=True,
                                  attestations=result["runtime_metadata"].get("manifest_attestations", []))
    for key in ("reasoning_items", "runtime_recording"):
        require(result.get(key) == recording.get(key), key)
        require(payload.get(key) == recording.get(key), "archived " + key)
    observed = None
    cumulative = 0
    for call in recording["runtime_recording"]["model_calls"]:
        if any(type(call.get(key)) is not int or call[key] < 0 for key in ("input_tokens", "output_tokens")):
            break
        cumulative += call["input_tokens"] + call["output_tokens"]
        observed = cumulative
    require(result["usage"]["observed_total_tokens"] == observed, "per-call token totals")
    return world


def eligibility_inputs(result, fixture):
    v = fixture["truth"]["queue"]["focal_position"]
    served = [request for request in result["tool_requests"] if request["tool"] == "next_item"
              and (result_body(request.get("result") or {}).get("item") or {}).get("position") == v]
    # Dispatch was recorded before the response was sent. A compaction while
    # next_item was queued still preceded actual serving of the focal item.
    dispatched = [event["event_seq"] for event in result["events"] if event["kind"] == "tool_dispatch_recorded"
                  and any(event["arrival_seq"] == request["arrival_seq"] for request in served)]
    focal_seq = min(dispatched) if dispatched else None
    raw_seqs = [event["event_seq"] for event in result["events"] if event["kind"] == "runtime_event"]
    compactions = result.get("runtime_recording", {}).get("compaction_events", [])
    token_stops = [event for event in result["events"] if event["kind"] == "queue_token_stop_observed"]
    before = any(focal_seq is None or raw_seqs[event["raw_event_index"]] < focal_seq for event in compactions)
    return {"reached": bool(dispatched), "focal_served_event_seq": focal_seq,
            "compaction_before_focal": before, "compaction_after_focal": bool(compactions) and not before,
            "ineligibility_reason": "context_compacted" if before else None,
            "cap_ended": result["termination_kind"] == "per_trial_limit"
                or any(event["generation_active"] for event in token_stops),
            "token_stops": deepcopy(token_stops),
            "termination_kind": result["termination_kind"], "runtime_closed": result["runtime_closed"],
            "queue_reconciled": result["queue_reconciled"], "exposure_confirmed": result["exposure_confirmed"]}


def export_fields(result, fixture):
    recording = result.get("runtime_recording", {})
    calls = recording.get("model_calls", [])
    sums = {key: sum(call[key] for call in calls) if calls and all(type(call.get(key)) is int for call in calls)
            else None for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")}
    sums["uncached_input_tokens"] = (sums["input_tokens"] - sums["cached_input_tokens"]
                                     if sums["input_tokens"] is not None and sums["cached_input_tokens"] is not None else None)
    messages = {}
    for receipt in result["tool_receipts"]:
        for message in receipt["result"].get("room_events", []) + result_body(receipt["result"]).get("messages", []):
            messages[message["event_id"]] = message
    return {"world_events": deepcopy(result["world_state"]["events"]),
            "world_state": deepcopy(result["world_state"]), "instructions_hash": result["instructions_hash"],
            "reasoning_items": deepcopy(result.get("reasoning_items", [])),
            "observed_room_messages": [deepcopy(message) for message in result["world_state"]["messages"]
                                       if messages.get(message["event_id"]) == message],
            "runtime_recording": deepcopy(recording), "compaction_events": deepcopy(recording.get("compaction_events", [])),
            "per_call_usage": deepcopy(calls), "provider_usage": sums,
            "closure_texts": deepcopy(closure_outputs(result["observer_outputs"])),
            "eligibility_inputs": eligibility_inputs(result, fixture), "boundary": deepcopy(result["boundary"]),
            "primary_evidence_hash": content_hash(result["events"])}
