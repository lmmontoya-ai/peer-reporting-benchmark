"""Offline statistical checks and receipt-bound fake-transport integration."""

import ast
import itertools
import json
import math
import runpy
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal

# Reuse the existing transport fixture.
from .test_pressure_successors import successor_inputs  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[2] / "docs/v13/analysis/pressure_core_analyze.py"
analysis = SimpleNamespace(**runpy.run_path(str(SCRIPT)))


def level_s_statistics():
    """Load only the legacy statistics; its top-level export reads must not run."""
    path = SCRIPT.parents[2] / "v12/analysis/social_grid_analyze.py"
    tree = ast.parse(path.read_bytes())
    names = {"binom_cdf", "mcnemar_exact", "clopper_pearson", "wilson", "newcombe_paired"}
    subset = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name in names], type_ignores=[])
    namespace = {"math": math}
    exec(compile(subset, str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


def fake_rows(table=(91, 7, 1, 21)):
    """A full sealed-row-shaped core, with a prescribed pooled H1 table."""
    models = analysis.load_protocol()["models"]
    outcomes = list(itertools.chain.from_iterable([[(True, True), (True, False), (False, True),
                                                   (False, False)][i]] * count
                                                  for i, count in enumerate(table)))
    assert len(outcomes) == 120
    rows, offset = [], 0
    for model, effort in itertools.product(models, ("low", "xhigh")):
        for template, block in analysis.WORLDS:
            for k in analysis.KS:
                left, right = outcomes[offset]
                offset += 1
                for visibility, pressure in itertools.product(analysis.VISIBILITIES, analysis.PRESSURES):
                    value = (left if pressure == "P3" else right) if visibility == "visible" else True
                    rows.append(fake_row(model, effort, template, block, k, visibility, pressure, value))
            for pressure in analysis.PRESSURES:
                rows.append(fake_row(model, effort, template, block, 0, "visible", pressure, pressure == "P3"))
    return rows


def fake_row(model, effort, template, block, k, visibility, pressure, value):
    variant = "twin" if k == 0 else "violation"
    labels = dict(arm="pressure_core_" + effort, model=model, effort=effort, template_id=template,
                  block=block, prevalence_k=k, visibility=visibility, pressure=pressure, variant=variant)
    def field(value, applicable=True):
        return {"value": value if applicable else None, "applicable": applicable, "reason": None}
    return {**labels, "level": "P", "status": "archived", "excluded_from_analysis": False,
            "assignment_id": "/".join(str(labels[key]) for key in analysis.LABELS),
            "score": {**labels, "level": "P", "eligibility": {"eligible": True, "reason": None},
                      "task": {"outcome": True}, "assistant_phase_counts": {"outputs": 1, "with_phase": 1},
                      "endpoints": {"focal_hit": field(value, k != 0), "owner_informed": field(value, k != 0),
                                    "false_alarm": field(value if k == 0 else False),
                                    "authorization_engaged": field(value), "any_hit": field(value or k == 12, k != 0),
                                    "concern_not_restated_at_closure": field(False, value and k != 0)}}}


def fake_export(tmp_path, rows=None):
    directory = tmp_path / "export"
    directory.mkdir()
    atomic_json(directory / "index.json", seal({"rows": fake_rows() if rows is None else rows}))
    return directory


@pytest.mark.parametrize("table", [(91, 7, 1, 9), (2, 0, 6, 2), (5, 4, 3, 8),
                                  (0, 0, 0, 20), (20, 0, 0, 0), (0, 20, 0, 0), (0, 0, 0, 0)])
def test_statistics_match_level_s(table):
    old = level_s_statistics()
    a, b, c, d = table
    assert analysis.mcnemar_exact(b, c) == pytest.approx(old.mcnemar_exact(b, c), abs=1e-14)
    new = analysis.newcombe_paired(*table)
    assert (tuple(round(value, 4) for value in new) if new else None) == old.newcombe_paired(*table)
    cp = analysis.clopper_pearson(a + b, sum(table))
    assert (tuple(round(value, 4) for value in cp) if cp else None) == old.clopper_pearson(a + b, sum(table))


def test_known_fake_export_pair_tables(tmp_path):
    export = fake_export(tmp_path)
    index = analysis.load_export(export, skip=True)
    rows, models = analysis.core_population(index)
    h1 = analysis.analyze_contrast(rows, models, "pressure", "P3", "neutral", fixed={"visibility": "visible"})
    pooled = h1["pooled"]
    assert pooled["table"] == dict(a=91, b=7, c=1, d=21)
    assert pooled["complete_pairs"] == 120
    assert pooled["mcnemar_p"] == 0.0703125
    assert tuple(round(value, 4) for value in pooled["newcombe_95"]) == level_s_statistics().newcombe_paired(91, 7, 1, 21)
    assert pooled["equal_weighted_lane_K_difference"] == pytest.approx(0.05)
    assert [group["complete_pairs"] for group in h1["models"]] == [40] * 3
    assert [group["complete_pairs"] for group in h1["lanes"]] == [20] * 6
    assert pooled["world_sign_flip"]["assignments"] == 1024
    twin = analysis.analyze_contrast(rows, models, "pressure", "P3", "neutral", variant="twin",
                                     endpoint="false_alarm", fixed={"visibility": "visible"})
    assert twin["pooled"]["table"] == dict(a=0, b=60, c=0, d=0)


def test_world_sign_flip_hand_checked():
    # +/-3 +/-2 +/-1 has totals +/-6, +/-4, +/-2 and zero twice.
    result = analysis.world_sign_flip([3, 2, -1, 0])
    assert result["absolute_total"] == 4
    assert result["extreme_assignments"] == 8
    assert result["assignments"] == 16
    assert result["p"] == 0.5
    assert analysis.world_sign_flip([1] * 10)["p"] == 2 / 1024
    assert analysis.world_sign_flip([0] * 10)["p"] == 1.0


def test_firth_separation_and_profile_interval():
    x, y = [[1, 0]] * 10 + [[1, 1]] * 10, [0] * 10 + [1] * 10
    # The ordinary likelihood keeps improving, with no finite maximum.
    def ordinary_ll(t):
        return -20 * math.log1p(math.exp(-t))
    assert ordinary_ll(20) > ordinary_ll(10) > ordinary_ll(5)
    fit = analysis.firth_fit(x, y)
    assert fit["converged"]
    assert fit["modified_score_max"] < 1e-8
    # Exact penalized fitted probabilities are 0.5/11 and 10.5/11.
    assert fit["coefficients"] == pytest.approx([-math.log(21), 2 * math.log(21)], abs=1e-7)
    lower, upper = analysis.firth_profile_interval(x, y, fit)
    assert 0 < lower < fit["coefficients"][1] < upper < 20
    for value in (lower, upper):
        profile = analysis.firth_fit(x, y, initial=fit["coefficients"], fixed={1: value})
        assert profile["modified_score_max"] < 1e-8
        assert 2 * (fit["penalized_log_likelihood"] - profile["penalized_log_likelihood"]) == pytest.approx(
            3.841458820694124, abs=1e-7)


def test_firth_with_lane_and_world_fixed_effects():
    x, y = [], []
    for lane, world, condition in itertools.product(range(2), range(3), (0, 1)):
        x.append([1, condition, lane, int(world == 1), int(world == 2)])
        y.append(condition)
    fit = analysis.firth_fit(x, y)
    assert fit["converged"] and fit["coefficients"][1] > 0
    assert all(math.isfinite(value) for value in fit["coefficients"])


def test_firth_modified_score_is_penalized_likelihood_gradient():
    x, y = [], []
    for lane, world, condition in itertools.product(range(2), range(3), (0, 1)):
        x.append([1, condition, lane, int(world == 1), int(world == 2)])
        y.append(int((lane + world + condition) % 3 == 0))
    beta = [0.2, -0.4, 0.3, -0.1, 0.5]
    _, score, _ = analysis._firth_state(x, y, beta)
    for j in range(len(beta)):
        plus, minus = list(beta), list(beta)
        plus[j] += 1e-5
        minus[j] -= 1e-5
        derivative = (analysis._firth_state(x, y, plus)[0] - analysis._firth_state(x, y, minus)[0]) / 2e-5
        assert score[j] == pytest.approx(derivative, abs=1e-8)


def test_missing_bounds_every_binary_resolution():
    values = [(True, False), (None, True), (False, None), (None, None)]
    positions = [(i, j) for i, pair in enumerate(values) for j, value in enumerate(pair) if value is None]
    differences = []
    for resolution in itertools.product((False, True), repeat=len(positions)):
        filled = [list(pair) for pair in values]
        for (i, j), value in zip(positions, resolution):
            filled[i][j] = value
        differences.append(sum(int(left) - int(right) for left, right in filled) / len(filled))
    assert analysis.missing_bounds(values) == (min(differences), max(differences)) == (-0.5, 0.5)


def test_complete_cases_missing_reasons_bounds_and_equal_weights():
    rows = fake_rows()
    # One non-null side survives in the bounds; the ambiguous side is not a miss.
    missing = next(row for row in rows if row["variant"] == "violation" and row["visibility"] == "visible"
                   and row["pressure"] == "P3" and row["prevalence_k"] == 12)
    missing["score"]["endpoints"]["focal_hit"].update(value=None, reason="ambiguous_coverage")
    pairs = analysis.make_pairs(rows, "pressure", "P3", "neutral", fixed={"visibility": "visible"})
    result = analysis.summarize_pairs(pairs)
    assert result["complete_pairs"] == 119
    assert result["missing_pairs"] == 1
    assert result["missingness"] == [{"side": "left", "reason": "ambiguous_coverage", "model": missing["model"],
                                      "effort": missing["effort"], "prevalence_k": 12, "pressure": "P3",
                                      "visibility": "visible", "count": 1}]
    assert result["missing_bounds"] == pytest.approx((5 / 120, 6 / 120))
    assert result["difference"] != result["equal_weighted_lane_K_difference"]
    missing["excluded_from_analysis"] = True
    result = analysis.summarize_pairs(pairs)
    assert result["excluded_pairs"] == 1 and result["missing_pairs"] == 0
    assert result["missing_bounds"][0] == result["difference"]


def test_absent_cells_and_lanes_are_distinct():
    rows = fake_rows()
    absent = next(row for row in rows if row["variant"] == "violation" and row["pressure"] == "P3"
                  and row["visibility"] == "visible")
    rows.remove(absent)
    pairs = analysis.make_pairs(rows, "pressure", "P3", "neutral", fixed={"visibility": "visible"})
    result = analysis.summarize_pairs(pairs)
    assert result["analysis_pairs"] == 120 and result["missing_pairs"] == 1
    assert result["missingness"][0]["reason"] == "unrun"
    model = analysis.load_protocol()["models"][0]
    rows = [row for row in rows if not (row["model"] == model and row["effort"] == "low")]
    result = analysis.analyze_contrast(rows, analysis.load_protocol()["models"], "pressure", "P3", "neutral",
                                       fixed={"visibility": "visible"})
    assert result["pooled"]["analysis_pairs"] == 100
    assert result["lanes"][0]["analysis_pairs"] == 0


def test_empty_lane_K_stratum_keeps_bounds_but_no_point_estimand():
    rows = fake_rows()
    model = analysis.load_protocol()["models"][0]
    for row in rows:
        if (row["model"], row["effort"], row["prevalence_k"], row["visibility"], row["pressure"]) == (
                model, "low", 1, "visible", "P3"):
            row["score"]["endpoints"]["focal_hit"].update(value=None, reason="ambiguous_coverage")
    pairs = analysis.make_pairs(rows, "pressure", "P3", "neutral", fixed={"visibility": "visible"})
    result = analysis.summarize_pairs(pairs)
    assert result["missing_pairs"] == 10
    assert result["equal_weighted_lane_K_difference"] is None
    assert result["equal_weighted_missing_bounds"] == pytest.approx(result["missing_bounds"])


def test_holm_step_down_does_not_resume_after_failure():
    result = analysis.holm({"a": 0.03, "b": 0.04, "c": 0.045})
    assert not any(item["reject"] for item in result.values())
    assert [result[key]["adjusted_p"] for key in ("a", "b", "c")] == pytest.approx([0.09] * 3)


def test_tost_uses_90_percent_interval_and_margins():
    equal = analysis.tost_equivalence(dict(a=50, b=0, c=0, d=50))
    assert equal["equivalent_at_0_05"] and equal["tost_p"] < 0.05
    assert equal["newcombe_90"] == analysis.newcombe_paired(50, 0, 0, 50, alpha=0.10)
    unequal = analysis.tost_equivalence(dict(a=30, b=30, c=0, d=40))
    assert not unequal["equivalent_at_0_05"] and unequal["tost_p"] > 0.05


def test_summary_marginals_misses_and_conditional_domain():
    rows = fake_rows()
    groups = analysis.summarize_rates(rows)
    assert len(groups) == 33
    assert len(groups[-1]["cells"]) == 60
    all_trials = sum(cell["trials"] for cell in groups[0]["cells"])
    assert all_trials == 600
    tables = analysis.miss_tables(rows)
    assert sum(cell["not_engaged"] for cell in tables["misses_by_authorization_engaged"]) == 50
    assert all(cell["no_claim"] == 0 for cell in tables["K12_misses_by_any_hit"])
    assert all(cell["share_with_phase"] == 1 for cell in tables["assistant_phase_coverage"])
    closure = analysis.analyze_contrast(rows, analysis.load_protocol()["models"], "visibility", "visible", "private",
                                        endpoint="concern_not_restated_at_closure", fixed={"pressure": "P3"})
    assert closure["pooled"]["not_applicable_pairs"] == 22
    assert closure["pooled"]["missing_pairs"] == 0


def test_interaction_is_difference_of_differences():
    rows = fake_rows()
    result = analysis.interaction(rows)
    assert result["complete_quartets"] == 120
    assert result["difference_in_differences"] == pytest.approx(0.05)
    assert result["missing_bounds"] == pytest.approx((0.05, 0.05))
    assert result["world_sign_flip"]["assignments"] == 1024


def test_cli_requires_evidence_and_preserves_no_output_on_refusal(tmp_path):
    export = fake_export(tmp_path)
    output = tmp_path / "report.txt"
    with pytest.raises(SystemExit) as error:
        analysis.main([str(export), "--output", str(output)])
    assert error.value.code == 2 and not output.exists()


def test_verifier_failure_is_not_converted_into_missing_data(tmp_path, monkeypatch):
    export = fake_export(tmp_path)
    receipts, repairs = tmp_path / "receipts", tmp_path / "repairs"
    output = tmp_path / "report.txt"

    def refuse(directory, index, *, receipt_directory, repair_evidence_directory):
        assert directory == export and receipt_directory == receipts and repair_evidence_directory == repairs
        assert len(index["rows"]) == 600
        raise ValueError("altered primary evidence")

    monkeypatch.setattr(analysis.review, "verify_pressure_export_evidence", refuse)
    with pytest.raises(SystemExit) as error:
        analysis.main([str(export), "--receipt-directory", str(receipts), "--repair-evidence-directory", str(repairs),
                       "--output", str(output)])
    assert error.value.code == 2
    assert not output.exists() and not output.with_suffix(".json").exists()


def test_fake_cli_deterministic_output_and_pilot_separation(tmp_path, monkeypatch, capsys):
    export = fake_export(tmp_path)
    # Numerical Firth/profile fits are covered separately and in transport integration.
    # Avoid repeating those fits to test only byte/order determinism here.
    globals_ = analysis.build_report.__globals__
    monkeypatch.setitem(globals_, "firth_sensitivity", lambda pairs: {"status": "test_stub", "pairs": len(pairs)})
    pilot_rows = deepcopy(fake_rows()[:10])
    for row in pilot_rows:
        row["arm"] = "pressure_pilot_" + row["effort"]
        row["block"] = 0
        row["score"].update(arm=row["arm"], block=0)
    p4 = deepcopy(next(row for row in pilot_rows if row["visibility"] == "visible" and row["pressure"] == "P3"
                       and row["prevalence_k"] == 1))
    p4["assignment_id"] += "/P4"
    p4["pressure"] = p4["score"]["pressure"] = "P4"
    pilot_rows.append(p4)
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    atomic_json(pilot / "index.json", seal({"rows": pilot_rows}))
    output = tmp_path / "report.txt"
    args = [str(export), "--skip-verification", "--output", str(output), "--pilot-export-directory", str(pilot)]
    assert analysis.main(args) == 0
    data, text = output.with_suffix(".json").read_bytes(), output.read_bytes()
    assert analysis.main(args) == 0
    assert data == output.with_suffix(".json").read_bytes() and text == output.read_bytes()
    assert b"\r" not in data + text
    assert analysis.WARNING.encode() in text and analysis.WARNING in capsys.readouterr().out
    report = json.loads(data)
    assert report["pilot"]["rows"] == 11 and report["primary"]["H1"]["pooled"]["complete_pairs"] == 120
    assert all("pressure" in group["by"] for group in report["pilot"]["summaries"])
    assert {cell["pressure"] for cell in report["pilot"]["summaries"][0]["cells"]} == {"neutral", "P3", "P4"}
    assert len(report["secondary"]) == 21
    assert set(report["primary"]["H1"]["pooled"]["missing_pair_sides"]) == {"left", "right", "both"}
    assert all("arm" in cell for cell in report["P_A3"]["misses_by_authorization_engaged"])
    assert all(len(report["primary"][name]["models"]) == 3 for name in ("H1", "H2"))
    index = read_sealed(export / "index.json")
    index["rows"].reverse()
    reordered = analysis.build_report(index, pilot_index=read_sealed(pilot / "index.json"), skip_verification=True)
    assert analysis.render_report(reordered).encode() == text
    assert (json.dumps(reordered, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode() == data


@pytest.mark.parametrize("mutation", ["duplicate", "P4", "pilot"])
def test_core_refuses_non_preregistered_or_duplicate_cells(mutation):
    index = seal({"rows": fake_rows()})
    if mutation == "duplicate":
        index["rows"].append(deepcopy(index["rows"][0]))
        with pytest.raises(ValueError, match="duplicate"):
            analysis.make_pairs(index["rows"], "pressure", "P3", "neutral")
    else:
        index["rows"][0]["pressure" if mutation == "P4" else "arm"] = (
            "P4" if mutation == "P4" else "pressure_pilot_low")
        with pytest.raises(ValueError, match="core"):
            analysis.core_population(index)


def test_receipt_verified_fake_transport_cli_and_resealed_row_refusal(successor_inputs, tmp_path):  # noqa: F811
    _, _, _, receipts, _, exports, _ = successor_inputs
    export = exports["core", 2]
    output = tmp_path / "transport-report.txt"
    assert analysis.main([str(export), "--receipt-directory", str(receipts), "--output", str(output)]) == 0
    report = json.loads(output.with_suffix(".json").read_bytes())
    assert report["verification"]["verified"]
    assert report["primary"]["H1"]["pooled"]["planned_pairs"] == 120
    assert report["primary"]["H2"]["pooled"]["planned_pairs"] == 120
    assert all(report["primary"][name]["firth_sensitivity"]["status"] == "converged" for name in ("H1", "H2"))
    assert analysis.WARNING.encode() not in output.read_bytes()
    path = export / "index.json"
    original = path.read_bytes()
    try:
        index = read_sealed(path)
        row = next(row for row in index["rows"] if row.get("score"))
        row["score"]["task"]["outcome"] = not row["score"]["task"]["outcome"]
        atomic_json(path, seal({key: value for key, value in index.items() if key != "seal_hash"}))
        refused = tmp_path / "refused.txt"
        with pytest.raises(SystemExit) as error:
            analysis.main([str(export), "--receipt-directory", str(receipts), "--output", str(refused)])
        assert error.value.code == 2
        assert not refused.exists() and not refused.with_suffix(".json").exists()
    finally:
        path.write_bytes(original)
