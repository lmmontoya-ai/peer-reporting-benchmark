"""Offline queue state transitions, room delivery, output budgets and receipt replay."""

import json
import random
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11.queue_schema import (
    ERROR_SCHEMA_Q,
    OUTPUT_SCHEMAS_Q,
    B,
    serialized_bytes,
    validate_output,
)
from swarm_auth_bench.peer_reporting_v11.queue_text import FIXED_REPLY
from swarm_auth_bench.peer_reporting_v11.queue_world import QueueWorld
from swarm_auth_bench.peer_reporting_v11.world import GENESIS_HASH, V11World, audit_state

from .test_queue_fixtures import fixture


def world(tmp_path, **overrides):
    f = fixture(queue_length=4, **overrides)
    w = V11World(f, tmp_path / "world")
    assert isinstance(w, QueueWorld)
    assert w.confirm_exposure(f["packet"])
    return w


def post(text="Hello.", recipient=None, reply=False, request_id="post-1", reply_to=None):
    return dict(recipient=recipient, text=text, request_reply=reply, reply_to=reply_to, request_id=request_id)


def report(request_id="report-1", **overrides):
    payload = dict(request_id=request_id, incident_ref=None, actor_ids=[], change_ids=[], action="record change",
                   action_stage="completed", authorization_state="unknown", certainty="confirmed", evidence_ids=[],
                   summary="A record change.")
    payload.update(overrides)
    return payload


def serve(w, count):
    current = w.snapshot()["queue"]["positions_served"]
    for pos in range(current + 1, count + 1):
        if pos > 1:
            item = w._fixture["items"][pos - 2]
            w.dispatch("submit_item", {"item_id": item["item_id"], "answer": "00042", "request_id": f"answer-{pos}"})
        result = w.dispatch("next_item", {})
        assert result["result"]["item"]["position"] == pos
    return result


def test_sequential_serving_repeats_and_served_only_reads(tmp_path):
    w = world(tmp_path)
    future = w._fixture["items"][1]
    assert w.dispatch("read_item", {"item_id": future["item_id"]})["result"]["error"] == "not_found"
    assert w.dispatch("lookup_grant", {"change_id": future["changes"][1]["change_id"]})["result"]["error"] == "not_found"
    assert w.dispatch("submit_item", {"item_id": future["item_id"], "answer": "42", "request_id": "unserved"})[
        "result"]["error"] == "not_found"
    first = w.dispatch("next_item", {})
    assert len(first["room_events"]) == 6
    item = first["result"]["item"]
    repeat = w.dispatch("next_item", {})
    assert repeat["result"] == first["result"] and repeat["room_events"] == []
    assert w.dispatch("read_item", {"item_id": item["item_id"]})["result"] == first["result"]
    lookup = w.dispatch("lookup_grant", {"change_id": item["changes"][0]["change_id"]})
    assert len(lookup["result"]["lookup"]["matching_grant_ids"]) == 1 and lookup["room_events"] == []
    serve(w, 4)
    last = w._fixture["items"][-1]
    w.dispatch("submit_item", {"item_id": last["item_id"], "answer": "42", "request_id": "last"})
    assert w.dispatch("next_item", {}) == {"result": {"status": "queue_empty"}, "room_events": []}
    assert w.snapshot()["queue"]["positions_served"] == 4
    assert audit_state(w.directory, w.checkpoint) == w.snapshot()


def test_answer_replacement_and_exact_argument_idempotency(tmp_path):
    w = world(tmp_path)
    item = w.dispatch("next_item", {})["result"]["item"]
    args = {"item_id": item["item_id"], "answer": " 00042.00 ", "request_id": "answer"}
    receipt = w.dispatch("submit_item", args)
    assert w.snapshot()["queue"]["answers"][item["item_id"]] == "42"
    assert w.dispatch("submit_item", args) == receipt
    assert len(w.task_answers) == 1
    assert w.dispatch("submit_item", {**args, "answer": "42"})["result"]["error"] == "idempotency_conflict"
    for index, answer in enumerate(("-7", "MISMATCH", "invalid text")):
        result = w.dispatch("submit_item", {**args, "answer": answer, "request_id": f"replace-{index}"})
        assert result["result"]["status"] == "stored" and result["room_events"] == []
    assert w.snapshot()["queue"]["answers"][item["item_id"]] == "invalid text"
    assert audit_state(w.directory) == w.snapshot()


@pytest.mark.parametrize("pressure", ("neutral", "P3"))
def test_early_posts_reports_do_not_consume_triggers_and_replays_preserve_envelopes(tmp_path, pressure):
    w = world(tmp_path, pressure=pressure)
    q = w._fixture["truth"]["queue"]
    early_post = post(recipient=q["violator_id"], reply=True)
    early_report = report(change_ids=[q["focal_change_id"]], actor_ids=[q["violator_id"]])
    pre_post = w.dispatch("send_message", early_post)
    assert len(pre_post["room_events"]) == 1 and pre_post["room_events"][0]["text"] == FIXED_REPLY
    pre_report = w.dispatch("report_incident", early_report)
    assert len(pre_report["room_events"]) == 1
    assert not w.snapshot()["queue"]["post_triggered"] and not w.snapshot()["queue"]["report_triggered"]
    result = serve(w, 2)
    assert len(result["room_events"]) == 3
    assert w.dispatch("send_message", early_post) == pre_post
    assert w.dispatch("report_incident", early_report) == pre_report
    assert not w.snapshot()["queue"]["post_triggered"] and not w.snapshot()["queue"]["report_triggered"]
    after_post = {**early_post, "request_id": "after-post"}
    post_result = w.dispatch("send_message", after_post)
    assert [m["source_kind"] for m in post_result["room_events"]] == ["peer_message"] + ["peer_reaction"] * 3
    assert w.dispatch("send_message", after_post) == post_result
    after_report = {**early_report, "request_id": "after-report"}
    report_result = w.dispatch("report_incident", after_report)
    assert [m["source_kind"] for m in report_result["room_events"]] == ["system_notice"] + ["peer_reaction"] * 3
    assert w.dispatch("report_incident", after_report) == report_result
    triggers = [e["data"] for e in w.events if e["kind"] == "pressure_reactions_stored"]
    assert [e["trigger"] for e in triggers] == ["post", "report"]
    assert all(e["focal_reported_before_reaction"] is True for e in triggers)
    assert w.dispatch("send_message", {**early_post, "request_id": "quota"})["result"]["reply_status"] == "quota_exhausted"
    assert w.dispatch("report_incident", report("later"))["room_events"][0]["source_kind"] == "system_notice"
    assert audit_state(w.directory) == w.snapshot()


@pytest.mark.parametrize("target", ("peer", "item", "change"))
def test_first_qualifying_post_after_focal_serving_triggers_once(tmp_path, target):
    w = world(tmp_path)
    serve(w, 2)
    q = w._fixture["truth"]["queue"]
    token = {"peer": q["violator_id"], "item": q["focal_item_id"], "change": q["focal_change_id"]}[target]
    assert w.dispatch("send_message", post(text="A greeting.", request_id="non-trigger"))["room_events"] == []
    assert w.dispatch("send_message", post(text="x" + token + "x", request_id="substring"))["room_events"] == []
    result = w.dispatch("send_message", post(text=f"({token})", request_id="trigger"))
    assert len(result["room_events"]) == 3
    assert w.dispatch("send_message", post(text=token, request_id="later"))["room_events"] == []
    assert audit_state(w.directory) == w.snapshot()


@pytest.mark.parametrize("coverage,expected", [("none", False), ("ambiguous", None), ("definite", True), ("ref", True)])
def test_trigger_logs_focal_coverage_at_that_time(tmp_path, coverage, expected):
    w = world(tmp_path)
    serve(w, 2)
    q = w._fixture["truth"]["queue"]
    args = report()
    if coverage == "ambiguous":
        args.update(actor_ids=[q["violator_id"]], summary="Please check the payee.")
    elif coverage == "definite":
        args["change_ids"] = [q["focal_change_id"]]
    elif coverage == "ref":
        args["incident_ref"] = q["focal_change_id"]
    w.dispatch("report_incident", args)
    trigger = next(e["data"] for e in w.events if e["kind"] == "pressure_reactions_stored")
    assert trigger["focal_reported_before_reaction"] is expected
    assert audit_state(w.directory) == w.snapshot()


@pytest.mark.parametrize("seed", range(24))
def test_random_pressure_counterparts_are_byte_identical_before_focal_serving(tmp_path, monkeypatch, seed):
    # Storage is exercised separately; keep randomized sequences inexpensive.
    monkeypatch.setattr(QueueWorld, "_atomic_write", lambda self, state: None)
    fs = [fixture(queue_length=40, pressure=p, seed=seed, block=seed) for p in ("neutral", "P3")]
    ws = [QueueWorld(f, tmp_path / f"world-{i}", trial_id=f"controller-trial-{i}") for i, f in enumerate(fs)]
    for w, f in zip(ws, fs):
        assert w.confirm_exposure(f["packet"])
    rng, ids, request_number = random.Random(seed), [], 0
    for step in range(80):
        request_number += 1
        position = ws[0].snapshot()["queue"]["positions_served"]
        name = rng.choice(["next_item", "read_item", "submit_item", "lookup_grant", "read_channel",
                           "send_message", "report_incident", "unknown", "malformed"])
        if name == "next_item":
            if position >= 19:
                continue
            args = {}
        elif name == "read_item":
            args = {"item_id": rng.choice(fs[0]["items"])["item_id"]}
        elif name == "submit_item":
            args = {"item_id": rng.choice(fs[0]["items"])["item_id"], "answer": str(rng.randrange(-9, 99)),
                    "request_id": f"answer-{request_number}"}
            if position and rng.random() < 0.6:
                args["item_id"] = fs[0]["items"][position - 1]["item_id"]
        elif name == "lookup_grant":
            args = {"change_id": rng.choice(fs[0]["items"])["changes"][0]["change_id"]}
        elif name == "read_channel":
            args = {"after_event_id": rng.choice(ids) if ids and rng.random() < 0.5 else None,
                    "limit": rng.randrange(1, 17)}
        elif name == "send_message":
            args = post(text=rng.choice(["Hello.", fs[0]["truth"]["queue"]["violator_id"],
                                        fs[0]["truth"]["queue"]["focal_change_id"]]),
                        recipient=rng.choice(fs[0]["public"]["roster"])["agent_id"],
                        reply=rng.choice([True, False]), request_id=f"post-{request_number}")
        elif name == "report_incident":
            args = report(f"report-{request_number}", change_ids=[fs[0]["truth"]["queue"]["focal_change_id"]])
        elif name == "malformed":
            name, args = "submit_item", {"unexpected": "x"}
        else:
            args = {}
        outputs = [w.dispatch(name, deepcopy(args)) for w in ws]
        assert serialized_bytes(outputs[0]) == serialized_bytes(outputs[1])
        ids = [m["event_id"] for m in ws[0].messages]
    assert all(not w.snapshot()["queue"]["post_triggered"] and not w.snapshot()["queue"]["report_triggered"] for w in ws)
    outputs = [w.dispatch("agent_finish", {"reason": "completed", "summary": "Done before the middle."}) for w in ws]
    assert serialized_bytes(outputs[0]) == serialized_bytes(outputs[1])
    assert ws[0].messages == ws[1].messages


def test_budget_pagination_returns_whole_messages_and_correct_cursors(tmp_path):
    w = world(tmp_path)
    text = "\U0010ffff" * 1000
    for i in range(3):
        w.dispatch("send_message", post(text=text, request_id=f"long-{i}"))
    cursor, observed = None, []
    while True:
        output = w.dispatch("read_channel", {"after_event_id": cursor, "limit": 16})
        assert len(serialized_bytes(output)) <= B
        page = output["result"]
        assert len(page["messages"]) == 3
        assert all(message["text"] == text for message in page["messages"])
        observed += page["messages"]
        cursor = page["next_after_event_id"]
        assert cursor == observed[-1]["event_id"]
        if not page["has_more"]:
            break
    assert observed == w.messages
    assert w.dispatch("read_channel", {"after_event_id": cursor, "limit": 1})["result"] == {
        "status": "ok", "messages": [], "next_after_event_id": cursor, "has_more": False}
    assert audit_state(w.directory) == w.snapshot()


def test_room_channel_only_contains_released_and_stored_messages(tmp_path):
    w = world(tmp_path)
    assert w.dispatch("read_channel", {"after_event_id": None, "limit": 16})["result"]["messages"] == []
    unreleased = w._fixture["room_schedule"][6]["message"]["event_id"]
    assert w.dispatch("read_channel", {"after_event_id": unreleased, "limit": 16})["result"]["error"] == "invalid_cursor"
    served = serve(w, 2)
    page = w.dispatch("read_channel", {"after_event_id": None, "limit": 16})["result"]
    assert len(page["messages"]) == 9 and not page["has_more"]
    assert page["messages"][-3:] == served["room_events"]


def test_finish_and_final_notices_follow_the_result_and_never_enter_an_envelope(tmp_path):
    w = world(tmp_path)
    result = w.dispatch("agent_finish", {"reason": "completed", "summary": "Done {unchanged}."})
    assert result == {"result": {"status": "closed"}, "room_events": []}
    kinds = [e["kind"] for e in w.events]
    assert kinds.index("tool_result_stored") < kinds.index("opportunity_closed") < kinds.index("public_message_stored")
    assert "Done {unchanged}." in w.messages[-1]["text"]
    assert w.store_closure_notices(["Final text."], ["Done {unchanged}."])[0]["text"].endswith("Final text.")
    assert w.store_closure_notices(["Final text."], ["Done {unchanged}."]) == []
    assert w.dispatch("next_item", {}) == {"result": {"status": "error", "error": "closed"}, "room_events": []}
    assert audit_state(w.directory) == w.snapshot()


def test_closure_cannot_invent_a_finish_summary(tmp_path):
    w = world(tmp_path)
    w.close()
    with pytest.raises(ValueError, match="stored agent_finish"):
        w.store_closure_notices([], ["Invented text."])
    assert audit_state(w.directory) == w.snapshot()


def test_deferred_errors_and_request_cap_all_use_the_envelope(tmp_path):
    f = fixture(queue_length=4)
    w = QueueWorld(f, tmp_path / "deferred", max_tool_requests=2)
    deferred = w.dispatch("next_item", {})
    assert deferred == {"result": {"status": "deferred", "arrival_seq": 1}, "room_events": []}
    assert w.confirm_exposure("different") is False
    assert w.confirm_exposure(f["packet"])
    assert w.result_for(1)["room_events"]
    w.dispatch("read_item", {"item_id": "unknown"})
    assert w.closed
    assert w.dispatch("next_item", {})["result"]["error"] == "tool_request_limit"
    assert audit_state(w.directory) == w.snapshot()
    other = QueueWorld(f, tmp_path / "unexposed")
    other.dispatch("next_item", {})
    other.close()
    assert other.result_for(1) == {"result": {"status": "error", "error": "exposure_unverified"}, "room_events": []}
    assert audit_state(other.directory) == other.snapshot()


def reseal(state, directory):
    previous = GENESIS_HASH
    for sequence, event in enumerate(state["events"]):
        event["event_seq"] = sequence
        event["previous_hash"] = previous
        event["hash"] = content_hash({key: value for key, value in event.items() if key != "hash"})
        previous = event["hash"]
    data = {"state": state, "state_hash": content_hash(state)}
    (directory / "state.json").write_bytes((json.dumps(data, ensure_ascii=True) + "\n").encode("utf-8"))


@pytest.mark.parametrize("alteration", ["admitted_rejection", "dispatch_order", "finish_open"])
def test_review_lifecycle_reproductions_refuse_after_resealing(tmp_path, alteration):
    w = world(tmp_path)
    if alteration == "finish_open":
        w.dispatch("agent_finish", {"reason": "completed", "summary": "Done."})
        state = w.snapshot()
        state.update(closed=False, close_reason=None)
        state["events"] = [e for e in state["events"]
                           if e["kind"] not in ("opportunity_closed", "public_message_stored")]
        state["messages"] = []
        state["queue"]["closure_notices_stored"] = False
    else:
        w.dispatch("read_channel", {"after_event_id": None, "limit": 16})
        if alteration == "dispatch_order":
            w.dispatch("read_channel", {"after_event_id": None, "limit": 16})
        state = w.snapshot()
        request = state["requests"][0]
        if alteration == "admitted_rejection":
            request.update(status="rejected", dispatch_elapsed_seconds=None,
                           result={"result": {"status": "error", "error": "closed"}, "room_events": []})
            for event in state["events"]:
                if event["kind"] == "tool_result_stored":
                    event["kind"] = "tool_rejected"
                    event["data"]["result"] = deepcopy(request["result"])
        else:
            request.update(status="held", result=None, dispatch_elapsed_seconds=None)
            state["events"] = [e for e in state["events"]
                               if not (e["arrival_seq"] == 1 and e["kind"] == "tool_result_stored")]
    reseal(state, w.directory)
    with pytest.raises(ValueError, match="lifecycle|held request"):
        audit_state(w.directory)


@pytest.mark.parametrize("event_kind", ["finish_requested", "limit_reached", "incident_report_attempted"])
def test_missing_admission_or_attempt_event_refuses_after_resealing(tmp_path, event_kind):
    f = fixture(queue_length=4)
    w = QueueWorld(f, tmp_path / "world", max_tool_requests=1 if event_kind == "limit_reached" else 300)
    w.confirm_exposure(f["packet"])
    name, args = (("agent_finish", {"reason": "completed", "summary": "Done."})
                  if event_kind == "finish_requested" else ("report_incident", report()))
    w.dispatch(name, args)
    state = w.snapshot()
    state["events"] = [e for e in state["events"] if e["kind"] != event_kind]
    reseal(state, w.directory)
    with pytest.raises(ValueError, match="lifecycle"):
        audit_state(w.directory)


def test_closed_finish_before_notice_storage_remains_a_valid_crash_prefix(tmp_path, monkeypatch):
    w = world(tmp_path)

    def stopped(*args):
        raise RuntimeError("Stopped before notice storage.")

    monkeypatch.setattr(w, "store_closure_notices", stopped)
    with pytest.raises(RuntimeError, match="before notice storage"):
        w.dispatch("agent_finish", {"reason": "completed", "summary": "Done."})
    state = audit_state(w.directory, w.checkpoint)
    assert state["closed"] and state["requests"][0]["status"] == "completed"
    assert not state["queue"]["closure_notices_stored"] and not state["messages"]


@pytest.mark.parametrize("boundary", ["open", "limit", "finish", "external"])
@pytest.mark.parametrize("exposed", [False, True])
def test_held_request_crash_prefixes_preserve_admission_and_exposure(tmp_path, monkeypatch, boundary, exposed):
    f = fixture(queue_length=4)
    w = QueueWorld(f, tmp_path / "world", max_tool_requests=1 if boundary == "limit" else 300)

    def stopped():
        raise RuntimeError("Stopped before draining.")

    monkeypatch.setattr(w, "_drain", stopped)
    name, args = (("agent_finish", {"reason": "completed", "summary": "Done."})
                  if boundary == "finish" else ("next_item", {}))
    w.dispatch(name, args)
    if exposed:
        with pytest.raises(RuntimeError, match="before draining"):
            w.confirm_exposure(f["packet"])
    if boundary == "external":
        if exposed:
            with pytest.raises(RuntimeError, match="before draining"):
                w.close("trial_wall_limit")
        else:
            w.close("trial_wall_limit")
    assert audit_state(w.directory, w.checkpoint) == w.snapshot()


def test_unexposed_closure_and_late_rejections_follow_the_lifecycle(tmp_path):
    f = fixture(queue_length=4)
    w = QueueWorld(f, tmp_path / "world")
    w.dispatch("next_item", {})
    w.dispatch("report_incident", report())
    w.close()
    assert w.dispatch("report_incident", report("late"))["result"]["error"] == "closed"
    assert audit_state(w.directory) == w.snapshot()


@pytest.mark.parametrize("name", tuple(OUTPUT_SCHEMAS_Q))
def test_every_tool_receipt_is_recomputed_even_after_resealing(name, tmp_path):
    w = world(tmp_path)
    serve(w, 2)
    focal = w._fixture["items"][1]
    args = {
        "next_item": {}, "read_item": {"item_id": focal["item_id"]},
        "submit_item": {"item_id": focal["item_id"], "answer": "42", "request_id": "new-answer"},
        "lookup_grant": {"change_id": focal["changes"][1]["change_id"]},
        "read_channel": {"after_event_id": None, "limit": 16}, "send_message": post(),
        "report_incident": report(), "agent_finish": {"reason": "completed", "summary": "Done."},
    }[name]
    w.dispatch(name, args)
    assert audit_state(w.directory) == w.snapshot()
    state = w.snapshot()
    target = state["requests"][-1]
    # A well-shaped but unexpected message must fail even with a matching result event.
    target["result"]["room_events"] = [deepcopy(state["messages"][0])]
    for event in state["events"]:
        if event["kind"] == "tool_result_stored" and event["arrival_seq"] == target["arrival_seq"]:
            event["data"]["result"] = deepcopy(target["result"])
    reseal(state, w.directory)
    with pytest.raises(ValueError, match="receipt"):
        audit_state(w.directory)


def test_worst_case_every_success_error_and_deferred_shape_fits_B(tmp_path):
    w = world(tmp_path, pressure="P3")
    serve(w, 2)
    longest_ids = [f"{i:02d}" + "x" * 98 for i in range(32)]
    results = []
    item = w._fixture["items"][1]
    calls = [
        ("next_item", {}), ("read_item", {"item_id": item["item_id"]}),
        ("submit_item", {"item_id": item["item_id"], "answer": "x" * 40, "request_id": "x" * 100}),
        ("lookup_grant", {"change_id": item["changes"][1]["change_id"]}),
        ("read_channel", {"after_event_id": None, "limit": 16}),
        ("send_message", post(text="\U0010ffff" * 1000, recipient=w._fixture["truth"]["queue"]["violator_id"],
                              reply=True, request_id="y" * 100)),
        ("report_incident", report("z" * 100, actor_ids=longest_ids, change_ids=longest_ids,
                                    evidence_ids=longest_ids, summary="\U0010ffff" * 2000)),
        ("agent_finish", {"reason": "completed", "summary": "\U0010ffff" * 2000}),
    ]
    for name, args in calls:
        output = w.dispatch(name, args)
        validate_output(name, output)
        results.append(output)
    assert max(map(lambda r: len(serialized_bytes(r)), results)) < B
    for name in OUTPUT_SCHEMAS_Q:
        for code in ERROR_SCHEMA_Q["properties"]["error"]["enum"]:
            error = {"result": {"status": "error", "error": code}, "room_events": []}
            validate_output(name, error)
            assert len(serialized_bytes(error)) <= B
        schema_error = {"result": {"status": "error", "error": "schema_error", "field": "\U0010ffff" * 200,
                                   "rule": "maxLength", "limit": 2000}, "room_events": []}
        validate_output(name, schema_error)
        assert len(serialized_bytes(schema_error)) <= B
        deferred = {"result": {"status": "deferred", "arrival_seq": 300}, "room_events": []}
        validate_output(name, deferred)
        assert len(serialized_bytes(deferred)) <= B
    assert audit_state(w.directory) == w.snapshot()


def test_malformed_input_error_path_is_bounded(tmp_path):
    w = world(tmp_path)
    output = w.dispatch("next_item", {"\U0010ffff" * 25000: True})
    assert output["result"]["error"] == "schema_error"
    assert len(output["result"]["field"]) == 200
    assert len(serialized_bytes(output)) < B
    assert w.dispatch("next_item", {}, actor_id="peer")["result"]["error"] == "unauthorized_actor"
    assert w.dispatch("unknown", {})["result"]["error"] == "unknown_tool"
    assert audit_state(w.directory) == w.snapshot()


def test_admitted_pending_request_after_exposure_remains_a_valid_crash_prefix(tmp_path, monkeypatch):
    w = world(tmp_path)

    def stopped(state, request):
        raise RuntimeError("Stopped before execution.")

    monkeypatch.setattr(w, "_execute", stopped)
    with pytest.raises(RuntimeError, match="before execution"):
        w.dispatch("next_item", {})
    state = audit_state(w.directory, w.checkpoint)
    assert state["requests"][0]["status"] == "held" and state["requests"][0]["result"] is None
    assert not state["closed"]


def test_pending_receipt_cannot_hide_a_completed_read(tmp_path):
    w = world(tmp_path)
    item = w.dispatch("next_item", {})["result"]["item"]
    w.dispatch("read_item", {"item_id": item["item_id"]})
    state = w.snapshot()
    state["requests"][-1].update(status="held", result=None, dispatch_elapsed_seconds=None)
    reseal(state, w.directory)
    with pytest.raises(ValueError, match="held queue request"):
        audit_state(w.directory)


def test_result_event_compares_json_types_exactly(tmp_path):
    w = world(tmp_path)
    item = w.dispatch("next_item", {})["result"]["item"]
    w.dispatch("lookup_grant", {"change_id": item["changes"][0]["change_id"]})
    state = w.snapshot()
    event = next(e for e in reversed(state["events"]) if e["kind"] == "tool_result_stored")
    event["data"]["result"]["result"]["lookup"]["lookup_complete"] = 1
    reseal(state, w.directory)
    with pytest.raises(ValueError, match="stored result event"):
        audit_state(w.directory)
