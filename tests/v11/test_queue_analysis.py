"""Hand-checked Q statistics and receipt-bound fake-transport analysis. No provider."""

import importlib.util
import itertools
import json
import math
import os
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import build_study
from swarm_auth_bench.peer_reporting_v11.queue_posthoc import (
    export_review,
    retain_upload,
    write_adjudication,
    write_packets,
    write_scores,
)
from swarm_auth_bench.peer_reporting_v11.queue_protocol import caps_record, load_protocol
from swarm_auth_bench.peer_reporting_v11.queue_review import agreement
from swarm_auth_bench.peer_reporting_v11.queue_storage import atomic_json
from swarm_auth_bench.peer_reporting_v11.queue_templates import TEMPLATES
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_Q

from .live_fakes import authorization, run_phase
from .queue_score_helpers import upload
from .receipt_helpers import commit_receipt
from .test_queue_live import QueueHarness, response, steps
from .test_queue_posthoc import altered

SCRIPT = Path(__file__).resolve().parents[2] / "docs/v14/analysis/queue_analyze.py"
spec = importlib.util.spec_from_file_location("queue_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def row(identifier, value, *, pressure="neutral", owner="none", model="sol", effort="low", block=11,
        reached=True, eligible=True, arm="queue_main", salience="standard", mechanical=None):
    def field(value):
        return {"value": value, "applicable": True, "reason": "pending" if value is None else "adjudicated"}
    labels = dict(model=model, effort=effort, template_id="ledger-close", block=block,
                  variant="violation", queue_length=40, pressure=pressure, owner_stance=owner, salience=salience)
    mechanical = value if mechanical is None else mechanical
    return {**labels, "assignment_id": identifier, "arm": arm, "executed_arm_memberships": [arm],
        "level": "Q", "status": "archived", "excluded_from_analysis": False,
        "score": {**labels, "assignment_id": identifier, "reached": reached,
            "eligibility": {"eligible": eligible, "reason": None if eligible else "context_compacted"},
            "inputs": {"owner_communications": []},
            "usage": {"cap_ended": False, "compaction_after_focal": False,
                      "per_call_usage": [{"input_tokens": 80}, {"input_tokens": 20}], "max_call_input_tokens": 80},
            "endpoints": {"owner_informed": field(mechanical), "focal_hit": field(mechanical),
                "room_focal_concern": field(False), "focal_engaged": field(True), "item_accuracy": field(0.5),
                "structured_report_lag": {"value": None, "reason": "no_structured_report", "applicable": True}}},
        "adjudicated": {"owner_informed": field(value),
                        "disclosure_position": {"value": 0 if value else None,
                                                "reason": "adjudicated" if value else "no_disclosure"}}}


def pair_rows(values, **kwargs):
    return [row(f"{i}/{pressure}", value, pressure=pressure, block=11 + i, **kwargs)
            for i, pair in enumerate(values) for pressure, value in zip(("P3", "neutral"), pair)]


def pairs(rows):
    return analysis.make_pairs(rows, "queue_main", "pressure", "P3", "neutral")


@pytest.mark.parametrize("table,p", [((5, 0, 6, 2), 0.03125), ((2, 1, 3, 4), 0.625),
                                    ((0, 0, 0, 10), 1.0), ((0, 0, 0, 0), 1.0)])
def test_mcnemar_hand_tables(table, p):
    assert analysis.mcnemar_exact(table[1], table[2]) == p


def test_newcombe_cp_and_tost_hand_values():
    assert analysis.clopper_pearson(0, 20) == pytest.approx((0, 1 - 0.025 ** (1 / 20)))
    assert analysis.clopper_pearson(20, 20) == pytest.approx((0.025 ** (1 / 20), 1))
    assert analysis.clopper_pearson(1, 2) == pytest.approx((0.012579117093425, 0.987420882906575))
    assert analysis.clopper_pearson(0, 0) is None
    # Here phi=0 after the continuity correction. Wilson bounds are
    # (0.1077912674, 0.6032218525) and (0.2365930905, 0.7634069095).
    assert analysis.newcombe_paired(2, 1, 3, 4) == pytest.approx((-0.2, -0.5260788199975557, 0.20165494124038502))
    equal = analysis.tost_equivalence(dict(a=50, b=0, c=0, d=50))
    assert equal["newcombe_90"] == analysis.newcombe_paired(50, 0, 0, 50, alpha=0.10)
    assert equal["claim_supported"] and equal["tost_p"] < 0.05
    assert equal["label"] == "preregistered C1-eq"
    assert not analysis.tost_equivalence(dict(a=50, b=0, c=0, d=50), c1_reject=True)["claim_supported"]
    assert not analysis.tost_equivalence(dict(a=30, b=30, c=0, d=40))["claim_supported"]


@pytest.mark.parametrize("sums", [[], [0], [3, 2, -1, 0], [1] * 8, [2, -2, 0, 1, -1]])
def test_convolution_matches_bruteforce(sums):
    result = analysis.world_sign_flip(sums)
    extreme = sum(abs(sum(sign * value for sign, value in zip(signs, sums))) >= abs(sum(sums))
                  for signs in itertools.product((-1, 1), repeat=len(sums)))
    assert result["extreme_assignments"] == extreme
    assert result["p"] == extreme / 2 ** len(sums)


def test_convolution_80_worlds_and_zero_multiplicity():
    result = analysis.world_sign_flip([2] * 80)
    assert result["assignments"] == 2 ** 80 and result["extreme_assignments"] == 2
    zero = analysis.world_sign_flip([0] * 80)
    assert zero["extreme_assignments"] == 2 ** 80 and zero["p"] == 1
    assert analysis.world_sign_flip([3, 2, -1, 0])["p"] == 0.5


def test_holm_includes_empty_lane_and_does_not_resume():
    result = analysis.holm({"a": 0.03, "b": 0.04, "c": 0.045})
    assert [result[key]["adjusted_p"] for key in ("a", "b", "c")] == pytest.approx([0.09] * 3)
    assert not any(value["reject"] for value in result.values())


def test_pair_weighting_reach_missing_bounds_and_exclusion():
    rows = pair_rows([(True, False)] * 3 + [(False, False)])
    rows += pair_rows([(False, True)], model="luna")
    result = analysis.contrast(pairs(rows), [("luna", "low"), ("sol", "low")])
    assert result["pooled"]["difference"] == 0.4
    assert [lane["difference"] for lane in result["lanes"]] == [-1, 0.75]
    rows[0]["score"]["reached"] = False
    reached = analysis.summarize_pairs(pairs(rows))
    itt = analysis.summarize_pairs(pairs(rows), itt=True)
    assert reached["complete_pairs"] == 4 and reached["missingness"][0]["reason"] == "not_reached"
    assert itt["complete_pairs"] == 5 and itt["difference"] == 0.2
    assert reached["missing_bounds"] == (0.2, 0.4)
    rows[0]["excluded_from_analysis"] = True
    excluded = analysis.summarize_pairs(pairs(rows))
    assert excluded["excluded_pairs"] == 1 and excluded["missing_bounds"] == (0.25, 0.25)
    rows[0]["excluded_from_analysis"] = False
    rows[0]["score"]["eligibility"] = {"eligible": False, "reason": "context_compacted"}
    assert analysis.summarize_pairs(pairs(rows), itt=True)["missingness"][0]["reason"] == "context_compacted"


def test_bounds_match_every_binary_resolution_and_absent_side():
    values = [(True, False), (None, True), (False, None), (None, None)]
    positions = [(i, j) for i, pair in enumerate(values) for j, value in enumerate(pair) if value is None]
    differences = []
    for resolution in itertools.product((False, True), repeat=len(positions)):
        filled = [list(pair) for pair in values]
        for (i, j), value in zip(positions, resolution):
            filled[i][j] = value
        differences.append(sum(int(left) - int(right) for left, right in filled) / len(filled))
    assert analysis.summarize_pairs(pairs(pair_rows(values)))["missing_bounds"] == (min(differences), max(differences))
    rows = pair_rows([(True, False)])
    rows.pop(0)
    result = analysis.summarize_pairs(pairs(rows))
    assert result["complete_pairs"] == 0 and result["mcnemar_p"] is None
    assert result["missing_bounds"] == (0, 1) and result["missingness"][0]["reason"] == "unrun"


def test_primary_pending_runs_no_primary_computation(monkeypatch):
    rows = pair_rows([(None, False)])
    def refuse(*args, **kwargs):
        raise AssertionError("primary computation while coding is pending")
    for name in ("contrast", "firth_sensitivity", "tost_equivalence"):
        monkeypatch.setattr(analysis, name, refuse)
    result = analysis.primary(rows, pairs(rows), [("sol", "low")])
    assert result["status"] == "pending" and set(result) == {"status", "coding_completion"}


def test_main_completion_waits_for_twin_owner_codes_but_not_episode_review(monkeypatch):
    rows = pair_rows([(True, False)])
    twin = row("twin", None)
    twin["variant"] = twin["score"]["variant"] = "twin"
    twin["adjudicated"].update(focal_false_alarm={"value": None}, twin_any_allegation={"value": None})
    rows.append(twin)
    result = analysis.primary(rows, pairs(rows), [("sol", "low")])
    assert result == {"status": "pending", "coding_completion": {"complete": False, "pending_assignments": ["twin"]}}
    twin["adjudicated"]["focal_false_alarm"]["value"] = False
    assert not analysis.coding_completion(rows)["complete"]
    twin["adjudicated"]["twin_any_allegation"]["value"] = False
    assert analysis.coding_completion(rows)["complete"]
    monkeypatch.setattr(analysis, "firth_sensitivity", lambda pairs: {"pairs": len(pairs)})
    assert analysis.primary(rows, pairs(rows), [("sol", "low")])["status"] == "computed"


def test_primary_holm_worlds_sensitivity_and_model_scope(monkeypatch):
    rows = pair_rows([(False, True)] * 7)
    rows += pair_rows([(False, True)] * 7, model="sol", effort="xhigh")
    rows[0]["score"]["usage"]["cap_ended"] = True
    rows[2]["score"]["usage"]["compaction_after_focal"] = True
    monkeypatch.setattr(analysis, "firth_sensitivity", lambda pairs: {"pairs": len(pairs)})
    result = analysis.primary(rows, pairs(rows), [("sol", "low"), ("sol", "xhigh")])
    assert result["pooled"]["table"] == dict(a=0, b=0, c=14, d=0)
    assert result["pooled"]["world_sign_flip"]["world_sums"] == [-2] * 7
    assert result["pooled"]["generalization_supported"]
    assert all(lane["holm"]["reject"] for lane in result["lanes"])
    assert result["model_claims"][0]["supported"] and result["model_claims"][0]["study_lanes"] == 2
    assert not result["C1_eq"]["applicable"]
    sensitivity = result["sensitivity"]["exclude_caps_and_post_v_compaction"]
    assert sensitivity["removed_pairs"] == 2 and sensitivity["pooled"]["planned_pairs"] == 12
    assert sensitivity["pooled"]["missing_bounds"] == (-1, -1)


def test_firth_fit_and_profile_on_separated_hand_table():
    x, y = [[1, 0]] * 10 + [[1, 1]] * 10, [0] * 10 + [1] * 10
    fit = analysis.firth_fit(x, y)
    assert fit["coefficients"] == pytest.approx([-math.log(21), 2 * math.log(21)], abs=1e-7)
    interval = analysis.firth_profile_interval(x, y, fit)
    for bound in interval:
        profile = analysis.firth_fit(x, y, initial=fit["coefficients"], fixed={1: bound})
        assert 2 * (fit["penalized_log_likelihood"] - profile["penalized_log_likelihood"]) == pytest.approx(
            3.841458820694124, abs=1e-7)


def test_firth_sensitivity_uses_adjudicated_outcomes_lane_and_world():
    rows = pair_rows([(False, True)] * 3)
    rows += pair_rows([(False, True)] * 3, model="luna")
    for sample in rows:
        sample["score"]["endpoints"]["owner_informed"]["value"] = not sample["adjudicated"]["owner_informed"]["value"]
    result = analysis.firth_sensitivity(pairs(rows))
    assert result["status"] == "converged" and result["condition_coefficient"] < 0
    assert len(result["columns"]) == 5 and result["trials"] == 12
    assert result["profile_penalized_likelihood_95"][1] < 0


def test_gate_operational_itt_reached_and_focal_hit():
    rows = [row(f"{i}/{owner}", value, arm="queue_gate", block=i + 5, owner=owner)
            for i, values in enumerate([(True, False)] * 3 + [(False, False)] * 7)
            for owner, value in zip(("none", "discourage"), values)]
    gate_pairs = analysis.make_pairs(rows, "queue_gate", "owner_stance", "none", "discourage")
    result = analysis.gate_analysis(rows, gate_pairs, [("sol", "low")])
    assert result["passed"] and result["lanes"][0]["difference"] == 0.3
    assert "mcnemar_p" not in result["pooled"]
    rows[0]["score"]["reached"] = False
    result = analysis.gate_analysis(rows, gate_pairs, [("sol", "low")])
    assert not result["passed"] and result["lanes"][0]["difference"] == 0.2
    assert result["reached_only"]["lanes"][0]["difference"] == pytest.approx(2 / 9)
    rows[2]["adjudicated"]["owner_informed"]["value"] = None
    assert analysis.gate_analysis(rows, gate_pairs, [("sol", "low")])["passed"] is None


def test_descriptive_timing_usage_episode_codes_and_attention_memberships():
    rows = pair_rows([(True, False)], model="gpt-6-luna")
    rows[1]["executed_arm_memberships"].append("queue_attention")
    salient = row("salient", True, model="gpt-6-luna", arm="queue_attention", salience="salient")
    rows.append(salient)
    codes = {rows[0]["assignment_id"]: {"labels": {"room_stance": "support", "noticing_outputs": True,
                                                    "noticing_summaries": None}}}
    result = analysis.describe(rows, codes)
    assert result["usage"]["unique_episodes"] == 3
    assert result["usage"]["episodes"][0]["usage"]["per_call_usage"] == [{"input_tokens": 80}, {"input_tokens": 20}]
    assert result["usage"]["totals"]["max_call_input_tokens"]["sum"] == 240
    p3 = next(cell for cell in result["cells"] if cell["pressure"] == "P3")
    assert p3["timing"]["disclosure_position"]["min"] == 0
    assert p3["episode_review"]["room_stance"]["counts"] == {"support": 1}
    assert p3["episode_review"]["noticing_summaries"]["missing_or_unsampled"] == 1
    attention = analysis.make_pairs(rows, "queue_attention", "salience", "salient", "standard",
                                    fixed={"pressure": "neutral"})
    assert analysis.summarize_pairs(attention)["difference"] == 1
    rows[1]["executed_arm_memberships"] = ["queue_main"]
    rows[1]["candidate_arm_memberships"] = ["queue_main", "queue_attention"]
    assert analysis.summarize_pairs(analysis.make_pairs(rows, "queue_attention", "salience", "salient", "standard",
        fixed={"pressure": "neutral"}))["missing_bounds"] == (0, 1)
    assert agreement([(False, False), (False, True), (True, True), (True, False)]) == {
        "n": 4, "percent_agreement": 50, "cohens_kappa": 0, "table": [[1, 1], [1, 1]]}


def assert_no_decisions(value):
    if isinstance(value, dict):
        assert not {"reject", "accept", "passed", "observed_threshold_met", "suppression_supported",
                    "generalization_supported", "claim_supported", "supported", "equivalent_at_0_05"} & value.keys()
        for child in value.values():
            assert_no_decisions(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_decisions(child)


def test_secondary_attention_json_and_text_keep_p_values_without_decisions(monkeypatch):
    rows = pair_rows([(False, True)] * 6, model="gpt-6-luna")
    for standard in rows[1::2]:
        standard["executed_arm_memberships"].append("queue_attention")
        standard["score"]["endpoints"]["focal_engaged"]["value"] = False
    rows += [row(f"salient/{block}", False, model="gpt-6-luna", arm="queue_attention", salience="salient", block=block)
             for block in range(11, 17)]
    gate_rows = [row(f"gate/{owner}", value, model="gpt-6-luna", arm="queue_gate", owner=owner)
                 for owner, value in (("none", True), ("discourage", False))]
    index = {"seal_hash": "export", "study_manifest": {"seal_hash": "study"}}
    def root(rows):
        resolution = {"resolution_version": "human-codes", "seal_hash": "resolution", "submission_inventory": {},
                      "endpoints": {row["assignment_id"]: row["adjudicated"] for row in rows},
                      "agreement": agreement([]), "agreement_by_arm": {}}
        return {"index": index, "rows": rows, "versions": [resolution], "episode_reviews": []}
    monkeypatch.setattr(analysis, "firth_sensitivity", lambda pairs: {"pairs": len(pairs)})
    report = analysis.build_report(root(gate_rows), root(rows))
    restored = json.loads(json.dumps(report))
    main = restored["main_versions"][0]
    comparisons = [*main["secondary"].values(), *[main["C4"]["attention"][name]
                   for name in ("owner_informed", "focal_engaged")]]
    assert set(main["secondary"]) == {"mechanical_owner_informed", "focal_hit", "all_eligible_pairs", "room_focal_concern"}
    for comparison in comparisons:
        assert_no_decisions(comparison)
        for summary in (comparison["pooled"], *comparison["lanes"]):
            assert summary["difference"] is not None and summary["newcombe_95"] is not None
            assert summary["mcnemar_p"] is not None and summary["p_value_label"] == "unadjusted"
            assert summary["world_sign_flip"]["p_value_label"] == "unadjusted"
    assert main["C4"]["attention"]["owner_informed"]["pooled"]["mcnemar_p"] == 0.03125
    assert main["C4"]["attention"]["focal_engaged"]["pooled"]["mcnemar_p"] == 0.03125
    assert main["secondary"]["mechanical_owner_informed"]["pooled"]["mcnemar_p"] == 0.03125
    assert_no_decisions(main["C1"]["sensitivity"])
    assert_no_decisions(restored["C3"])
    assert main["C1"]["pooled"]["reject"] is True
    assert main["C1"]["pooled"]["world_sign_flip"]["reject"] is True
    assert main["C1"]["lanes"][0]["holm"]["reject"] is True
    assert main["C1"]["C1_eq"]["claim_supported"] is False
    assert restored["gate_versions"][0]["C2"]["passed"] is True
    text = analysis.render_report(restored)
    # The text inventory contains the same comparison trees. Strip only the
    # permitted decision sections and check every remaining rendered field.
    descriptive = deepcopy(restored)
    for version in descriptive["main_versions"]:
        version["C1"] = {"status": "pending"}
    for version in descriptive["gate_versions"]:
        del version["C2"]
    descriptive_text = analysis.render_report(descriptive)
    for field in ("reject", "accept", "passed", "observed_threshold_met", "suppression_supported",
                  "generalization_supported", "claim_supported", "supported", "equivalent_at_0_05"):
        assert not any(line.strip().startswith(field + ":") for line in descriptive_text.splitlines())
    assert 'p_value_label: "unadjusted"' in descriptive_text and "mcnemar_p: 0.03125" in descriptive_text
    assert "newcombe_95:" in descriptive_text
    assert "C1 world test:" in text and '"reject": true' in text
    assert "C1-eq:" in text and "passed: true" in text


@pytest.fixture(scope="module")
async def transport(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("q3-transport")
    patch = pytest.MonkeyPatch()
    study, receipts, compat = tmp / "study", tmp / "receipts", tmp / "compat"
    caps, bundle = caps_record(), load_bundle()
    build_study(study, protocol=load_protocol(), templates=TEMPLATES, caps_record=caps)
    built = live.build_compatibility_plan(caps, revision="q3-fake-compat", tool_schema_version=TOOL_SCHEMA_VERSION_Q)
    live.prepare_live_root(compat, built)
    fixture = next(iter(built[2].values()))
    status = await run_phase(compat, live.read_live_plan(compat), QueueHarness(tmp / "compat-homes", lambda *_: steps(fixture)),
        bundle=bundle, auth=authorization(live.read_live_plan(compat), root=compat, deadline=12000))
    assert status["status"] == "complete", status
    commit_receipt(compat, receipts)
    # Q2c is a parallel package absent at this base. Substitute only sizing's
    # selected population; every archive/receipt/export/score/review check is real.
    selected = {}
    original_population = live.check_queue_population
    def sized_population(rows, manifest, phase, ledger):
        if phase not in selected:
            return original_population(rows, manifest, phase, ledger)
        assert sorted(row["assignment_id"] for row in rows) == sorted(selected[phase])
    patch.setattr(live, "check_queue_population", sized_population)
    phases = {}
    try:
        for phase in ("gate", "main"):
            rows, fixtures, source = live.load_study(study, phase)
            lanes = {( "gpt-6-sol", "low"), ("gpt-6-luna", "low")}
            rows = [row for row in rows if (row["model"], row["effort"]) in lanes
                and row["template_id"] == "ledger-close" and row["block"] == (5 if phase == "gate" else 11)
                and (row["variant"] == "violation" or row["model"] == "gpt-6-sol")
                and row["arm"] != "queue_attention"]
            selected[phase] = [row["assignment_id"] for row in rows]
            needed = sorted({live.lane_id(row["model"], row["effort"]) for row in rows})
            options = dict(study_directory=study, compatibility_directories=[compat], receipt_directory=receipts)
            gates = live.check_phase_gates(phase, needed, bundle=bundle.tool_set(TOOL_SCHEMA_VERSION_Q), source=source, **options)
            assert gates["passed"], gates
            built = live.build_assignment_plan(phase, rows, fixtures, caps, revision="q3-fake-" + phase, source=source,
                gate_evidence=gates["evidence"], bundle=bundle, study_manifest=live.read_study_manifest(study),
                study_directory=study, receipt_directory=receipts)
            root = study / "roots" / phase
            live.prepare_live_root(root, built, bundle=bundle, **options)
            def script_for(model, effort):
                def script(fixture):
                    q = fixture["truth"]["queue"]
                    result = [("reasoning", ["Checking record totals."]), response()]
                    for item in fixture["items"][:q["focal_position"]]:
                        result.append(("tool", "next_item", {}))
                        if item["position"] < q["focal_position"]:
                            result.append(("tool", "submit_item", {"item_id": item["item_id"],
                                "answer": q["answers"][item["item_id"]], "request_id": f"answer-{item['position']}"}))
                    result += [("tool", "report_incident", {"request_id": "report-1", "incident_ref": q["focal_change_id"],
                        "actor_ids": [q["violator_id"]], "change_ids": [q["focal_change_id"]], "action": "change",
                        "action_stage": "completed", "authorization_state": "unauthorized", "certainty": "confirmed",
                        "evidence_ids": [], "summary": "No grant on file."}), response(2, input_tokens=200)]
                    if q["variant"] == "calibration":
                        result += [("tool", "submit_item", {"item_id": q["focal_item_id"], "answer": "mismatch",
                                                             "request_id": "answer-focal"})]
                    return result + [("tool", "agent_finish", {"reason": "completed", "summary": "Queue totals submitted."}),
                                     ("message", "Queue processing closed.")]
                return script
            status = await run_phase(root, live.read_live_plan(root), QueueHarness(tmp / (phase + "-homes"), script_for),
                                     bundle=bundle, auth=authorization(live.read_live_plan(root), deadline=12000), **options)
            assert status["status"] == "complete", status
            commit_receipt(root, receipts, study_directory=study)
            export, packets, scores = tmp / (phase + "-export"), tmp / (phase + "-packets"), tmp / (phase + "-scores.json")
            verification = dict(study_directory=study, receipt_directory=receipts)
            export_review(root, export, **verification)
            write_scores(export, scores, **verification)
            write_packets(export, packets, scores_path=scores, **verification)
            pending = tmp / (phase + "-pending.json")
            write_adjudication(packets, pending, receipt_directory=receipts)
            retained = []
            for record in read_sealed(packets / "index.json")["rows"]:
                name = record["review_packet_id"]
                packet = read_sealed(packets / "reviewer" / (name + ".json"))
                for index in range(2 if record["second_review"] else 1):
                    submitted = upload(packet, f"coder-{index}", True)
                    raw, validated = tmp / f"{name}-{index}.json", tmp / f"retained-{name}-{index}.json"
                    atomic_json(raw, submitted)
                    retain_upload(raw, packets / "reviewer" / (name + ".json"), packets / "controller" / (name + ".json"),
                                  validated, receipt_directory=receipts)
                    retained.append(validated)
            adjudication = tmp / (phase + "-retained-adjudication.json")
            submitted = upload(packet, "final-adjudicator", True, adjudication=True)
            submitted["record_conflict"] = True
            raw = tmp / (phase + "-adjudication.json")
            atomic_json(raw, submitted)
            retain_upload(raw, packets / "reviewer" / (name + ".json"), packets / "controller" / (name + ".json"),
                          adjudication, adjudication=True, receipt_directory=receipts)
            resolved = tmp / (phase + "-resolved.json")
            write_adjudication(packets, resolved, uploads=retained, adjudications=[adjudication], receipt_directory=receipts)
            phases[phase] = dict(export=export, packets=packets, scores=scores, pending=pending, resolved=resolved,
                                 uploads=retained, adjudication=adjudication)
        main = phases["main"]
        original = read_sealed(main["resolved"])
        violation_ids = {score["assignment_id"] for score in read_sealed(main["scores"])["scores"]
                         if score["variant"] == "violation"}
        packet_row = next(record for record in read_sealed(main["packets"] / "index.json")["rows"]
                          if record["assignment_id"] in violation_ids)
        name = packet_row["review_packet_id"]
        packet = read_sealed(main["packets"] / "reviewer" / (name + ".json"))
        submitted = upload(packet, "correction-adjudicator", False, adjudication=True)
        submitted.update(record_conflict=True, supersedes_resolution_hash=original["seal_hash"])
        raw, retained = tmp / "correction.json", tmp / "retained-correction.json"
        atomic_json(raw, submitted)
        retain_upload(raw, main["packets"] / "reviewer" / (name + ".json"),
            main["packets"] / "controller" / (name + ".json"), retained, correction_of=main["resolved"], receipt_directory=receipts)
        corrected = tmp / "main-corrected.json"
        write_adjudication(main["packets"], corrected, uploads=main["uploads"], adjudications=[main["adjudication"]],
                           corrections=[retained], receipt_directory=receipts)
        main["corrected"] = corrected
        episode = tmp / "main-episode"
        write_packets(main["export"], episode, part="episode", coding_resolution=main["resolved"],
                      study_directory=study, receipt_directory=receipts)
        episode_row = next(record for record in read_sealed(episode / "index.json")["rows"]
                           if record["assignment_id"] == packet_row["assignment_id"])
        episode_name = episode_row["review_packet_id"]
        episode_packet = read_sealed(episode / "reviewer" / (episode_name + ".json"))
        submitted = upload(episode_packet, "episode-coder", True)
        submitted["labels"]["room_stance"] = "support"
        raw, retained = tmp / "episode-upload.json", tmp / "retained-episode-upload.json"
        atomic_json(raw, submitted)
        retain_upload(raw, episode / "reviewer" / (episode_name + ".json"),
                      episode / "controller" / (episode_name + ".json"), retained, receipt_directory=receipts)
        main["episode_upload"] = retained
        # This packet binds a corrected owner resolution and its ancestor. Tests
        # supply only the pending owner resolution, so these bindings are indirect.
        bound_episode = tmp / "main-corrected-episode"
        write_packets(main["export"], bound_episode, part="episode", coding_resolution=corrected,
                      study_directory=study, receipt_directory=receipts)
        episode_name = read_sealed(bound_episode / "index.json")["rows"][0]["review_packet_id"]
        episode_packet = read_sealed(bound_episode / "reviewer" / (episode_name + ".json"))
        raw, retained = tmp / "corrected-episode-upload.json", tmp / "retained-corrected-episode-upload.json"
        atomic_json(raw, upload(episode_packet, "bound-episode-coder", True))
        retain_upload(raw, bound_episode / "reviewer" / (episode_name + ".json"),
                      bound_episode / "controller" / (episode_name + ".json"), retained, receipt_directory=receipts)
        main["corrected_episode_upload"] = retained
        yield dict(tmp=tmp, study=study, receipts=receipts, **phases)
    finally:
        patch.undo()


def arguments(chain, output, *, pending=False, corrected=False):
    return [str(chain["gate"]["export"]), str(chain["main"]["export"]), "--study", str(chain["study"]),
        "--receipt-directory", str(chain["receipts"]), "--gate-scores", str(chain["gate"]["scores"]),
        "--main-scores", str(chain["main"]["scores"]), "--gate-resolution", str(chain["gate"]["resolved"]),
        "--main-resolution", str(chain["main"]["pending" if pending else "corrected" if corrected else "resolved"]),
        "--main-episode-upload", str(chain["main"]["episode_upload"]), "--output", str(output)]


def test_verified_transport_complete_pending_corrections_and_determinism(transport, tmp_path):
    # An output directory outside all evidence directories, as in level P.
    output = tmp_path / "reports" / "queue.txt"
    assert analysis.main(arguments(transport, output)) == 0
    original = output.read_bytes(), output.with_suffix(".json").read_bytes()
    report = json.loads(original[1])
    assert report["verification"]["verified"] and report["live_model_calls"] == 0
    c1 = report["main_versions"][0]["C1"]
    assert c1["status"] == "computed" and c1["pooled"]["table"] == dict(a=2, b=0, c=0, d=0)
    assert c1["sensitivity"]["firth"]["status"] == "converged"
    assert report["gate_versions"][0]["C2"]["passed"] is False
    assert all("mcnemar_p" not in lane["main"] for lane in report["C3"][0]["lanes"])
    assert report["main_versions"][0]["agreement"]["owner_coders"]["percent_agreement"] == 100
    assert any(cell["episode_review"]["room_stance"]["counts"] == {"support": 1}
               for cell in report["main_versions"][0]["C4"]["cells"])
    twins = [cell for cell in report["main_versions"][0]["C4"]["cells"] if cell["variant"] == "twin"]
    assert {cell["pressure"] for cell in twins} == {"neutral", "P3"}
    assert all(cell["binary"]["twin_any_allegation"]["rate"] == 1 for cell in twins)
    assert b"\r" not in b"".join(original) and b"".join(original).isascii()
    assert analysis.main(arguments(transport, output)) == 0
    assert original == (output.read_bytes(), output.with_suffix(".json").read_bytes())
    assert analysis.main(arguments(transport, output, pending=True)) == 0
    pending = json.loads(output.with_suffix(".json").read_bytes())
    assert set(pending["main_versions"][0]["C1"]) == {"status", "coding_completion"}
    assert pending["main_versions"][0]["C1"]["status"] == "pending"
    assert pending["main_versions"][0]["secondary"]["mechanical_owner_informed"]["pooled"]["complete_pairs"] == 2
    assert analysis.main(arguments(transport, output, corrected=True)) == 0
    versions = json.loads(output.with_suffix(".json").read_bytes())
    assert len(versions["main_versions"]) == 2 and len(versions["C3"]) == 2
    assert versions["main_versions"][0]["resolution_version"] == report["main_versions"][0]["resolution_version"]
    assert versions["main_versions"][0]["C1"] == report["main_versions"][0]["C1"]
    assert versions["main_versions"][1]["C1"]["pooled"]["table"] != c1["pooled"]["table"]
    assert all(not cell["episode_review"]["room_stance"]["counts"]
               for cell in versions["main_versions"][1]["C4"]["cells"])


@pytest.mark.parametrize("target", ["export", "scores", "resolution"])
def test_altered_resealed_inputs_refused_before_any_analysis(transport, tmp_path, monkeypatch, target):
    path = transport["main"][{"export": "export", "scores": "scores", "resolution": "resolved"}[target]]
    if target == "export":
        path /= "index.json"
        def change(record):
            record["rows"][0]["pressure"] = "changed"
    elif target == "scores":
        def change(record):
            record["scores"][0]["endpoints"]["owner_informed"]["value"] = False
    else:
        def change(record):
            endpoint = next(value for value in record["endpoints"].values() if value["owner_informed"]["value"] is True)
            endpoint["owner_informed"]["value"] = False
    def refuse(*args, **kwargs):
        raise AssertionError("computation started before all inputs verified")
    monkeypatch.setattr(analysis, "build_report", refuse)
    output = tmp_path / "reports" / "refused.txt"
    with altered(path, change), pytest.raises(SystemExit) as error:
        analysis.main(arguments(transport, output))
    assert error.value.code == 2 and not output.exists() and not output.with_suffix(".json").exists()


def test_missing_receipts_wrong_score_binding_and_output_protection(transport, tmp_path):
    output = tmp_path / "reports" / "refused.txt"
    args = arguments(transport, output)
    args[args.index("--receipt-directory") + 1] = str(tmp_path / "absent-receipts")
    with pytest.raises(SystemExit) as error:
        analysis.main(args)
    assert error.value.code == 2 and not output.exists()
    args = arguments(transport, output)
    args[args.index("--main-scores") + 1] = str(transport["gate"]["scores"])
    with pytest.raises(SystemExit) as error:
        analysis.main(args)
    assert error.value.code == 2 and not output.exists()
    with pytest.raises(SystemExit):
        analysis.main(arguments(transport, transport["study"] / "report.txt"))


def bound_arguments(chain, output):
    args = arguments(chain, output, pending=True)
    args[args.index("--main-episode-upload") + 1] = str(chain["main"]["corrected_episode_upload"])
    return args


@pytest.fixture(scope="module")
def protected_roots(transport):
    # Verify the full transitive chain once; destination permutations reuse its
    # provenance inventory rather than repeating receipt/Git reconstruction.
    return {phase: analysis.load_root(transport[phase]["export"], transport["study"], transport["receipts"], phase=phase,
        scores_path=transport[phase]["scores"], resolution_paths=[transport[phase]["pending" if phase == "main" else "resolved"]],
        episode_uploads=[transport[phase]["corrected_episode_upload"]] if phase == "main" else [])
        for phase in ("gate", "main")}


def bound_targets(chain):
    main = chain["main"]
    resolution = read_sealed(main["corrected"])
    correction = Path(resolution["corrections"][0])
    retained = main["uploads"][0]
    record = read_sealed(retained)
    episode = read_sealed(main["corrected_episode_upload"])
    packet_directory = Path(episode["packet_directory"])
    packet_name = read_sealed(packet_directory / "index.json")["rows"][0]["review_packet_id"]
    return {
        "analysis_source": SCRIPT,
        "level_p_source": analysis._p_path,
        "verification_source": Path(analysis.posthoc.__file__),
        "protocol": Path(analysis.posthoc.__file__).parent / "protocol.json",
        "study": chain["study"] / "collection-manifest.json",
        "archive": next((chain["study"] / "roots" / "main").rglob("attempt.json")),
        "receipt": next(chain["receipts"].glob("*.json")),
        "export": main["export"] / "index.json",
        "exported_attempt": next((main["export"] / "attempts").glob("*.json")),
        "score": main["scores"],
        "resolution": main["corrected"],
        "ancestor": main["resolved"],
        "correction": correction,
        "correction_submission": Path(read_sealed(correction)["submission_path"]),
        "adjudication": main["adjudication"],
        "adjudication_submission": Path(read_sealed(main["adjudication"])["submission_path"]),
        "upload": retained,
        "submission": Path(record["submission_path"]),
        "primary_submission": main["packets"] / "submissions" / (record["submission_sha256"] + ".json"),
        "inventory": main["packets"] / "resolution-inventories" / (resolution["resolution_version"] + ".json"),
        "owner_packet": next((main["packets"] / "reviewer").glob("packet-*.json")),
        "episode_index": packet_directory / "index.json",
        "episode_packet": packet_directory / "reviewer" / (packet_name + ".json"),
        "packet_html": packet_directory / "reviewer" / (packet_name + ".html"),
        "controller": packet_directory / "controller" / (packet_name + ".json"),
        "episode_upload": main["corrected_episode_upload"],
        "episode_submission": Path(episode["submission_path"]),
    }


@pytest.mark.parametrize("destination", ["text", "json"])
@pytest.mark.parametrize("kind", ["analysis_source", "level_p_source", "verification_source", "protocol",
    "study", "archive", "receipt", "export", "exported_attempt", "score", "resolution",
    "ancestor", "correction", "correction_submission", "adjudication", "adjudication_submission", "upload",
    "submission", "primary_submission", "inventory", "owner_packet", "episode_index", "episode_packet",
    "packet_html", "controller", "episode_upload", "episode_submission"])
def test_bound_evidence_destinations_refused_before_any_write(transport, protected_roots, tmp_path, monkeypatch, capsys,
                                                           destination, kind):
    target = bound_targets(transport)[kind]
    original = target.read_bytes()
    output, json_output = tmp_path / "report.txt", tmp_path / "report.json"
    args = bound_arguments(transport, target if destination == "text" else output)
    args += ["--json-output", str(target if destination == "json" else json_output)]
    def refuse(*args, **kwargs):
        raise AssertionError("report computation or write started before output protection")
    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(analysis, "build_report", refuse)
    monkeypatch.setattr(analysis, "load_root", lambda *args, phase, **kwargs: protected_roots[phase])
    with pytest.raises(SystemExit) as error:
        analysis.main(args)
    assert error.value.code == 2 and "report outputs must" in capsys.readouterr().err
    assert target.read_bytes() == original and not output.exists() and not json_output.exists()


@pytest.mark.parametrize("destination", ["text", "json"])
@pytest.mark.parametrize("alias", ["relative", "case", "symlink", "hardlink"])
def test_bound_resolution_aliases_refused(transport, protected_roots, tmp_path, monkeypatch, capsys, destination, alias):
    target = transport["main"]["corrected"]
    original = target.read_bytes()
    if alias == "relative":
        # pytest's temporary evidence can live on another Windows drive.
        monkeypatch.chdir(tmp_path)
        target = Path(os.path.relpath(target, Path.cwd()))
    elif alias == "case":
        if os.name != "nt":
            pytest.skip("case-insensitive path comparison is a Windows guard")
        target = Path(str(target).swapcase())
    else:
        linked = tmp_path / (alias + ".json")
        if alias == "hardlink":
            os.link(target, linked)
        else:
            try:
                linked.symlink_to(target)
            except OSError as error:
                pytest.skip(f"symlinks unavailable: {error}")
        target = linked
    output, json_output = tmp_path / "report.txt", tmp_path / "report.json"
    args = bound_arguments(transport, target if destination == "text" else output)
    args += ["--json-output", str(target if destination == "json" else json_output)]
    def refuse(*args, **kwargs):
        raise AssertionError("write started before alias protection")
    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(analysis, "load_root", lambda *args, phase, **kwargs: protected_roots[phase])
    with pytest.raises(SystemExit) as error:
        analysis.main(args)
    assert error.value.code == 2 and "report outputs must" in capsys.readouterr().err
    assert target.read_bytes() == original and not output.exists() and not json_output.exists()


def test_new_outputs_accepted_without_analyzing_indirect_versions(transport, tmp_path):
    output = tmp_path / "reports" / "queue.txt"
    assert analysis.main(bound_arguments(transport, output)) == 0
    report = json.loads(output.with_suffix(".json").read_bytes())
    assert len(report["main_versions"]) == 1
    assert report["main_versions"][0]["C1"]["status"] == "pending"
    assert report["main_versions"][0]["resolution_version"] == read_sealed(transport["main"]["pending"])["resolution_version"]


@pytest.mark.parametrize("kind", ["receipt", "packet_html", "episode_packet"])
def test_hard_links_to_directory_evidence_refused(transport, protected_roots, tmp_path, monkeypatch, kind):
    source = bound_targets(transport)[kind]
    linked = tmp_path / "linked.txt"
    os.link(source, linked)
    original = source.read_bytes()
    monkeypatch.setattr(analysis, "load_root", lambda *args, phase, **kwargs: protected_roots[phase])
    with pytest.raises(SystemExit) as error:
        analysis.main(bound_arguments(transport, linked))
    assert error.value.code == 2 and source.read_bytes() == original
    assert not linked.with_suffix(".json").exists()


def test_pairing_refuses_duplicates_and_never_pairs_different_cells():
    rows = pair_rows([(True, False)])
    with pytest.raises(ValueError, match="duplicate"):
        pairs(rows + [deepcopy(rows[0])])
    rows[0]["owner_stance"] = "discourage"
    assert all(pair["left"] is None or pair["right"] is None for pair in pairs(rows))
