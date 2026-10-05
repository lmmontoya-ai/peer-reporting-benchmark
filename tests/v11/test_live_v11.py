"""v1.1 lanes and live phases with fake runtimes and the fake v1.1 bundle. No model or provider call."""

import shutil
import time
from pathlib import Path

import pytest

from swarm_auth_bench.long_events import iter_events
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.lanes import lane_id

from .live_fakes import (
    Harness,
    authorization,
    caps_record,
    compat_fixture,
    compat_root,
    fake_bundle,
    qualified_root,
    qualifier_steps,
    smoke_root,
    study_rows,
    write_study,
)
from .live_fakes import run_phase as run


def journal(lane_dir):
    return list(iter_events(Path(lane_dir) / "journal.jsonl"))


def kinds(lane_dir):
    return [record["kind"] for record in journal(lane_dir)]


class Tracker:
    """Counts transports between authenticated session start and close."""

    def __init__(self, root):
        self.root, self.active, self.peak, self.started_before_session = Path(root), set(), 0, []

    def __call__(self, point, runtime):
        if point == "thread/start":
            starts = [record for record in journal(self.root / "lanes" / lane_id(runtime.model, runtime.reasoning_effort))
                      if record["kind"] == "attempt_started"]
            self.started_before_session.append(len(starts))
            self.active.add(id(runtime))
            self.peak = max(self.peak, len(self.active))
        elif point == "close":
            self.active.discard(id(runtime))


@pytest.fixture(scope="module")
def qualified(tmp_path_factory):
    trackers = []

    def tracker(root):
        trackers.append(Tracker(root))
        return trackers[-1]

    root, plan, status, harness = qualified_root(tmp_path_factory.mktemp("qualified"), observe=tracker)
    return root, plan, status, harness, trackers[0]


def test_compatibility_plan_has_one_tool_exercise_call_per_model_and_effort(tmp_path):
    root, plan = compat_root(tmp_path / "compat")
    assert plan["maximum_live_calls"] == 6 and len(plan["lanes"]) == 6
    assert plan["tool_manifest_hash"] == fake_bundle().tool_manifest_hash
    assert plan["source"]["kind"] == "compatibility_probe" and plan["behavioral_observation"] is False
    for lane in plan["lanes"]:
        lane_plan = read_sealed(root / lane["path"] / "phase-plan.json")
        (entry,) = lane_plan["planned_order"]
        assert lane_plan["reasoning_effort"] == lane["reasoning_effort"] == entry["reasoning_effort"]
        assert lane_plan["caps"]["max_concurrency"] == 1 and lane_plan["caps"]["max_tool_requests_per_trial"] == 32
        assert entry["instructions"].endswith(v11_live.QUALIFIER_BLOCK) and "operation_ids" in entry["instructions"]
        assert entry["world_mode"] == "normal" and entry["prompt_condition"] == "neutral"
        assert kinds(root / lane["path"]) == ["phase_sealed"]
    assert v11_live.verify_live_root(root, bundle=fake_bundle())["live_model_call_starts"] == 0


def test_compatibility_qualifies_all_six_lanes_with_their_own_effort(qualified):
    root, plan, status, harness, tracker = qualified
    assert status["status"] == "complete" and status["holds"] == []
    assert status["qualified_lanes"] == sorted(lane["lane_id"] for lane in plan["lanes"])
    assert status["live_model_call_starts"] == 6 and status["peak_active_attempts"] <= 6
    assert sorted((runtime.model, runtime.reasoning_effort, tuple(runtime.turn_efforts))
                  for runtime in harness.created) == sorted(
        (lane["model"], lane["reasoning_effort"], (lane["reasoning_effort"],)) for lane in plan["lanes"])
    assert [runtime.probe_calls for runtime in harness.created] == [1] * 6
    assert tracker.started_before_session == [1] * 6  # attempt_started was durable before the session start
    for lane in plan["lanes"]:
        sequence = kinds(root / lane["path"])
        assert sequence.index("preflight_passed") < sequence.index("reservation_admitted") < sequence.index(
            "attempt_started") < sequence.index("usage_observed") < sequence.index("attempt_archived")
        (attempt,) = (root / lane["path"] / "attempts").iterdir()
        payload = read_sealed(attempt / "attempt.json")
        assert payload["reasoning_effort"] == lane["reasoning_effort"] and payload["world_mode"] == "normal"
        assert payload["check"]["classification"] == "qualified"
        assert payload["check"]["checks"]["operation_ids_exercised"] is True
    assert read_sealed(next((root / "authorizations").iterdir()))["live_plan_hash"] == plan["seal_hash"]


async def test_completed_phase_resume_starts_nothing(qualified, tmp_path):
    root = Path(shutil.copytree(qualified[0], tmp_path / "compat", ignore=shutil.ignore_patterns("*.lock")))
    plan = qualified[1]
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    status = await run(root, plan, harness)
    assert harness.created == [] and status["status"] == "complete" and status["live_model_call_starts"] == 6


async def test_global_concurrency_limit_bounds_active_attempts(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=2))
    sample = compat_fixture(root, plan)
    tracker = Tracker(root)
    harness = Harness(tmp_path / "homes", lambda model, effort: [("sleep", 0.05), *qualifier_steps(sample)],
                      observe=tracker)
    status = await run(root, plan, harness)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 6
    assert tracker.peak == 2 and status["peak_active_attempts"] == 2


async def test_unknown_final_usage_holds_all_new_admission_and_every_later_run(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=1))
    sample = compat_fixture(root, plan)

    def script(model, effort):
        return qualifier_steps(sample, usage=None if (model, effort) == ("gpt-6-luna", "xhigh") else 4321)

    harness = Harness(tmp_path / "homes", script)
    status = await run(root, plan, harness)
    assert [(runtime.model, runtime.reasoning_effort) for runtime in harness.created] == [("gpt-6-luna", "xhigh")]
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    attempt = "compat-v1-gpt-6-luna-xhigh-live-1"
    assert f"unknown_final_usage:{attempt}" in status["holds"]
    assert f"execution_check_failure:{attempt}" in status["holds"]
    held_lanes = [lane for lane in plan["lanes"] if lane["lane_id"] != "gpt-6-luna-xhigh"]
    assert all("admission_held" in kinds(root / lane["path"]) for lane in held_lanes)
    assert all("attempt_started" not in kinds(root / lane["path"]) for lane in held_lanes)
    again = Harness(tmp_path / "homes-2", script)
    status = await run(root, plan, again)
    assert again.created == [] and status["status"] == "held"
    assert f"retained_failed_or_unknown_attempt:{attempt}" in status["holds"]


async def test_failed_execution_check_holds_admission_without_retry(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=1))
    sample = compat_fixture(root, plan)

    def script(model, effort):
        skip = ("report_incident",) if (model, effort) == ("gpt-6-sol", "xhigh") else ()
        return qualifier_steps(sample, skip=skip)

    harness = Harness(tmp_path / "homes", script)
    status = await run(root, plan, harness)
    assert [lane_id(runtime.model, runtime.reasoning_effort) for runtime in harness.created] == [
        "gpt-6-luna-xhigh", "gpt-6-sol-xhigh"]
    assert status["holds"] == ["execution_check_failure:compat-v1-gpt-6-sol-xhigh-live-1",
                               "failed_or_unknown_attempt:compat-v1-gpt-6-sol-xhigh-live-1"]
    report = v11_live.verify_live_root(root, bundle=fake_bundle())
    sol = report["lanes"]["gpt-6-sol-xhigh"]["entries"][0]
    assert sol["classification"] == "protocol_incompatibility_or_check_incomplete"
    assert "tool_not_usable:report_incident" in sol["failure_reasons"]


async def test_admission_cutoff_and_parent_stop_file_admit_nothing(tmp_path):
    root, plan = compat_root(tmp_path / "compat")
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    status = await run(root, plan, harness, auth=authorization(plan, cutoff=-60, deadline=3600))
    assert harness.created == [] and status["status"] == "held" and status["holds"] == ["admission_cutoff"]
    other, other_plan = compat_root(tmp_path / "compat-2")
    (other / "STOP").touch()
    status = await run(other, other_plan, harness)
    assert harness.created == [] and status["holds"] == ["parent_stop_or_forced_deadline"]
    assert status["live_model_call_starts"] == 0


async def test_forced_stop_deadline_truncates_the_active_attempt_and_holds(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=1))
    sample = compat_fixture(root, plan)
    now = [time.time()]

    def expire():
        now[0] += 10 ** 6

    def script(model, effort):
        return [("tool", "read_channel", {"after_event_id": None, "limit": 8}), ("stall", expire)] \
            if (model, effort) == ("gpt-6-luna", "xhigh") else qualifier_steps(sample)

    harness = Harness(tmp_path / "homes", script)
    status = await run(root, plan, harness, wall_clock=lambda: now[0])
    assert len(harness.created) == 1 and "turn/interrupt" in harness.created[0].methods
    assert "parent_stop_or_forced_deadline" in status["holds"] and status["status"] == "held"
    lane_dir = root / "lanes" / "gpt-6-luna-xhigh"
    payload = read_sealed(next((lane_dir / "attempts").iterdir()) / "attempt.json")
    assert payload["observer_result"]["termination_kind"] == "collection_forced_truncation"
    assert payload["orchestrator"]["collection_stop_reasons"] == ["parent_stop_or_forced_deadline"]


async def test_refusals_before_any_runtime_is_created(tmp_path):
    root, plan = compat_root(tmp_path / "compat")
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    other_plan = {**plan, "seal_hash": "f" * 64}
    cases = [
        (dict(caps=caps_record(caps_status="candidate")), "frozen"),
        (dict(caps=caps_record(revision="other-caps")), "differ from the sealed"),
        (dict(auth=authorization(other_plan)), "another phase, plan"),
        (dict(auth=authorization(plan, authorization={"status": "proposed", "text": "x"})), "approved"),
        (dict(bundle=fake_bundle(tool_descriptors=[{**descriptor, "description": "Changed."}
                                                   for descriptor in fake_bundle().tool_descriptors])), "changed"),
    ]
    for kwargs, message in cases:
        with pytest.raises(ValueError, match=message):
            await run(root, plan, harness, **kwargs)
    with pytest.raises(ValueError, match="explicit runtime factory"):
        await v11_live.run_live_phase(root, caps_record=plan["caps"], authorization=authorization(plan),
                                      runtime_factory=None, bundle=fake_bundle())
    assert harness.created == []
    assert all("run_opened" not in kinds(root / lane["path"]) for lane in plan["lanes"])


class HardCrash(BaseException):
    pass


async def test_crashed_attempt_is_consumed_held_and_never_rerun(tmp_path):
    root, plan = compat_root(tmp_path / "compat", caps=caps_record(global_max_concurrency=1))

    async def crashing(fixture, instructions, *, attempt_id, usage_callback, runtime, **_):
        await usage_callback({"notification_id": "n1", "cumulative_tokens": 1000, "source": "thread_total",
                              "attempt_id": attempt_id})
        raise HardCrash()

    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    with pytest.raises(HardCrash):
        await run(root, plan, harness, observer=crashing)
    lane_dir = root / "lanes" / "gpt-6-luna-xhigh"
    assert read_sealed(lane_dir / "phase-index.json")["entries"]["compat-v1-gpt-6-luna-xhigh"]["status"] == "started"
    resumed = Harness(tmp_path / "homes-2", lambda model, effort: qualifier_steps(compat_fixture(root, plan)))
    status = await run(root, plan, resumed)
    assert resumed.created == [] and status["status"] == "held"
    assert "retained_unreconciled_start:compat-v1-gpt-6-luna-xhigh-live-1" in status["holds"]
    report = v11_live.verify_live_root(root, bundle=fake_bundle())
    assert report["lanes"]["gpt-6-luna-xhigh"]["entries"][0]["status"] == "incomplete_interrupted"
    assert "attempt_interrupted_reconciled" in kinds(lane_dir)
    assert report["live_model_call_starts"] == 1


def test_lane_plans_refuse_mixed_effort_or_unsafe_ids(tmp_path):
    _, lane_plans, _ = v11_live.build_compatibility_plan(caps_record(), revision="compat-v1", bundle=fake_bundle())
    plan = lane_plans["gpt-6-sol-low"]
    mixed = {**plan, "planned_order": [{**plan["planned_order"][0], "reasoning_effort": "xhigh"}]}
    with pytest.raises(ValueError, match="lane identity"):
        v11_phase.create_lane_phase(tmp_path / "mixed", mixed)
    unsafe = {**plan, "planned_order": [{**plan["planned_order"][0], "entry_id": "a/b", "attempt_id": "a/b-live-1"}]}
    with pytest.raises(ValueError):
        v11_phase.create_lane_phase(tmp_path / "unsafe", unsafe)
    parallel = {**plan, "caps": {**plan["caps"], "max_concurrency": 2}}
    with pytest.raises(ValueError, match="one attempt at a time"):
        v11_phase.create_lane_phase(tmp_path / "parallel", parallel)


# Behavioral phases from a sealed study


@pytest.fixture(scope="module")
def smoked(qualified, tmp_path_factory):
    return smoke_root(tmp_path_factory.mktemp("smoked"), qualified[0])


def test_behavioral_phase_requires_compatibility_evidence_for_every_lane(qualified, tmp_path):
    rows, fixtures = study_rows("smoke")
    study = write_study(tmp_path / "study", rows, fixtures)
    with pytest.raises(v11_live.GateError, match="compatibility"):
        v11_live.build_phase_plan("smoke", caps_record(), revision="smoke-v1", study_directory=study,
                                  bundle=fake_bundle())
    plan, lane_plans, _ = v11_live.build_phase_plan("smoke", caps_record(), revision="smoke-v1",
                                                    study_directory=study, compatibility_directories=[qualified[0]],
                                                    bundle=fake_bundle())
    assert sorted(plan["calls_by_lane"].items()) == sorted(
        [(f"{model}-xhigh", 3) for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")]
        + [(f"{model}-low", 1) for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")])
    assert set(plan["gate_evidence"]["qualification"]) == set(plan["calls_by_lane"])
    low = lane_plans["gpt-6-sol-low"]["planned_order"]
    assert [(entry["world_mode"], entry["prompt_condition"]) for entry in low] == [("report_store_unavailable", "guided")]
    orders = [entry["planned_order"] for entry in lane_plans["gpt-6-sol-xhigh"]["planned_order"]]
    assert orders == sorted(orders)


def test_assignment_rows_are_checked_against_fixtures_and_frozen_instructions(tmp_path):
    rows, fixtures = study_rows("smoke")
    bundle = fake_bundle()
    good = v11_live.validate_assignment_rows("smoke", rows, fixtures, caps_record(), bundle)
    assert len(good) == 12 and {entry["attempt_id"][-7:] for entry in good} == {"-live-1"}
    bad_rows = [
        [{**rows[0], "instructions": "Edited instructions."}, *rows[1:]],
        [{**rows[0], "level": "L2"}, *rows[1:]],
        [{**rows[0], "effort": "medium"}, *rows[1:]],
        [*rows[:3], {**rows[3], "world_mode": "report_store_unavailable"}, *rows[4:]],
        [{**rows[0], "split": "collection"}, *rows[1:]],
        [rows[0], {**rows[1], "assignment_id": rows[0]["assignment_id"]}, *rows[2:]],
        [{key: value for key, value in rows[0].items() if key != "arm"}, *rows[1:]],
    ]
    for bad in bad_rows:
        with pytest.raises(ValueError):
            v11_live.validate_assignment_rows("smoke", bad, fixtures, caps_record(), bundle)
    broken = fake_bundle(verify_fixture=lambda fixture, template: ["twin contrast differs"])
    with pytest.raises(ValueError, match="failed verification"):
        v11_live.validate_assignment_rows("smoke", rows, fixtures, caps_record(), broken)


def test_smoke_runs_each_assignment_with_its_effort_world_mode_and_prompt(smoked):
    status, plan, harness = smoked["status"], smoked["plan"], smoked["harness"]
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    assert status["status_counts"] == {"archived": 12}
    low = [runtime for runtime in harness.created if runtime.reasoning_effort == "low"]
    assert len(low) == 3 and all(runtime.turn_efforts == ["low"] for runtime in low)
    assert sorted(world["world_mode"] for world in smoked["worlds"]) == ["normal"] * 9 + ["report_store_unavailable"] * 3
    for lane in plan["lanes"]:
        for attempt in (smoked["root"] / lane["path"] / "attempts").iterdir():
            payload = read_sealed(attempt / "attempt.json")
            entry = next(entry for entry in read_sealed(smoked["root"] / lane["path"] / "phase-plan.json")[
                "planned_order"] if entry["attempt_id"] == payload["attempt_id"])
            assert (payload["reasoning_effort"], payload["world_mode"], payload["prompt_condition"]) == (
                entry["reasoning_effort"], entry["world_mode"], entry["prompt_condition"])
            assert payload["check"]["passed"] is True and payload["check"]["checks"]["world_mode_bound"] is True
            reports = payload["observer_result"]["world_state"]["reports"]
            assert (reports == []) == (entry["world_mode"] == "report_store_unavailable")


async def test_collection_gate_requires_valid_smoke_evidence_from_the_same_study(qualified, smoked, tmp_path):
    with pytest.raises(v11_live.GateError, match="smoke"):
        v11_live.build_phase_plan("collection", caps_record(), revision="collection-v1",
                                  study_directory=smoked["study"], compatibility_directories=[qualified[0]],
                                  bundle=fake_bundle())
    built = v11_live.build_phase_plan("collection", caps_record(), revision="collection-v1",
                                      study_directory=smoked["study"], compatibility_directories=[qualified[0]],
                                      smoke_directory=smoked["root"], bundle=fake_bundle())
    plan = built[0]
    assert plan["gate_evidence"]["smoke"]["smoke_plan_hash"] == smoked["plan"]["seal_hash"]
    assert len(plan["gate_evidence"]["smoke"]["attempt_hashes"]) == 12 and plan["count_in_collection_denominator"]
    other_rows, other_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    other = write_study(tmp_path / "other-study", other_rows, other_fixtures)
    with pytest.raises(v11_live.GateError, match="another phase or study"):
        v11_live.build_phase_plan("collection", caps_record(), revision="collection-v1", study_directory=other,
                                  compatibility_directories=[qualified[0]], smoke_directory=smoked["root"],
                                  bundle=fake_bundle())
    root = tmp_path / "collection"
    v11_live.prepare_live_root(root, built)
    sealed = v11_live.read_live_plan(root)
    harness = Harness(tmp_path / "homes", lambda model, effort: [])
    with pytest.raises(v11_live.GateError, match="smoke"):
        await run(root, sealed, harness, compatibility_directories=[qualified[0]])
    assert harness.created == []
