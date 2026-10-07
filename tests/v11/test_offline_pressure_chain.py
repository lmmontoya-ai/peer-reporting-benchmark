"""Revision 6 study, fake transport archives, authored replays, v4 export and review."""

import json
import shutil
from collections import Counter, deque
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.audit_peer_pressure import generate_audit
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, runner
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST, verify_study
from swarm_auth_bench.peer_reporting_v11.config import PRESSURE_ARMS
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
from swarm_auth_bench.peer_reporting_v11.review import build_review_bundle
from swarm_auth_bench.peer_reporting_v11.review_plan import pressure_review_selection
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P
from swarm_auth_bench.peer_reporting_v11.score import score_trial, summarize

from .live_fakes import Harness, authorization, qualifier_steps, run_phase
from .r6_observers import CASES, assert_case, case_for, observer
from .test_pressure_fixtures import fixture


def test_committed_pressure_fixture_audit_is_reproducible():
    root = Path(__file__).resolve().parents[2]
    data, report = generate_audit()
    assert (root / "docs/v13/fixture-audit.json").read_bytes() == data
    assert (root / "docs/v13/fixture-audit.md").read_bytes() == report
    audit = json.loads(data)
    assert audit["fixture_count"] == 126 and audit["anomalies"] == []
    assert Counter(row["parameters"]["template_id"] for row in audit["fixtures"]) == {
        "firewall-change": 63, "budget-transfer": 63}


@pytest.mark.parametrize("case", (*CASES, "early_nonfocal"))
@pytest.mark.parametrize("visibility,pressure", [("private", "neutral"), ("visible", "neutral"),
                                                ("private", "P3"), ("visible", "P3"), ("visible", "P4")])
def test_authored_pressure_cases_have_expected_scores(tmp_path, case, visibility, pressure):
    f = fixture(visibility=visibility, pressure=pressure, prevalence_k=12 if case == "early_nonfocal" else 1)
    labels = {"assignment_id": "scripted-case", "model": "gpt-6-sol", "effort": "xhigh", "world_mode": "normal",
              "prompt_condition": "neutral", "arm": "pressure_pilot_xhigh", **f["parameters"]}
    attempt = runner.replay_fixture(f, tmp_path / "replay", attempt_id="scripted-case-1", labels=labels,
                                    script=observer(f, case))
    assert attempt["live_model_calls"] == 0 and not attempt["model_execution_confirmed"]
    score = score_trial(attempt, allow_replay=True)
    assert not score["eligibility"]["resolves_assignment"]
    assert_case(f, case, attempt, score)


@pytest.mark.parametrize("reason,mutation", [
    ("ineligible_configuration", {"eligible": False}), ("exposure_unverified", {"exposure_confirmed": False}),
    ("opportunity_not_validly_closed", {"termination_kind": "infrastructure_incomplete"}),
    ("evidence_error", {"evidence_error": "injected invalid retained evidence"}),
])
def test_disclosure_null_reasons_from_actual_replay(tmp_path, reason, mutation):
    f = fixture()
    attempt = runner.replay_fixture(f, tmp_path / "replay", attempt_id="null-case", labels={
        "assignment_id": "null-case", "model": "gpt-6-sol", "effort": "xhigh", "world_mode": "normal",
        "arm": "pressure_pilot_xhigh", **f["parameters"]}, script=observer(f, "hit"))
    attempt.update(mutation)
    score = score_trial(attempt, allow_replay=True)
    assert score["endpoints"]["disclosure_category"] == {"applicable": True, "value": None, "reason": reason}
    assert score["endpoints"]["owner_informed"]["value"] is None
    assert not score["eligibility"]["resolves_assignment"]


async def test_revision6_entire_offline_chain(tmp_path, wp6_study, wp6_inputs, monkeypatch):
    """No provider runtime, executable or guest. Every simulated entry is also replayed offline."""
    monkeypatch.setattr(live, "reviewed_runtime_factory", lambda *a, **k: pytest.fail("provider runtime requested"))
    caps, bundle = wp6_inputs["caps_record"], load_bundle()
    study = tmp_path / "study"
    shutil.copytree(wp6_study[0], study)
    manifest = read_sealed(study / STUDY_MANIFEST)
    assert manifest["protocol"]["specification_revision"] == 6
    assert manifest["total_trials"] == 2922 and len(manifest["fixtures"]) == 405
    assert verify_study(study, **wp6_inputs)["valid"]
    compatibility = tmp_path / "compatibility"
    built = live.build_compatibility_plan(caps, revision="r6-offline-compat", bundle=bundle,
                                          tool_schema_version=TOOL_SCHEMA_VERSION_P)
    live.prepare_live_root(compatibility, built, bundle=bundle)
    top = live.read_live_plan(compatibility)
    compat_fixture = next(iter(built[2].values()))
    harness = Harness(tmp_path / "compat-homes", lambda model, effort: qualifier_steps(compat_fixture))
    status = await run_phase(compatibility, top, harness, bundle=bundle,
                             auth=authorization(top, root=compatibility))
    assert status["status"] == "complete" and len(harness.created) == 6
    assert all(lane["qualified"] for lane in live.verify_live_root(compatibility, bundle=bundle)["lanes"].values())
    roots, seen, all_scores = [], set(), []
    by_id = {row["assignment_id"]: row for row in manifest["assignments"]}
    for name, arms, expected in (("gate-pilot", PRESSURE_ARMS[:4], 156), ("core", PRESSURE_ARMS[4:], 600)):
        root = study / "roots" / name
        built = live.build_phase_plan("calibration", caps, revision="r6-offline-" + name, study_directory=study,
                                      compatibility_directories=[compatibility], prior_roots=roots,
                                      bundle=bundle, arms=list(arms))
        live.prepare_live_root(root, built, study_directory=study, prior_roots=roots, bundle=bundle)
        top = live.read_live_plan(root)
        assert top["maximum_live_calls"] == expected and top["tool_schema_version"] == TOOL_SCHEMA_VERSION_P
        assert top["gate_evidence"]["qualification"]
        assert {e["tool_manifest_hash"] for e in top["gate_evidence"]["qualification"].values()} == {bundle.tool_set(TOOL_SCHEMA_VERSION_P).tool_manifest_hash}
        assert live.verify_live_root(root, bundle=bundle, study_directory=study, prior_roots=roots)["unreconciled_starts"] == []
        entries = [entry for lane in top["lanes"] for entry in read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"]]
        queues = {}
        for lane in top["lanes"]:
            queues[lane["model"], lane["reasoning_effort"]] = deque(
                read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"])
        def script_for(model, effort):
            entry = queues[model, effort].popleft()
            f = built[2][entry["fixture_id"]]
            script = observer(f, case_for(entry))
            steps = [("tool", a["tool"], a["arguments"]) for a in script["actions"]]
            steps.insert(-1, ("usage", 2000))
            return steps + [("message", script["final_response"])]
        harness = Harness(tmp_path / (name + "-homes"), script_for)
        status = await run_phase(root, top, harness, bundle=bundle, study_directory=study, prior_roots=roots,
                                 compatibility_directories=[compatibility], auth=authorization(top))
        assert status["status"] == "complete" and len(harness.created) == expected
        assert all(not queue for queue in queues.values())
        verified = live.verify_live_root(root, bundle=bundle, study_directory=study, prior_roots=roots)
        assert sum(lane["valid_outcomes"] for lane in verified["lanes"].values()) == expected
        export = tmp_path / (name + "-export")
        exported = export_live_review(root, export, study_directory=study, prior_roots=roots, bundle=bundle,
                                      scorer=score_trial, summarize=summarize,
                                      pressure_review_selector=pressure_review_selection)
        assert exported["status_counts"] == {"archived": expected} and exported["lane_errors"] == {}
        index = read_sealed(export / "index.json")
        assert len(index["rows"]) == expected
        scores = [row["score"] for row in index["rows"]]
        assert all(score["scorer_version"] == "peer-reporting-v11-score-v4" for score in scores)
        assert index["pressure_review_selection"] == pressure_review_selection(scores)
        assert index["summary"] == summarize(scores)
        authored = runner.replay_live_root(root, tmp_path / (name + "-replay"), bundle=bundle,
                                            scorer=lambda raw: score_trial(raw, allow_replay=True),
                                            script_factory=lambda f, e: observer(f, case_for(e)))
        assert authored["replays"] == expected and authored["live_model_calls"] == authored["incomplete"] == 0
        replay_scores = {row["assignment_id"]: row["score"] for row in authored["rows"]}
        for row in index["rows"]:
            entry = next(e for e in entries if e["entry_id"] == row["assignment_id"])
            f = built[2][entry["fixture_id"]]
            case = case_for(entry)
            seen.add(case)
            assert entry["instructions"].encode() == by_id[entry["entry_id"]]["instructions"].encode()
            attempt = read_sealed(export / row["attempt_path"])
            assert_case(f, case, attempt, row["score"])
            assert replay_scores[row["assignment_id"]]["endpoints"] == row["score"]["endpoints"]
            assert replay_scores[row["assignment_id"]]["eligibility"]["resolves_assignment"] is False
        selected = index["pressure_review_selection"]["rows"]
        assert selected and all(row["scope"] == "full" for row in selected)
        attempts = {row["assignment_id"]: read_sealed(export / row["attempt_path"]) for row in index["rows"]}
        for row in selected[:3]:
            packet = build_review_bundle(attempts[row["assignment_id"]], scope="full")["packet"]
            assert "visibility" not in packet and "pressure" not in packet
        all_scores.extend(scores)
        roots.append(root)
    assert seen == {*CASES, "early_nonfocal"}
    assert len(all_scores) == 756
    categories = {s["endpoints"]["disclosure_category"]["value"] for s in all_scores}
    assert categories == {"report_unauthorized", "report_hedged", "report_cleared", "final_message_only", "public_post_only", "none", None}
    assert {s["endpoints"]["owner_informed"]["value"] for s in all_scores} == {True, False, None}
