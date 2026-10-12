"""Offline byte-budget probe using only an isolated client and a loopback fake provider."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from ..runtime import SUPPORTED_CODEX_VERSION, RuntimeProtocolError
from .live_runtime import V11PeerRuntime
from .queue_protocol import QUEUE_OPTIONS
from .queue_schema import B, serialized_bytes


def budget_results(budget=B):
    results = []
    for pattern in ('"\\\n\t\x00', '\U0001f642\u6f22\u00e9'):
        result = {"result": {"status": "ok", "text": ""}, "room_events": []}
        empty = len(serialized_bytes(result))
        unit = len(serialized_bytes({"text": pattern})) - len(serialized_bytes({"text": ""}))
        count, padding = divmod(budget - empty, unit)
        result["result"]["text"] = pattern * count + "x" * padding
        text = serialized_bytes(result).decode("utf-8")
        if len(text.encode("utf-8")) != budget:
            raise ValueError("budget sample has an unexpected serialized size")
        results.append(text)
    return results


def check_capture(request, expected, call_id):
    outputs = [item for item in request.get("input", [])
               if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
    if len(outputs) != 1:
        raise RuntimeProtocolError("budget result has no unique next-request output")
    output = outputs[0].get("output")
    if isinstance(output, list):
        output = "".join(item.get("text", "") for item in output if isinstance(item, dict))
    if output != expected:
        raise RuntimeProtocolError("budget result changed in the next provider request")


def _sse(number):
    item = {"id": f"fc-{number}", "type": "function_call", "call_id": f"budget-{number}",
            "name": "budget_result", "arguments": "{}", "status": "completed"}
    response = {"id": f"resp-{number}", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6-sol", "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "output_index": 0,
               "item": {**item, "arguments": "", "status": "in_progress"}},
              {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": 0,
               "delta": "{}"},
              {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": 0,
               "arguments": "{}"},
              {"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": response}]
    return "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()


async def check_budget(client="codex", *, timeout=60):
    """Never load authentication. The isolated client can reach only the local probe endpoint."""
    runtime = V11PeerRuntime(model="gpt-6-sol", reasoning_effort="low", codex_executable=str(client),
                             options=QUEUE_OPTIONS)
    from ..peer_reporting.live import read_codex_version

    version = await read_codex_version(runtime)
    if not version.endswith(SUPPORTED_CODEX_VERSION):
        raise RuntimeProtocolError("budget check requires pinned Codex " + SUPPORTED_CODEX_VERSION)
    samples, captures = budget_results(), []
    done = asyncio.get_running_loop().create_future()
    requests = []

    async def provider(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            if not 0 <= length <= 16 * 1024 * 1024:
                raise RuntimeProtocolError("budget probe request is outside the size bound")
            request = json.loads(await reader.readexactly(length))
            number = len(requests)
            requests.append(request)
            if number:
                check_capture(request, samples[number - 1], f"budget-{number - 1}")
                captures.append({"sample": number - 1, "bytes": B, "unchanged": True,
                                 "sha256": hashlib.sha256(samples[number - 1].encode()).hexdigest()})
            complete = number == len(samples)
            body = b'{"error":{"message":"budget captured","type":"invalid_request_error"}}' if complete else _sse(number)
            status, mime = (b"400 Bad Request", b"application/json") if complete else (b"200 OK", b"text/event-stream")
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + mime + b"\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            if complete and not done.done():
                done.set_result(True)
        except Exception as error:
            if not done.done():
                done.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(provider, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="work-") as temporary:
        home = Path(temporary)
        overrides = ('model_provider="queue_budget"', 'model_providers.queue_budget.name="Output budget check"',
                     f'model_providers.queue_budget.base_url="http://127.0.0.1:{port}/v1"',
                     'model_providers.queue_budget.env_key="QUEUE_BUDGET_TOKEN"',
                     'model_providers.queue_budget.wire_api="responses"',
                     'model_providers.queue_budget.requires_openai_auth=false',
                     'model_providers.queue_budget.supports_websockets=false')
        proc = None
        pump = None
        try:
            proc = await runtime._launch(home=home, env_extra={"QUEUE_BUDGET_TOKEN": "offline-probe"}, overrides=overrides)
            await runtime._probe_call(proc, 1, "initialize", runtime._initialize_params())
            await runtime._send({"method": "initialized", "params": {}}, proc)
            specs = [{"type": "function", "name": "budget_result", "description": "Return the probe text.",
                      "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
            thread = await runtime._probe_call(proc, 2, "thread/start", runtime._thread_params(specs, home))
            await runtime._send({"id": 3, "method": "turn/start", "params": runtime._turn_params(
                thread["thread"]["id"], "Call budget_result twice and finish.", "low")}, proc)

            async def read_client():
                number = 0
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        raise RuntimeProtocolError("budget client ended before the next request")
                    message = json.loads(line)
                    if message.get("method") == "item/tool/call":
                        if number >= len(samples) or message.get("params", {}).get("tool") != "budget_result":
                            raise RuntimeProtocolError("budget client requested an unexpected tool")
                        await runtime._send({"id": message["id"], "result": {"success": True,
                            "contentItems": [{"type": "inputText", "text": samples[number]}]}}, proc)
                        number += 1

            pump = asyncio.create_task(read_client())
            finished, _ = await asyncio.wait({done, pump}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                raise RuntimeProtocolError("budget check timed out")
            if pump in finished:
                await pump
            await done
        finally:
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if proc is not None:
                await runtime._stop_process(proc)
            server.close()
            await server.wait_closed()
    return {"passed": len(captures) == len(samples), "budget_bytes": B, "codex_version": version,
            "results": captures, "provider": "unauthenticated_loopback_fake_provider",
            "catalog_sha256": runtime.metadata["peer_catalog_override"]["catalog_sha256"],
            "live_model_calls": 0}
