"""Test-only fake Codex executable using an HTTP loopback provider."""

import http.client
import json
import os
import sys
from urllib.parse import urlsplit


def main():
    if "--version" in sys.argv:
        print("codex-cli 0.158.0")
        return
    url = next(argument.split("=", 1)[1].strip('"') for argument in sys.argv
               if argument.startswith("model_providers.queue_budget.base_url="))
    endpoint = urlsplit(url)
    request = {"input": []}

    def send(message):
        print(json.dumps(message), flush=True)

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            send({"id": message["id"], "result": {}})
        elif method == "thread/start":
            send({"id": message["id"], "result": {"thread": {"id": "budget-thread"}}})
        elif method == "turn/start":
            send({"id": message["id"], "result": {"turn": {"id": "budget-turn"}}})
        elif "result" in message:
            output = message["result"]["contentItems"][0]["text"]
            if os.environ.get("QUEUE_BUDGET_ALTER"):
                output = output[:-1]
            request["input"].append({"type": "function_call_output", "call_id": f"budget-{message['id'] - 10}",
                                      "output": output})
        else:
            continue
        if method not in ("turn/start", None):
            continue
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=20)
        connection.request("POST", endpoint.path + "/responses", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        if response.status != 200:
            return
        final = [json.loads(part[6:]) for part in body.splitlines() if part.startswith("data: ")][-1]
        call = final["response"]["output"][0]
        request["input"].append(call)
        number = int(call["call_id"].split("-")[1])
        send({"id": 10 + number, "method": "item/tool/call", "params": {"threadId": "budget-thread",
              "turnId": "budget-turn", "callId": call["call_id"], "tool": call["name"], "arguments": {}}})


if __name__ == "__main__":
    main()
