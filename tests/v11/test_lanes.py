"""Lanes, frozen caps, authorization records, the admission hold policy, and global slots. No model calls."""

import asyncio
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import seal
from swarm_auth_bench.peer_reporting_v11 import lanes

from .live_fakes import authorization, caps_record


def sealed_plan(**changes):
    return seal({"phase": "smoke", "caps_hash": "c" * 64, "maximum_live_calls": 12, "caps": caps_record(), **changes})


def archived(attempt_id, passed, reasons, settlement):
    return {"attempt_id": attempt_id, "check": {"passed": passed, "failure_reasons": reasons},
            "orchestrator": {"usage_settlement": {"status": settlement}}}


def test_lanes_are_every_model_at_each_effort_xhigh_first():
    order = lanes.lane_order()
    assert len(order) == 6 and len(set(order)) == 6
    assert [effort for _, effort in order] == ["xhigh"] * 3 + ["low"] * 3
    assert [lanes.lane_id(*pair) for pair in order][:2] == ["gpt-6-luna-xhigh", "gpt-6-sol-xhigh"]
    for bad in (("gpt-6-sol", "medium"), ("gpt-5", "xhigh")):
        with pytest.raises(ValueError):
            lanes.lane_id(*bad)


def test_caps_record_requires_frozen_status_protocol_tool_cap_and_bounded_concurrency():
    record = lanes.validate_caps_record(caps_record())
    assert record["global_max_concurrency"] == 6
    with pytest.raises(ValueError, match="frozen"):
        lanes.validate_caps_record(caps_record(caps_status="candidate"))
    assert lanes.validate_caps_record(caps_record(caps_status="candidate"), require_frozen=False)
    bad_tool_cap = caps_record()
    bad_tool_cap["trial"]["max_tool_requests_per_trial"] = 16
    for bad in (caps_record(global_max_concurrency=7), caps_record(global_max_concurrency=0),
                caps_record(global_max_concurrency=True), caps_record(lane_wall_seconds={"smoke": 10}),
                caps_record(protocol_id="peer-reporting-p1-v3"), caps_record(caps_status="pending"), bad_tool_cap,
                {**caps_record(), "extra": 1}):
        with pytest.raises(ValueError):
            lanes.validate_caps_record(bad, require_frozen=False)
    reservation_below_stop = caps_record()
    reservation_below_stop["trial"]["reserved_tokens_per_trial"] = 100
    with pytest.raises(ValueError, match="reservation"):
        lanes.validate_caps_record(reservation_below_stop)


def test_lane_caps_are_serial_with_one_reservation_of_headroom_above_the_planned_trials():
    caps = lanes.lane_caps(caps_record(), "collection", 336)
    assert caps["max_concurrency"] == 1
    assert caps["collection_observed_token_stop_target"] == 337 * 75000  # W09 N-c
    with pytest.raises(ValueError, match="exceed its planned reservations"):
        lanes.validate_token_headroom({**caps, "collection_observed_token_stop_target": 336 * 75000}, 336)
    assert caps["collection_wall_seconds"] == 3600 and caps["max_tool_requests_per_trial"] == 32
    with pytest.raises(ValueError):
        lanes.lane_caps(caps_record(), "collection", 0)


def test_authorization_must_be_sealed_approved_and_name_exactly_this_plan():
    plan = sealed_plan()
    record = lanes.validate_authorization(authorization(plan), plan)
    assert record["admission_cutoff"] < record["forced_stop_deadline"]
    refusals = [
        authorization(plan, live_plan_hash="a" * 64), authorization(plan, caps_hash="b" * 64),
        authorization(plan, phase="collection"), authorization(plan, maximum_live_calls=13),
        authorization(plan, authorization={"status": "proposed", "text": "x"}),
        authorization(plan, authorization={"status": "approved", "text": "  "}),
        authorization(plan, cutoff=7200, deadline=3600),
        authorization(plan, admission_cutoff_utc="2026-10-05T10:00:00"),
        authorization(plan, protocol_id="peer-reporting-p1-v3"),
    ]
    for bad in refusals:
        with pytest.raises(ValueError):
            lanes.validate_authorization(bad, plan)
    tampered = deepcopy(authorization(plan))
    tampered["maximum_live_calls"] = 99
    with pytest.raises(ValueError, match="seal"):
        lanes.validate_authorization(tampered, plan)
    extra = authorization(plan)
    extra = seal({**{key: value for key, value in extra.items() if key != "seal_hash"}, "note": "x"})
    with pytest.raises(ValueError, match="exactly"):
        lanes.validate_authorization(extra, plan)


def test_cutoff_must_precede_the_deadline_by_the_trial_wall_plus_drain():
    plan = sealed_plan()  # trial wall 60 s, drain 5 s
    assert lanes.validate_authorization(authorization(plan, cutoff=1000, deadline=1065), plan)
    with pytest.raises(ValueError, match="trial wall plus the drain"):
        lanes.validate_authorization(authorization(plan, cutoff=1000, deadline=1064), plan)
    with pytest.raises(ValueError, match="lacks the trial wall"):
        lanes.validate_authorization(authorization(plan), {**plan, "caps": {}})


def test_admission_holds_at_cutoff_deadline_and_stop_file_and_never_lift(tmp_path):
    now = [100.0]
    changes = []
    policy = lanes.AdmissionPolicy(admission_cutoff=200, forced_stop_deadline=300, wall_clock=lambda: now[0],
                                   hard_stop_files=[tmp_path / "HARD_STOP"], on_change=lambda: changes.append(1))
    assert policy.admission_check() is None
    now[0] = 200
    assert policy.admission_check()["holds"] == ["admission_cutoff"]
    assert not policy.force_stop_due()
    now[0] = 150  # a clock moving back never lifts a hold
    assert policy.admission_check() is not None
    (tmp_path / "HARD_STOP").touch()
    assert policy.force_stop_due() and policy.force_stop_reason() == "hard_stop"
    assert policy.admission_check()["holds"] == ["admission_cutoff", "hard_stop"]
    assert len(changes) == 2
    with pytest.raises(ValueError):
        lanes.AdmissionPolicy(admission_cutoff=300, forced_stop_deadline=300)


def test_every_stop_file_is_watched_and_soft_stops_never_truncate(tmp_path):
    policy = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1,
                                   stop_files=[tmp_path / "root-STOP", tmp_path / "flag-STOP"],
                                   hard_stop_files=[tmp_path / "root-HARD_STOP", tmp_path / "flag-HARD_STOP"])
    assert not policy.force_stop_due()
    (tmp_path / "flag-STOP").touch()
    assert not policy.force_stop_due() and policy.admission_check()["holds"] == ["soft_stop"]
    (tmp_path / "flag-HARD_STOP").touch()
    assert policy.force_stop_reason() == "hard_stop"
    assert policy.admission_check()["holds"] == ["soft_stop", "hard_stop"]
    deadline = lanes.AdmissionPolicy(admission_cutoff=10, forced_stop_deadline=20, wall_clock=lambda: 20)
    assert deadline.force_stop_reason() == "forced_stop_deadline"


def test_failed_check_or_unsettled_usage_holds_all_admission():
    policy = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    policy.accept_archived(archived("a", True, [], "settled"))
    assert policy.admission_check() is None
    policy.accept_archived(archived("b", False, ["runtime_closed", "usage_known"], "unresolved"))
    assert policy.holds == ["execution_check_failure:b", "unknown_final_usage:b"]
    assert policy.admission_check()["reason"] == "global_admission_hold"
    conflict = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    conflict.accept_archived(archived("c", True, [], "unresolved"))  # a settlement conflict alone still holds
    assert conflict.holds == ["unknown_final_usage:c"]


def test_bounded_settlement_alone_does_not_hold():
    policy = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    policy.accept_archived(archived("a", True, [], "bounded_by_reservation"))  # a behavioral transport check
    policy.accept_archived(archived("b", False, ["usage_known"], "bounded_by_reservation"))  # an unqualified probe
    assert policy.holds == []
    policy.accept_archived(archived("c", False, ["usage_known", "valid_close"], "bounded_by_reservation"))
    assert policy.holds == ["execution_check_failure:c"]
    assert lanes.attempt_hold_kinds(False, ["usage_known"], "unresolved") == ["execution_check_failure",
                                                                              "unknown_final_usage"]


def test_retained_lane_evidence_holds_on_failed_unknown_or_incomplete_rows():
    policy = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    clean = {"halted": {"reason": "global_admission_hold"}, "unreconciled_starts": [], "ledger": {},
             "entries": [{"attempt_id": "a", "status": "archived", "check_passed": True, "usage_total_tokens": 5,
                          "usage_settlement": "settled", "failure_reasons": []},
                         {"attempt_id": "g", "status": "archived", "check_passed": True,
                          "usage_total_tokens": 75000, "usage_settlement": "bounded_by_reservation",
                          "failure_reasons": []},
                         {"attempt_id": "b", "status": "unrun"},
                         {"attempt_id": "c", "status": "not_started_preflight_failed"}]}
    policy.accept_lane_report("lane", clean, retained=True)
    assert policy.holds == []
    dirty = {"halted": None, "unreconciled_starts": ["d"], "ledger": {"unresolved_reservations": ["e~r1"]},
             "entries": [{"attempt_id": "e", "status": "archived", "check_passed": True, "usage_total_tokens": None,
                          "usage_settlement": "unresolved", "failure_reasons": []},
                         {"attempt_id": "f", "status": "incomplete_interrupted"}]}
    policy.accept_lane_report("lane", dirty, retained=True)
    assert policy.holds == ["retained_unreconciled_start:d", "retained_unsettled_lane_reservation:lane",
                            "retained_failed_or_unknown_attempt:e", "retained_incomplete_attempt:f"]
    live = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    live.accept_lane_report("lane", {"halted": {"reason": "preflight_failed"}, "entries": []})
    assert live.holds == ["lane_halted:lane:preflight_failed"]


def test_a_retained_ledger_stop_holds_at_run_start():
    policy = lanes.AdmissionPolicy(admission_cutoff=10**12, forced_stop_deadline=10**12 + 1)
    ledger = {"stop_generation": True, "stop_reason": "collection_wall_limit", "unresolved_reservations": [],
              "active_reservations": []}
    finished = {"halted": None, "unreconciled_starts": [], "ledger": ledger, "entries": [
        {"attempt_id": "a", "status": "archived", "check_passed": True, "usage_total_tokens": 75000,
         "usage_settlement": "settled", "failure_reasons": []}]}
    policy.accept_lane_report("lane", finished, retained=True)
    policy.accept_lane_report("lane", {**finished, "entries": [{"attempt_id": "b", "status": "unrun"}]})
    assert policy.holds == []  # a finished lane, or the end-of-run report, does not hold on the flag alone
    policy.accept_lane_report("lane", {**finished, "entries": [{"attempt_id": "b", "status": "unrun"}]},
                              retained=True)
    assert policy.holds == ["retained_ledger_stop:lane:collection_wall_limit"]


async def test_global_slots_never_exceed_the_limit():
    slots = lanes.GlobalSlots(2)
    running = []

    async def attempt(number):
        async with slots.slot():
            running.append(number)
            assert slots.active <= 2
            await asyncio.sleep(0.01)

    await asyncio.gather(*(attempt(number) for number in range(7)))
    assert slots.peak == 2 and slots.active == 0 and slots.admitted == 7
    assert slots.try_acquire() and slots.try_acquire() and not slots.try_acquire()
    slots.release()
    slots.release()
    with pytest.raises(RuntimeError):
        slots.release()
    for bad in (0, 7, True, 2.0):
        with pytest.raises(ValueError):
            lanes.GlobalSlots(bad)
