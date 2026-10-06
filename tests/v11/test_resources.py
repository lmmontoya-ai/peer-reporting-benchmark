"""WP10 proposals from sealed roots over the offline live test transport."""

import asyncio
import json
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.config import read_json
from swarm_auth_bench.peer_reporting.storage import atomic_json, check_seal, read_sealed
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live, live_runtime
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.lanes import validate_caps_record
from swarm_auth_bench.peer_reporting_v11.resources import freeze_caps, propose_caps

from .live_fakes import (
    Harness,
    build_plan,
    caps_record,
    compat_fixture,
    compat_root,
    fake_bundle,
    packet_scripts,
    qualifier_steps,
    run_phase,
    study_rows,
    write_study,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(live, "load_bundle", fake_bundle)
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)

    def forbidden(*args, **kwargs):
        raise AssertionError("resource commands must never create a model runtime")

    monkeypatch.setattr(live, "reviewed_runtime_factory", forbidden)


def measured_root(base, tokens=100001, seconds=123.4, *, settlements=None):
    caps = caps_record(trial={"max_trial_wall_seconds": 600, "drain_grace_seconds": 10,
                             "max_tool_requests_per_trial": 32, "trial_observed_token_stop_target": 500000,
                             "reserved_tokens_per_trial": 625000})
    root, plan = compat_root(base / "compat", caps=caps)
    fixture = compat_fixture(root, plan)
    settlements = settlements or {}

    def script(model, effort):
        status = settlements.get((model, effort), "settled")
        return qualifier_steps(fixture, usage=None if status == "unresolved" else tokens)

    async def observer(*args, **kwargs):
        result = await live_runtime.run_live_observer(*args, **kwargs)
        status = settlements.get((kwargs["requested_model"], kwargs["reasoning_effort"]), "settled")
        result["elapsed_seconds"] = seconds if status == "settled" else 9999
        if status == "bounded_by_reservation":
            result["usage"]["total_tokens"] = None
        return result

    asyncio.run(run_phase(root, plan, Harness(base / "homes", script), observer=observer))
    return root


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    return measured_root(tmp_path_factory.mktemp("wp10-measured"))


@pytest.fixture(scope="module")
def floor_root(tmp_path_factory):
    return measured_root(tmp_path_factory.mktemp("wp10-floor"), tokens=1234, seconds=1)


def proposal(root, study, *, phase="calibration", protocol=None, **kwargs):
    return propose_caps([root], study_directory=study, phase=phase, protocol=protocol or load_protocol(), **kwargs)


@pytest.mark.parametrize("tokens,seconds,stop,reservation,wall", [
    (0, 0, 60000, 75000, 180),
    (40000, 90, 60000, 75000, 180),
    (40001, 90.1, 65000, 85000, 210),
    (100001, 123.4, 155000, 195000, 270),
])
def test_exact_formula_and_floors(tmp_path, wp6_study, tokens, seconds, stop, reservation, wall):
    root = measured_root(tmp_path, tokens, seconds)
    result = proposal(root, wp6_study[0])
    assert result["caps"]["trial"] == {
        "max_trial_wall_seconds": wall, "drain_grace_seconds": 10, "max_tool_requests_per_trial": 32,
        "trial_observed_token_stop_target": stop, "reserved_tokens_per_trial": reservation}
    assert result["measured_peaks"] == {"usage_total_tokens": tokens, "elapsed_seconds": seconds,
                                        "tool_request_count": 6}
    assert result["status"] == result["caps"]["caps_status"] == "proposed"
    assert result["live_model_calls"] == 0
    check_seal(result)


def test_bounded_unresolved_and_unarchived_attempts_are_listed_but_not_measured(tmp_path, wp6_study):
    statuses = {("gpt-6-sol", "xhigh"): "bounded_by_reservation", ("gpt-6-astra", "xhigh"): "unresolved"}
    root = measured_root(tmp_path, tokens=4321, seconds=20, settlements=statuses)
    result = proposal(root, wp6_study[0])
    assert result["measured_peaks"]["usage_total_tokens"] == 4321
    assert result["measured_peaks"]["elapsed_seconds"] == 20
    assert {row["usage_settlement"] for row in result["attempts_used"]} == {"settled"}
    assert {row["reason"] for row in result["attempts_ignored"]} >= {
        "usage_settlement:bounded_by_reservation", "usage_settlement:unresolved"}
    bounded = next(row for row in result["attempts_ignored"] if row["usage_settlement"] == "bounded_by_reservation")
    assert bounded["usage_total_tokens"] == 625000 and bounded["elapsed_seconds"] == 9999
    assert result["inputs"][0]["plan_hash"] == live.read_live_plan(root)["seal_hash"]
    used = result["inputs"][0]["attempt_ids_used"]
    ignored = [row["attempt_id"] for row in result["inputs"][0]["attempts_ignored"]]
    assert len(used) + len(ignored) == 6 and set(used).isdisjoint(ignored)


def test_unstarted_root_cannot_supply_measurements(tmp_path, wp6_study):
    root, _ = compat_root(tmp_path / "unstarted")
    with pytest.raises(ValueError, match="no archived settled attempts"):
        proposal(root, wp6_study[0])


@pytest.mark.parametrize("artifact", ["plan", "index", "attempt", "journal"])
def test_unverified_root_is_refused(tmp_path, wp6_study, artifact):
    root = measured_root(tmp_path)
    if artifact == "journal":
        path = next(root.glob("lanes/*/journal.jsonl"))
        path.write_text("{}\n", encoding="utf-8")
    else:
        pattern = {"plan": "live-plan.json", "index": "lanes/*/phase-index.json",
                   "attempt": "lanes/*/attempts/*/attempt.json"}[artifact]
        path = next(root.glob(pattern))
        record = read_json(path)
        record["tampered"] = True
        atomic_json(path, record)
    with pytest.raises((ValueError, KeyError)):
        proposal(root, wp6_study[0])


def test_lane_walls_use_slowest_lane_and_token_targets_have_headroom(measured, wp6_study):
    result = proposal(measured, wp6_study[0])
    sizing = result["per_lane_sizing"]["calibration"]
    assert sizing["slowest_lane_planned_rows"] == 164
    # 164 * (270 + 10 + 15) + 120 = 48500, rounded up to 48510 seconds.
    assert result["caps"]["lane_wall_seconds"]["calibration"] == 48510
    assert len(sizing["lanes"]) == 6
    for model in wp6_study[1]["protocol"]["models"]:
        for effort, count, reservations, target in (("xhigh", 164, 31980000, 32175000),
                                                     ("low", 110, 21450000, 21645000)):
            lane = sizing["lanes"][f"{model}-{effort}"]
            assert lane["planned_rows"] == count
            assert lane["caps"]["collection_wall_seconds"] == 48510
            assert lane["planned_reservations"] == reservations
            assert lane["caps"]["collection_observed_token_stop_target"] == target
            assert lane["caps"]["collection_observed_token_stop_target"] >= (
                lane["planned_reservations"] + lane["caps"]["reserved_tokens_per_trial"])


@pytest.fixture(scope="module")
def calibration(tmp_path_factory, floor_root):
    base = tmp_path_factory.mktemp("wp10-calibration")
    rows, fixtures = [], {}
    for split, template, seed in (("calibration", "firewall-change", 1102),
                                  ("smoke", "token-issuance", 1103), ("collection", "release-request", 1101)):
        selected, built = study_rows(split, template_id=template, seed=seed)
        rows.extend(selected)
        fixtures.update(built)
    # Deliberately unequal lane counts. Lane walls still use the busiest lane.
    rows = [row for row in rows if not (row["split"] == "collection" and row["model"] == "gpt-6-astra"
                                       and row["level"] == "L4")]
    study = write_study(base / "study", rows, fixtures)
    built = build_plan("calibration", study, compatibility_directories=[floor_root])
    root = study / "roots" / "calibration"
    live.prepare_live_root(root, built, study_directory=study)
    plan = live.read_live_plan(root)

    async def observer(*args, **kwargs):
        result = await live_runtime.run_live_observer(*args, **kwargs)
        result["elapsed_seconds"] = 100
        return result

    asyncio.run(run_phase(root, plan, Harness(base / "homes", packet_scripts(fixtures)),
                         compatibility_directories=[floor_root], study_directory=study, observer=observer))
    return root, study


@pytest.mark.parametrize("phase", ["smoke", "collection"])
def test_second_proposal_sizes_smoke_and_collection_together(calibration, phase):
    root, study = calibration
    result = proposal(root, study, phase=phase)
    assert result["target_phases"] == ["smoke", "collection"]
    assert result["caps"]["trial"]["max_trial_wall_seconds"] == 210
    assert result["caps"]["lane_wall_seconds"]["smoke"] == 840
    assert result["caps"]["lane_wall_seconds"]["collection"] == 840
    for sizing in result["per_lane_sizing"].values():
        assert sizing["slowest_lane_planned_rows"] == 3
        assert {lane["caps"]["collection_wall_seconds"] for lane in sizing["lanes"].values()} == {840}
        for lane in sizing["lanes"].values():
            assert lane["caps"]["collection_observed_token_stop_target"] == (lane["planned_rows"] + 1) * 75000
    assert {lane["planned_rows"] for lane in result["per_lane_sizing"]["collection"]["lanes"].values()} == {1, 2, 3}


def test_calibration_requires_registered_study(calibration, wp6_study):
    root, _ = calibration
    with pytest.raises(ValueError, match="another study instance"):
        proposal(root, wp6_study[0], phase="collection")


def test_prior_walls_are_preserved_and_global_concurrency_comes_from_protocol(measured, tmp_path):
    protocol = load_protocol()
    protocol["caps"]["global_max_concurrency"] = 1
    # Use a sealed fake manifest without an embedded protocol for this scheduling variant.
    rows, fixtures = study_rows("calibration", template_id="firewall-change")
    study = write_study(tmp_path / "serial-study", rows, fixtures)
    prior = caps_record(lane_wall_seconds={phase: 777 for phase in ("compatibility", "calibration", "smoke", "collection")})
    result = proposal(measured, study, protocol=protocol, prior_caps=prior)
    assert result["caps"]["global_max_concurrency"] == 1
    assert result["per_lane_sizing"]["calibration"]["dispatch_waves"] == 6
    assert result["caps"]["lane_wall_seconds"]["calibration"] == 5430
    assert all(result["caps"]["lane_wall_seconds"][phase] == 777 for phase in ("compatibility", "smoke", "collection"))
    assert prior["lane_wall_seconds"]["calibration"] == 777


@pytest.mark.parametrize("caps", [{"global_max_concurrency": 7}, {"global_max_concurrency": True},
                                 {"max_tool_requests_per_trial": 31}])
def test_invalid_protocol_limits_are_refused(measured, wp6_study, caps):
    protocol = load_protocol()
    protocol["caps"].update(caps)
    with pytest.raises(ValueError, match="protocol"):
        proposal(measured, wp6_study[0], protocol=protocol)


def test_phase_basis_and_duplicate_inputs_are_refused(measured, wp6_study):
    with pytest.raises(ValueError, match="calibration input roots"):
        proposal(measured, wp6_study[0], phase="collection")
    with pytest.raises(ValueError, match="counted twice"):
        propose_caps([measured, measured], study_directory=wp6_study[0], phase="calibration", protocol=load_protocol())
    with pytest.raises(ValueError, match="at least one"):
        propose_caps([], study_directory=wp6_study[0], phase="calibration", protocol=load_protocol())


def test_all_roots_contribute_verified_measurements(measured, floor_root, wp6_study):
    result = propose_caps([measured, floor_root], study_directory=wp6_study[0], phase="calibration",
                          protocol=load_protocol())
    assert len(result["inputs"]) == 2 and len(result["attempts_used"]) == 12
    assert result["measured_peaks"] == {"usage_total_tokens": 100001, "elapsed_seconds": 123.4,
                                        "tool_request_count": 6}
    assert {row["plan_hash"] for row in result["attempts_used"]} == {
        live.read_live_plan(root)["seal_hash"] for root in (measured, floor_root)}


def test_unarchived_rows_are_retained_as_ignored_inputs(measured, wp6_study, tmp_path):
    unstarted, _ = compat_root(tmp_path / "unstarted")
    result = propose_caps([measured, unstarted], study_directory=wp6_study[0], phase="calibration",
                          protocol=load_protocol())
    assert len(result["attempts_used"]) == 6 and len(result["attempts_ignored"]) == 6
    assert {row["reason"] for row in result["attempts_ignored"]} == {"not_archived:unrun"}


def test_calibration_prior_roots_must_be_inputs_and_are_verified(floor_root, tmp_path):
    rows, fixtures = [], {}
    for split, template in (("calibration", "firewall-change"), ("smoke", "token-issuance"),
                            ("collection", "release-request")):
        selected, built_fixtures = study_rows(split, template_id=template)
        rows.extend(selected)
        fixtures.update(built_fixtures)
    caps = caps_record(global_max_concurrency=1)
    study = write_study(tmp_path / "study", rows, fixtures, caps=caps)
    earlier = study / "roots" / "earlier"
    built = build_plan("calibration", study, caps=caps, compatibility_directories=[floor_root])
    live.prepare_live_root(earlier, built, study_directory=study)
    plan = live.read_live_plan(earlier)

    async def stop_after_first(*args, **kwargs):
        result = await live_runtime.run_live_observer(*args, **kwargs)
        (earlier / "STOP").touch()
        return result

    status = asyncio.run(run_phase(earlier, plan, Harness(tmp_path / "homes", packet_scripts(fixtures)),
                                   compatibility_directories=[floor_root], study_directory=study,
                                   observer=stop_after_first))
    assert status["live_model_call_starts"] == 1
    built = build_plan("calibration", study, caps=caps, revision="calibration-v2",
                       compatibility_directories=[floor_root], prior_roots=[earlier])
    later = study / "roots" / "later"
    live.prepare_live_root(later, built, study_directory=study, prior_roots=[earlier], bundle=fake_bundle())
    with pytest.raises(ValueError, match="supply every sealed prior root"):
        proposal(later, study, phase="collection")
    result = propose_caps([later, earlier], study_directory=study, phase="collection", protocol=load_protocol())
    later_input = next(row for row in result["inputs"] if row["plan_hash"] == live.read_live_plan(later)["seal_hash"])
    assert later_input["consumed_attempt_ledger"]["checked"] is True
    assert later_input["consumed_attempt_ledger"]["prior_roots"] == 1
    assert len(result["attempts_used"]) == 1


def test_freeze_validates_caps_records_approval_and_preserves_proposal(measured, wp6_study):
    original = proposal(measured, wp6_study[0])
    before = deepcopy(original)
    frozen = freeze_caps(original, "Approve these caps for calibration.")
    assert original == before
    assert frozen["caps"]["caps_status"] == frozen["status"] == "frozen"
    assert validate_caps_record(frozen["caps"]) == frozen["caps"]
    assert frozen["approval"] == {"status": "approved", "text": "Approve these caps for calibration.",
                                   "proposal_hash": original["seal_hash"], "caps_hash": content_hash(frozen["caps"])}
    frozen["caps"]["trial"]["max_trial_wall_seconds"] = 1
    assert original == before
    check_seal(freeze_caps(original, "Approved."))


@pytest.mark.parametrize("approval", ["", " \n", None, 123])
def test_freeze_requires_approval_text(measured, wp6_study, approval):
    with pytest.raises(ValueError, match="approval text"):
        freeze_caps(proposal(measured, wp6_study[0]), approval)


def test_freeze_refuses_tampering_and_reapproval(measured, wp6_study):
    original = proposal(measured, wp6_study[0])
    tampered = deepcopy(original)
    tampered["caps"]["trial"]["reserved_tokens_per_trial"] += 5000
    with pytest.raises(ValueError, match="seal mismatch"):
        freeze_caps(tampered, "Approved.")
    with pytest.raises(ValueError, match="unapproved"):
        freeze_caps(freeze_caps(original, "Approved."), "Approved again.")


def test_cli_proposes_freezes_and_refuses_overwrite(measured, wp6_study, tmp_path, capsys):
    output = tmp_path / "proposal.json"
    argv = ["propose-caps", "--study", str(wp6_study[0]), "--phase", "calibration",
            "--root", str(measured), "--output", str(output)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["live_model_calls"] == 0
    original_bytes = output.read_bytes()
    assert main(argv) == 2
    assert "FileExistsError" in json.loads(capsys.readouterr().out)["error"]
    assert output.read_bytes() == original_bytes
    caps_path = tmp_path / "caps.json"
    freeze_argv = ["freeze-caps", "--proposal", str(output), "--approval-text", "Approved for calibration.",
                   "--output", str(caps_path)]
    assert main(freeze_argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["live_model_calls"] == 0
    assert validate_caps_record(read_json(caps_path))["caps_status"] == "frozen"
    approval_path = tmp_path / "caps-approval.json"
    assert read_sealed(approval_path)["approval"]["text"] == "Approved for calibration."
    assert output.read_bytes() == original_bytes
    frozen_bytes = caps_path.read_bytes()
    assert main(freeze_argv) == 2
    assert "FileExistsError" in json.loads(capsys.readouterr().out)["error"]
    assert caps_path.read_bytes() == frozen_bytes
    caps_path.unlink()
    assert main(freeze_argv) == 2  # An existing approval sidecar also refuses the entire write.
    assert "FileExistsError" in json.loads(capsys.readouterr().out)["error"]
    assert not caps_path.exists()
