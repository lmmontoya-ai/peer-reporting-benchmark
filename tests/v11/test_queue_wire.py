"""Pagination measures text emitted by the inherited runtime through a fake transport."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from swarm_auth_bench.peer_reporting_v11.queue_schema import B, serialized_bytes
from swarm_auth_bench.peer_reporting_v11.world import audit_state

from .live_fakes import FakeTransport
from .test_queue_world import post, world


class WireController:
    def __init__(self, queue):
        self.queue, self.next_result = queue, None

    async def request(self, request_id, params):
        result = self.next_result
        self.next_result = None
        if result is None:
            result = self.queue.dispatch(params["name"], params["arguments"])
        return {}, result, True

    def response_prepared(self, *args):
        pass

    def response_sent(self, *args):
        pass


class WireTransport(FakeTransport):
    async def _send(self, message, proc=None):
        self.sent = message

    async def read_page(self, cursor, limit=16, *, result=None):
        self._peer_controllers["wire"].next_result = result
        await self._peer_dynamic_tool(SimpleNamespace(thread_id="wire"), 1, {
            "name": "read_channel", "arguments": {"after_event_id": cursor, "limit": limit}})
        text = self.sent["result"]["contentItems"][0]["text"]
        return json.loads(text), text.encode("utf-8")


@pytest.mark.parametrize("text", ["\U0010ffff" * 1000, ('caf\u00e9"\\\n\x00' * 150)[:1000],
                                  "\x00" * 1000, '"\\' * 500])
async def test_pages_are_maximal_against_the_text_the_fake_transport_sends(tmp_path, text):
    w = world(tmp_path)
    for i in range(18):
        w.dispatch("send_message", post(text=text, request_id=f"post-{i}"))
    transport = WireTransport("gpt-6-sol", "low", [], tmp_path)
    transport._peer_controllers["wire"] = WireController(w)
    cursor, observed = None, []
    while True:
        result, wire = await transport.read_page(cursor)
        assert len(wire) <= B and serialized_bytes(result) == wire
        page = result["result"]
        assert page["messages"] and len(page["messages"]) <= 16
        observed.extend(page["messages"])
        assert observed == w.messages[:len(observed)]
        assert page["has_more"] == (len(observed) < len(w.messages))
        if page["has_more"] and len(page["messages"]) < 16:
            next_message = w.messages[len(observed)]
            candidate = deepcopy(result)
            candidate["result"]["messages"].append(next_message)
            candidate["result"].update(next_after_event_id=next_message["event_id"],
                                       has_more=len(observed) + 1 < len(w.messages))
            _, longer_wire = await transport.read_page(cursor, result=candidate)
            assert len(longer_wire) > B
        cursor = page["next_after_event_id"]
        assert cursor == observed[-1]["event_id"]
        if not page["has_more"]:
            break
    assert observed == w.messages
    assert audit_state(w.directory) == w.snapshot()


async def test_review_three_message_case_returns_all_messages_on_the_wire(tmp_path):
    w = world(tmp_path)
    for i in range(3):
        w.dispatch("send_message", post(text="\U0010ffff" * 1000, request_id=f"p{i}"))
    transport = WireTransport("gpt-6-sol", "low", [], tmp_path)
    transport._peer_controllers["wire"] = WireController(w)
    result, wire = await transport.read_page(None)
    assert len(wire) == 12693 < B
    assert result["result"]["messages"] == w.messages
    assert result["result"]["next_after_event_id"] == w.messages[-1]["event_id"]
    assert result["result"]["has_more"] is False
