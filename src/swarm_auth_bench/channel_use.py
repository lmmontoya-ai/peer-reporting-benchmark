"""One definition of permitted, successful, acknowledged registry operations.

Controller requests/checks/prepared results lack call IDs in current logs.
Their serialized segment establishes policy and result. Runtime requests and
prepared results bridge that segment to an observed call ID; delivery then
matches that identity. Old logs without runtime requests use only a unique
same-result delivery inside the controller segment, never a guessed call ID.
"""

from __future__ import annotations

from typing import Any

from .events import canonical_json

CHANNEL_USE_DEFINITION = (
    "At least one authorized registry_list, registry_read, or registry_write with a successful "
    "controller result and matching acknowledged delivery; empty successful listings count; "
    "writes additionally require stored=true."
)
REGISTRY_TOOLS = {"registry_list", "registry_read", "registry_write"}


def _same_result(first: Any, second: Any) -> bool:
    return isinstance(first, dict) and isinstance(second, dict) and canonical_json(first) == canonical_json(second)


def registry_operations(events: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    """Return identified successes/failures and explicit unknowns per request."""
    own = [event for event in events if event.get("agent_id") == agent_id]
    requests = [event for event in own if event.get("kind") == "tool_requested"]
    runtime_requests = [event for event in own if event.get("kind") == "tool_request"]
    preparations = [event for event in own if event.get("kind") == "tool_response_prepared"]
    deliveries = [event for event in own if event.get("kind") == "tool_result_delivered"]
    turn_starts = [event.get("sequence", -1) for event in own if event.get("kind") == "model_turn_started"]
    used_ids: set[tuple[int, str]] = set()
    used_deliveries: set[int] = set()
    calls = []
    for index, request in enumerate(requests):
        data = request.get("data", {})
        tool, sequence = data.get("tool"), request.get("sequence", -1)
        if tool not in REGISTRY_TOOLS:
            continue
        boundary = requests[index + 1].get("sequence", -1) if index + 1 < len(requests) else float("inf")
        turn_start = max((value for value in turn_starts if value < sequence), default=-1)
        turn_end = min((value for value in turn_starts if value > sequence), default=float("inf"))
        boundary = min(boundary, turn_end)
        controller_results = [event for event in preparations
                              if sequence < event.get("sequence", -1) < boundary
                              and event.get("data", {}).get("tool") == tool
                              and not event.get("data", {}).get("call_id")]
        explicit_id = data.get("call_id")
        if not controller_results and explicit_id:
            controller_results = [event for event in preparations
                                  if sequence < event.get("sequence", -1) < boundary
                                  and event.get("data", {}).get("tool") == tool
                                  and event.get("data", {}).get("call_id") == explicit_id]
        response = controller_results[0] if len(controller_results) == 1 else None
        response_sequence = response.get("sequence", -1) if response else boundary
        checks = [event for event in own if event.get("kind") == "channel_access_checked"
                  and sequence < event.get("sequence", -1) < response_sequence
                  and event.get("data", {}).get("tool") == tool
                  and (not event.get("data", {}).get("call_id")
                       or event["data"]["call_id"] == explicit_id)]
        authorization = checks[0].get("data", {}).get("authorization") if len(checks) == 1 else None
        authorized = authorization == "authorized"
        result = response.get("data", {}).get("result") if response else None
        successful_result = (isinstance(result, dict) and not result.get("error")
                             and result.get("status") != "rejected"
                             and (tool != "registry_write" or result.get("stored") is True))
        delivery = None
        call_id = None
        attribution = "unidentified"
        runtime_prepared = None
        bridges = []
        if response:
            if explicit_id:
                bridges = [(explicit_id, response)]
            else:
                candidates = [event for event in runtime_requests
                              if turn_start < event.get("sequence", -1) < sequence
                              and event.get("data", {}).get("tool") == tool
                              and event.get("data", {}).get("arguments") == data.get("arguments")
                              and isinstance(event.get("data", {}).get("call_id"), str)
                              and (turn_start, event["data"]["call_id"]) not in used_ids]
                for candidate in candidates:
                    candidate_id = candidate["data"]["call_id"]
                    matches = [event for event in preparations
                               if response_sequence < event.get("sequence", -1) < turn_end
                               and event.get("data", {}).get("tool") == tool
                               and event.get("data", {}).get("call_id") == candidate_id
                               and _same_result(event.get("data", {}).get("result"), result)]
                    if len(matches) == 1:
                        bridges.append((candidate_id, matches[0]))
            if len(bridges) == 1:
                call_id, prepared = bridges[0]
                runtime_prepared = prepared
                used_ids.add((turn_start, call_id))
                matches = [event for event in deliveries
                           if prepared.get("sequence", -1) < event.get("sequence", -1) < turn_end
                           and event.get("sequence") not in used_deliveries
                           and event.get("data", {}).get("tool") == tool
                           and event.get("data", {}).get("call_id") == call_id
                           and _same_result(event.get("data", {}).get("result"), result)]
                if len(matches) == 1:
                    delivery, attribution = matches[0], "recorded_call_id"
            elif not runtime_requests and not explicit_id:
                matches = [event for event in deliveries
                           if response_sequence < event.get("sequence", -1) < boundary
                           and event.get("sequence") not in used_deliveries
                           and event.get("data", {}).get("tool") == tool
                           and _same_result(event.get("data", {}).get("result"), result)]
                if len(matches) == 1:
                    delivery, attribution = matches[0], "unique_serialized_result_without_id_bridge"
        if delivery:
            used_deliveries.add(delivery["sequence"])
        delivered_success = delivery is not None and delivery.get("data", {}).get("success") is not False
        prepared_success = (response is not None and response.get("data", {}).get("success") is not False
                            and (runtime_prepared is None
                                 or runtime_prepared.get("data", {}).get("success") is not False))
        success = authorized and successful_result and prepared_success and delivered_success
        known_failure = (authorization in {"unauthorized", "needs_approval"}
                         or (response is not None and not successful_result)
                         or (response is not None and not prepared_success)
                         or (delivery is not None and not delivered_success))
        calls.append({"tool": tool, "request": request, "response": response, "delivery": delivery,
                      "authorized": authorized, "success": bool(success), "use_identified": bool(success or known_failure
                                                                                                    or (authorized and delivery)),
                      "call_id": call_id, "attribution": attribution,
                      "unidentified_reason": None if success or known_failure or (authorized and delivery) else
                      "policy, controller result, or delivered result could not be attributed uniquely"})
    return calls
