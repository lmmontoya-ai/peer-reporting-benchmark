"""Offline app-server wire fixture. No model, provider, auth or network access."""

import json
import sys


def main():
    fixture, case = json.loads(sys.argv[1]), sys.argv[2]
    scope = {"threadId": "thread-gpt-6-sol-low", "turnId": "turn-gpt-6-sol-low"}
    first = fixture["items"][0]["item_id"]
    operations = [("next_item", {}), ("submit_item", {"item_id": first,
        "answer": fixture["truth"]["queue"]["answers"][first], "request_id": "answer-1"}), ("next_item", {})]
    position = 0

    def send(message):
        print(json.dumps(message), flush=True)

    def notify(method, **params):
        send({"method": method, "params": {**scope, **params}})

    def completed(number, tokens):
        notify("rawResponse/completed", responseId="response-" + str(number), usage=None if tokens is None else {
            "inputTokens": tokens, "cachedInputTokens": 0, "outputTokens": 10,
            "reasoningOutputTokens": 0, "totalTokens": tokens + 10}, usageMetadata=None)

    def request():
        tool, arguments = operations[position]
        call_id = "call-" + str(position)
        item = {"type": "function_call", "id": "fc-" + str(position), "call_id": call_id,
                "name": tool, "arguments": json.dumps(arguments)}
        if position != 2 or case != "missing_binding":
            notify("rawResponseItem/completed", item=item,
                   **({"threadId": "foreign-thread"} if position == 2 and case == "foreign_item" else {}))
        send({"id": 100 + position, "method": "item/tool/call",
              "params": {**scope, "callId": call_id, "tool": tool, "arguments": arguments}})

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            send({"id": message["id"], "result": {}})
        elif method == "thread/start":
            send({"id": message["id"], "result": {"thread": {"id": scope["threadId"]}}})
        elif method == "turn/start":
            send({"id": message["id"], "result": {"turn": {"id": scope["turnId"]}}})
            owner = {"id": "owner", "type": "userMessage", "content": message["params"]["input"]}
            notify("item/started", item=owner)
            notify("item/completed", item={**owner, "status": "completed"})
            request()
        elif "result" in message:
            tool, arguments = operations[position]
            notify("item/completed", item={"type": "dynamicToolCall", "id": "call-" + str(position),
                "callId": "call-" + str(position), "tool": tool, "arguments": arguments,
                "status": "completed", **message["result"]})
            if position == 2:
                # This response requested v and continues generating after delivery.
                notify("item/completed", item={"id": "continued", "type": "agentMessage",
                                               "phase": "commentary", "text": "The focal record arrived."})
                if case == "failed_stream":
                    notify("error", error={"message": "Offline stream ended.", "codexErrorInfo": "other"}, willRetry=True)
                else:
                    completed(3, None if case == "missing_usage" else 123)
                # A later response has different usage and cannot replace the binding.
                completed(4, 987)
                notify("item/completed", item={"id": "closure", "type": "agentMessage",
                                               "phase": "final_answer", "text": "Queue processing completed."})
                notify("turn/completed", turn={"id": scope["turnId"], "status": "completed", "items": [], "error": None})
            else:
                completed(position + 1, 50 + position)
                position += 1
                request()
        elif method == "turn/interrupt":
            send({"id": message["id"], "result": {}})
            notify("turn/completed", turn={"id": scope["turnId"], "status": "interrupted", "items": [], "error": None})


if __name__ == "__main__":
    main()
