"""Astra review of specification revision 3: M1, M2, M3, and m1, each kept as a regression test. No model calls.

M1: the overload exception is decided after shutdown and the last event drain, from every reconciled error
notification; a late error with another code, or an announced retry anywhere in the turn, removes it.
M2: a non-retryable error notification is an execution failure whatever the final turn status.
M3: provider pauses and their 60-minute window are recorded at study level, so every root of the study respects them.
m1: a scored export leaves rows excluded from analysis out of its summary and reports them as a separate count.
"""

from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11 import phase as v11_phase
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
from swarm_auth_bench.peer_reporting_v11.score import score_trial, summarize

from .live_fakes import (
    OVERLOAD_MESSAGE,
    FakeTransport,
    Harness,
    PauseClock,
    fake_bundle,
    overload_steps,
    qualified_root,
    report_steps,
)
from .test_live_overload import (
    INVALID_TURN,
    LUNA_L1,
    SOL_L1,
    calibration_study,
    lane_records,
    run_paused,
    sealed_root,
    started_at,
)
from .test_live_r2 import attempt_of, payloads, records, scripted, smoke_study

PROVIDER_ERROR = "non-retryable provider error notification"
EXTRA_ERRORS = {
    "stream_disconnected": {"error": {"message": "another transport failure", "codexErrorInfo": "streamDisconnected"},
                            "willRetry": False},
    "retry_announced": {"error": {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded",
                                  "additionalDetails": None}, "willRetry": True},
}


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("astra-r3-compat"))[0]


class LateErrorTransport(FakeTransport):
    """After the turn ends, one more scoped ``error`` notification: queued right behind ``turn/completed`` (read by
    the first drain) or pushed while the runtime closes (read by the last drain, after ``runtime.close()``)."""

    late: tuple[str, dict] | None = None

    async def _model(self, packet):
        await super()._model(packet)
        if self.late is not None and self.late[0] == "first_drain":
            self._late_error()

    async def close(self):
        if self.late is not None and self.late[0] == "last_drain" and self.thread_id in self._sessions:
            self._late_error()
        await super().close()

    def _late_error(self):
        params = {"threadId": self.thread_id, "turnId": self.turn_id, **self.late[1]}
        self._sessions[self.thread_id].queue.put_nowait({"method": "error", "params": params})


class LateHarness(Harness):
    def __init__(self, root, script_for, late):
        super().__init__(root, script_for)
        self.late = late

    def factory(self, model, effort):
        home = self.root / f"home-{len(self.created)}"
        home.mkdir(parents=True)
        runtime = LateErrorTransport(model, effort, self.script_for(model, effort), home)
        runtime.late = self.late
        self.created.append(runtime)
        return runtime


def error_notifications(result):
    return [(event["raw"]["params"]["error"]["codexErrorInfo"], event["raw"]["params"]["willRetry"])
            for event in result["raw_events"] if (event.get("raw") or {}).get("method") == "error"]


def export_row(root, study, output, attempt):
    export_live_review(root, output, study_directory=study, bundle=fake_bundle(), scorer=score_trial,
                       summarize=summarize)
    index = read_sealed(Path(output) / "index.json")
    return index, next(row for row in index["rows"] if row["assignment_id"] + v11_live.ATTEMPT_SUFFIX == attempt)


# M1: the overload exception is decided after the last drain


@pytest.mark.parametrize("boundary", ["in_turn", "first_drain", "last_drain"])
@pytest.mark.parametrize("extra", ["stream_disconnected", "retry_announced"])
async def test_another_code_or_a_retry_anywhere_in_the_turn_removes_the_overload_exception(compat, tmp_path, boundary,
                                                                                           extra):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    steps = overload_steps()
    if boundary == "in_turn":  # before the qualifying overload error, so the last error still announces no retry
        steps = [steps[0], ("raw", "error", EXTRA_ERRORS[extra]), *steps[1:]]
        harness = Harness(tmp_path / "h1", scripted(fixtures, {LUNA_L1: lambda f: steps}))
    else:
        harness = LateHarness(tmp_path / "h1", scripted(fixtures, {LUNA_L1: lambda f: steps}),
                              (boundary, EXTRA_ERRORS[extra]))
    # A soft stop at the first pause poll bounds the run if the exception were (wrongly) granted.
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    extra_code = (EXTRA_ERRORS[extra]["error"]["codexErrorInfo"], EXTRA_ERRORS[extra]["willRetry"])
    expected = ([extra_code, ("serverOverloaded", False)] if boundary == "in_turn"
                else [("serverOverloaded", False), extra_code])
    assert error_notifications(result) == expected
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    assert INVALID_TURN in result["infrastructure_failures"]
    assert payload["check"]["classification"] is None and payload["orchestrator"]["provider_pause"] is None
    assert payload["orchestrator"]["usage_settlement"]["status"] == "unresolved"
    # Subsequent admission: the run holds on the execution failure instead of pausing.
    assert status["status"] == "held" and status["live_model_call_starts"] == 1 and clock.waits == []
    assert f"execution_check_failure:{attempt}" in status["holds"] and "soft_stop" not in status["holds"]
    assert records(root, "provider_pause_started") == [] and v11_live.study_provider_pauses(study) == {}
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    later = await run_paused(root, plan, resumed, clock, compat, study)
    assert later["status"] == "held" and f"retained_failed_or_unknown_attempt:{attempt}" in later["holds"]
    assert resumed.created == [] and later["live_model_call_starts"] == 1
    # Export: an ordinary failed attempt, neither excluded nor an eligible observation.
    index, row = export_row(root, study, tmp_path / "export", attempt)
    assert row["excluded_from_analysis"] is False and index["analysis_exclusions"] == []
    assert row["score"]["eligibility"]["eligible"] is False
    assert row["score"]["eligibility"]["resolves_assignment"] is False


async def test_the_recorded_overload_evidence_lists_every_reconciled_error_notification(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    repeated = {"error": {"message": OVERLOAD_MESSAGE, "codexErrorInfo": "serverOverloaded", "additionalDetails": None},
                "willRetry": False}
    harness = LateHarness(tmp_path / "h1", scripted(fixtures, {LUNA_L1: lambda f: overload_steps()}),
                          ("last_drain", repeated))
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    status = await run_paused(root, plan, harness, clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    result = payloads(root)[attempt]["observer_result"]
    # A second capacity refusal while the runtime closes keeps the exception, and the evidence lists both.
    assert result["termination_kind"] == "provider_unavailable" and status["holds"] == ["soft_stop"]
    assert [error["code"] for error in result["provider_overload"]["error_notifications"]] == ["serverOverloaded"] * 2
    assert len(result["provider_overload"]["error_notifications"]) == len(error_notifications(result)) == 2


# M2: a non-retryable error is never masked by a stop or a completion


@pytest.mark.parametrize("code", ["serverOverloaded", "streamDisconnected"])
@pytest.mark.parametrize("ending", ["hard_stop", "completed"])
async def test_a_non_retryable_error_is_an_execution_failure_whatever_the_final_status(compat, tmp_path, code, ending):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    error = ("raw", "error", {"error": {"codexErrorInfo": code, "message": "capacity refused"}, "willRetry": False})
    tail = ([("sleep", 0.02), ("stall", lambda: (root / v11_live.HARD_STOP_FILE).touch())] if ending == "hard_stop"
            else [("end", "completed", None)])
    harness = Harness(tmp_path / "h1", scripted(fixtures, {LUNA_L1: lambda f: [("usage", 1000), error, *tail]}))
    status = await run_paused(root, plan, harness, PauseClock(), compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    result = payload["observer_result"]
    assert error_notifications(result) == [(code, False)]
    if ending == "hard_stop":
        assert result["boundary"]["termination_kind"] == "collection_forced_truncation"
        assert payload["orchestrator"]["collection_stop_reasons"] == ["hard_stop_or_forced_deadline"]
        assert "hard_stop" in status["holds"]
    else:
        assert result["boundary"]["reason"] == "turn_completed"
    assert result["termination_kind"] == "infrastructure_incomplete" and result["provider_overload"] is None
    assert PROVIDER_ERROR in result["infrastructure_failures"]
    assert payload["check"]["classification"] is None and payload["check"]["passed"] is False
    assert "no_infrastructure_failure" in payload["check"]["failure_reasons"]
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    assert f"execution_check_failure:{attempt}" in status["holds"]
    # Resume: the retained failure holds; nothing else starts.
    (root / v11_live.HARD_STOP_FILE).unlink(missing_ok=True)
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    later = await run_paused(root, plan, resumed, PauseClock(), compat, study)
    assert later["status"] == "held" and f"retained_failed_or_unknown_attempt:{attempt}" in later["holds"]
    assert resumed.created == [] and later["live_model_call_starts"] == 1
    # Export: the row is no eligible observation and resolves nothing.
    _, row = export_row(root, study, tmp_path / "export", attempt)
    exported = read_sealed(tmp_path / "export" / row["attempt_path"])
    assert exported["termination_kind"] == "infrastructure_incomplete"
    assert row["score"]["eligibility"]["eligible"] is False
    assert row["score"]["eligibility"]["resolves_assignment"] is False
    assert all(endpoint["value"] is None for endpoint in row["score"]["endpoints"].values())


async def test_a_retrying_error_that_recovers_is_not_by_itself_a_failure(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    retry = ("raw", "error", EXTRA_ERRORS["retry_announced"])
    harness = Harness(tmp_path / "h1", scripted(fixtures, {LUNA_L1: lambda f: [retry, *report_steps(f)]}))
    status = await run_paused(root, plan, harness, PauseClock(), compat, study)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    result = payloads(root)[attempt]["observer_result"]
    assert error_notifications(result) == [("serverOverloaded", True)]
    assert result["termination_kind"] == "natural_end" and result["infrastructure_failures"] == []
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12


# M3: pauses and their window are recorded at study level


def first_attempt_overloads(fixtures):
    """A root's first session meets the capacity refusal; every later session reports normally."""
    by_packet = {fixture["packet"]: fixture for fixture in fixtures.values()}
    served = []

    def script_for(model, effort):
        def steps(packet):
            if not served:
                served.append((model, effort))
                return overload_steps()
            return report_steps(by_packet[packet])
        return steps
    return script_for, served


def sealed_pause(root):
    (pause,) = [payload["orchestrator"]["provider_pause"] for payload in payloads(root).values()
                if payload["orchestrator"]["provider_pause"] is not None]
    return pause


async def test_every_root_of_the_study_waits_out_a_pause_and_the_third_refusal_holds(compat, tmp_path):
    study, rows, fixtures = calibration_study(tmp_path / "study")
    clock = PauseClock()
    prior, roots, sessions = [], {}, {}
    for name, arm in (("a", "calibration_extension_xhigh"), ("b", "calibration_extension_low"),
                      ("c", "calibration")):
        root, plan = sealed_root(study, compat, phase="calibration", revision=f"calibration-{name}", arms=[arm],
                                 prior_roots=list(prior))
        script_for, served = first_attempt_overloads(fixtures)
        # A soft stop at the first pause poll after this root's own refusal, never while it waits for an earlier one.
        clock.on_wait = lambda clock, root=root, served=served: (root / v11_live.STOP_FILE).touch() if served else None
        sessions[name] = []
        harness = Harness(tmp_path / f"h-{name}", script_for, observe=started_at(clock, sessions[name]))
        status = await run_paused(root, plan, harness, clock, compat, study, prior_roots=list(prior))
        roots[name] = (root, status, sealed_pause(root))
        prior.append(root)
    (_, a_status, a_pause), (_, b_status, b_pause), (c_root, c_status, c_pause) = roots.values()
    assert a_status["holds"] == b_status["holds"] == ["soft_stop"]
    assert [status["live_model_call_starts"] for status, _ in ((a_status, 0), (b_status, 0), (c_status, 0))] == [1] * 3
    # B and C start nothing before the pause in force when they open has ended.
    assert min(time for _, _, time in sessions["b"]) >= a_pause["resume_at"]
    assert min(time for _, _, time in sessions["c"]) >= b_pause["resume_at"]
    # The window count runs across the roots; the third refusal within 60 minutes holds.
    assert [pause["window_count"] for pause in (a_pause, b_pause, c_pause)] == [1, 2, 3]
    assert c_pause["holds_admission"] is True and c_pause["paused_at"] - a_pause["paused_at"] < 3600
    assert c_status["status"] == "held" and c_status["holds"] == [f"provider_unavailable_limit:{c_pause['attempt_id']}"]
    # Each pause is recorded once at study level, naming its root, and every root still verifies.
    recorded = v11_live.study_provider_pauses(study)
    assert sorted(recorded) == sorted(pause["attempt_id"] for pause in (a_pause, b_pause, c_pause))
    for root, _, pause in roots.values():
        record = recorded[pause["attempt_id"]]
        assert record["pause"] == pause and record["plan_hash"] == v11_live.read_live_plan(root)["seal_hash"]
        report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
        assert report["study_provider_pauses"] == [a_pause, b_pause, c_pause]
    # A later root only journals the ends of its own pauses.
    b_root = roots["b"][0]
    ended = [record["attempt_id"] for lane in v11_live.read_live_plan(b_root)["lanes"]
             for record in lane_records(b_root, lane["lane_id"], "provider_pause_ended")]
    assert a_pause["attempt_id"] not in ended
    # A fourth root, opened while the window still holds three refusals, holds from its start.
    d_root, d_plan = sealed_root(study, compat, phase="calibration", revision="calibration-d",
                                 arms=["calibration_extension_xhigh"], prior_roots=list(prior))
    d_harness = Harness(tmp_path / "h-d", scripted(fixtures))
    held = await run_paused(d_root, d_plan, d_harness, clock, compat, study, prior_roots=list(prior))
    assert held["status"] == "held" and held["holds"] == [
        f"retained_provider_unavailable_limit:{c_pause['attempt_id']}"]
    assert d_harness.created == [] and held["live_model_call_starts"] == 0


async def test_a_crash_before_the_study_pause_record_leaves_an_incomplete_start_that_holds(compat, tmp_path,
                                                                                         monkeypatch):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)

    def crash(*_args, **_kwargs):
        raise OSError("simulated crash before the study pause record")

    clock = PauseClock()
    with monkeypatch.context() as patched:
        patched.setattr(v11_live, "record_study_pause", crash)
        crashed = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures,
                                                                                  {SOL_L1: lambda f: overload_steps()})),
                                   clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    assert crashed["status"] == "held" and crashed["live_model_call_starts"] == 2
    assert f"attempt_unarchived:{attempt}:OSError" in crashed["holds"]
    # The classification was never accepted: no archive, no journaled pause, and no study pause.
    assert attempt not in [record["data"]["attempt_id"] for record in records(root, "attempt_archived")]
    assert records(root, "provider_pause_started") == [] and v11_live.study_provider_pauses(study) == {}
    # The restart reconciles the start as incomplete; it stays consumed, holds, and never runs again.
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    later = await run_paused(root, plan, resumed, clock, compat, study)
    assert later["status"] == "held" and f"retained_unreconciled_start:{attempt}" in later["holds"]
    assert resumed.created == [] and later["live_model_call_starts"] == 2 and clock.waits == []
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["study_provider_pauses"] == [] and report["provider_pauses"] == []


async def test_a_pause_recorded_before_a_crash_binds_a_superseding_root(compat, tmp_path, monkeypatch):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)

    summary = v11_phase.attempt_summary

    def crash(payload):
        if payload["orchestrator"]["provider_pause"] is not None:
            raise OSError("simulated crash between the study pause record and the archive record")
        return summary(payload)

    clock = PauseClock()
    with monkeypatch.context() as patched:
        patched.setattr(v11_phase, "attempt_summary", crash)
        crashed = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures,
                                                                                  {SOL_L1: lambda f: overload_steps()})),
                                   clock, compat, study)
    attempt = attempt_of(rows, "gpt-6-sol", "L1", "violation")
    assert crashed["status"] == "held" and records(root, "provider_pause_started") == []
    (recorded,) = v11_live.study_provider_pauses(study).values()
    pause = recorded["pause"]
    assert pause["attempt_id"] == attempt and pause["window_count"] == 1
    # The incomplete start holds its own root; verify accepts the study pause of an unarchived start.
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert report["study_provider_pauses"] == [pause] and attempt in report["unreconciled_starts"]
    # A root that supersedes it still waits out the recorded pause before its first session.
    successor, successor_plan = sealed_root(study, compat, revision="smoke-v2", prior_roots=[root])
    sessions = []
    status = await run_paused(successor, successor_plan, Harness(tmp_path / "h2", scripted(fixtures),
                                                                 observe=started_at(clock, sessions)),
                              clock, compat, study, prior_roots=[root])
    assert status["status"] == "complete" and status["live_model_call_starts"] == 10
    assert min(time for _, _, time in sessions) >= pause["resume_at"]


# m1: summaries leave excluded rows out of their counts


async def test_a_scored_export_leaves_an_excluded_refusal_out_of_the_summary(compat, tmp_path):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = sealed_root(study, compat)
    clock = PauseClock(on_wait=lambda clock: (root / v11_live.STOP_FILE).touch())
    special = {SOL_L1: lambda f: [("usage", 1000), *overload_steps()]}
    status = await run_paused(root, plan, Harness(tmp_path / "h1", scripted(fixtures, special)), clock, compat, study)
    luna, sol = (attempt_of(rows, model, "L1", "violation") for model in ("gpt-6-luna", "gpt-6-sol"))
    assert status["holds"] == ["soft_stop"] and status["live_model_call_starts"] == 2
    index, row = export_row(root, study, tmp_path / "export", sol)
    sealed = read_sealed(tmp_path / "export" / row["attempt_path"])
    assert sealed["usage"] == {"total_tokens": 75000, "observed_total_tokens": 1000,
                               "settlement": "bounded_by_reservation"}
    assert row["status"] == "archived" and row["excluded_from_analysis"] is True
    assert row["score"]["eligibility"]["eligible"] is False
    assert row["score"]["eligibility"]["resolves_assignment"] is False
    # Only the luna attempt enters the summary: every count and cell leaves the refusal out.
    summary = index["summary"]
    assert summary["trial_count"] == 1
    for grouping in summary["groupings"]:
        assert sum(cell["trial_count"] for cell in grouping["cells"]) == 1
    (cell,) = summary["cells"]
    assert cell["model"] == "gpt-6-luna" and cell["arm"] == "smoke"
    focal = cell["endpoints"]["focal_hit"]
    assert (focal["applicable"], focal["null"]) == (1, 0)
    # The exclusion is reported separately.
    assert index["analysis_exclusions"] == [row["assignment_id"]] and index["analysis_exclusion_count"] == 1
    assert luna[:-len(v11_live.ATTEMPT_SUFFIX)] not in index["analysis_exclusions"]
