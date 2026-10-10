"""Every exported row is checked before sampling or pilot decisions can filter it."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.pilot_decision import (
    build_pilot_decision,
    export_reference,
    validate_core_decision,
)
from swarm_auth_bench.peer_reporting_v11.review import write_review_packets
from swarm_auth_bench.peer_reporting_v11.review_plan import pressure_review_selection
from swarm_auth_bench.peer_reporting_v11.review_sampling import analysis_score_rows

from .review_root_helpers import register_review_root
from .test_pilot_decision import core_build
from .test_pilot_decision import decision_inputs as decision_inputs
from .test_review_population import core_review_inputs as core_review_inputs
from .test_review_population import export_root, export_rows


def reseal_index(path, index):
    index["pressure_review_selection"] = pressure_review_selection([
        row["score"] for row in analysis_score_rows(index["rows"])])
    exclusions = sorted(row["assignment_id"] for row in index["rows"] if row["excluded_from_analysis"])
    index.update(analysis_exclusions=exclusions, analysis_exclusion_count=len(exclusions))
    atomic_json(path, seal({key: value for key, value in index.items() if key != "seal_hash"}))
    return index["pressure_review_selection"]


@pytest.mark.parametrize("field", ["score", "excluded_from_analysis"])
def test_four_complete_exports_cannot_fabricate_four_singleton_packets(tmp_path, monkeypatch, core_review_inputs, field):
    study, manifest, assignments, attempts = core_review_inputs
    root, plan, _ = register_review_root(study, manifest, assignments, "combined")
    original = tmp_path / "original"
    export_root(monkeypatch, root, plan, study, original, export_rows(assignments, attempts))
    selection = read_sealed(original / "index.json")["pressure_review_selection"]
    assert write_review_packets(original, selection, tmp_path / "valid-packets", study_directory=study)["packets"] == 1
    for kept in range(4):
        forged = tmp_path / f"forged-{kept}"
        shutil.copytree(original, forged)
        path = forged / "index.json"
        index = read_sealed(path)
        assert len(index["rows"]) == 4 and all("attempt_path" in row for row in index["rows"])
        for number, row in enumerate(index["rows"]):
            if number != kept:
                row[field] = None if field == "score" else True
        selection = reseal_index(path, index)
        assert len(selection["rows"]) == 1
        output = tmp_path / f"refused-packets-{kept}"
        with pytest.raises(ValueError, match="exported row differs from registered live root"):
            write_review_packets(forged, selection, output, study_directory=study)
        assert not output.exists()
        assert all((forged / row["attempt_path"]).read_bytes() == (original / row["attempt_path"]).read_bytes()
                   for row in index["rows"])


@pytest.mark.parametrize("field", ["score", "excluded_from_analysis"])
def test_pilot_cannot_suppress_two_unchanged_flagged_attempts(tmp_path, decision_inputs, wp6_inputs, field):
    def flags(data, rows):
        for row in [row for row in rows if row["pressure"] == "P3" and row["visibility"] == "visible"
                    and row["prevalence_k"] == 1 and row["variant"] == "violation"][:4]:
            data[row["assignment_id"]]["observer_outputs"].append({"output_id": "script-comment",
                "source_kind": "final_response", "text": "These peers are scripted."})
    inputs = decision_inputs[-1](flags)
    record = build_pilot_decision(**inputs, ceiling_choice="a")
    assert record["Psel"] == "P2" and record["rungs"]["P3"]["test_aware_count"] == 4
    path = Path(inputs["pilot_exports"][0]["path"])
    index = read_sealed(path)
    before = {row["attempt_path"]: (path.parent / row["attempt_path"]).read_bytes() for row in index["rows"]}
    targets = [row for row in index["rows"] if row["score"]["pressure"] == "P3"
               and row["score"]["endpoints"]["test_awareness_flag"]["value"] is True][:2]
    assert len(targets) == 2
    for row in targets:
        row[field] = None if field == "score" else True
    reseal_index(path, index)
    reference = export_reference(path)
    with pytest.raises(ValueError, match="exported row differs from registered live root"):
        build_pilot_decision(gate_exports=[reference], pilot_exports=[reference], ceiling_choice="a")
    forged = deepcopy(record)
    forged.update(Psel="P3", ceiling=False, ceiling_choice=None, gate_exports=[reference], pilot_exports=[reference])
    forged = seal({key: value for key, value in forged.items() if key != "seal_hash"})
    with pytest.raises(ValueError, match="exported row differs from registered live root"):
        validate_core_decision(forged, study_manifest_hash=decision_inputs[0]["seal_hash"])
    with pytest.raises(ValueError, match="exported row differs from registered live root"):
        core_build(decision_inputs, wp6_inputs, forged)
    assert {name: (path.parent / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("mutation", ["score", "eligibility", "attempt_hash", "attempt", "status", "selection", "json_type"])
@pytest.mark.parametrize("consumer", ["packets", "pilot"])
def test_consumers_refuse_resealed_derived_evidence(tmp_path, monkeypatch, core_review_inputs, mutation, consumer):
    study, manifest, assignments, attempts = core_review_inputs
    root, plan, _ = register_review_root(study, manifest, assignments, "combined")
    output = tmp_path / "export"
    export_root(monkeypatch, root, plan, study, output, export_rows(assignments, attempts))
    path = output / "index.json"
    index = read_sealed(path)
    row = index["rows"][0]
    if mutation == "score":
        row["score"]["endpoints"]["focal_hit"]["value"] = True
    elif mutation == "eligibility":
        row["score"]["eligibility"]["eligible"] = False
    elif mutation == "attempt_hash":
        row["attempt_hash"] = "0" * 64
    elif mutation == "attempt":
        target = output / row["attempt_path"]
        raw = read_sealed(target)
        raw["observer_outputs"][0]["text"] = "A fabricated outcome."
        atomic_json(target, seal({key: value for key, value in raw.items() if key != "seal_hash"}))
    elif mutation == "status":
        row["status"] = "unrun"
    elif mutation == "json_type":
        row["excluded_from_analysis"] = 0
    selection = reseal_index(path, index)
    if mutation == "selection":
        selection["rows"][0]["reasons"] = ["invented"]
        index["pressure_review_selection"] = seal({key: value for key, value in selection.items() if key != "seal_hash"})
        atomic_json(path, seal({key: value for key, value in index.items() if key != "seal_hash"}))
        selection = index["pressure_review_selection"]
    with pytest.raises(ValueError, match="live root|pressure_review_selection"):
        if consumer == "packets":
            write_review_packets(output, selection, tmp_path / "packets", study_directory=study)
        else:
            reference = export_reference(path)
            build_pilot_decision(gate_exports=[reference], pilot_exports=[reference])
    assert not (tmp_path / "packets").exists()


def test_all_unrun_pressure_rows_keep_the_registered_source_and_empty_cli_selection(
        tmp_path, wp6_study, wp6_inputs, capsys):
    from swarm_auth_bench.peer_reporting_v11 import live
    from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
    from swarm_auth_bench.peer_reporting_v11.review import verify_pressure_export_evidence
    from swarm_auth_bench.peer_reporting_v11.score import score_trial

    study = tmp_path / "study"
    shutil.copytree(wp6_study[0], study)
    manifest = read_sealed(study / live.STUDY_MANIFEST)
    assignments = [row for row in manifest["assignments"]
                   if row["arm"] in ("pressure_gate_xhigh", "pressure_gate_low")]
    fixtures = {identifier: {key: value for key, value in read_sealed(
        study / manifest["fixtures"][identifier]["path"]).items() if key != "seal_hash"}
        for identifier in {row["fixture_id"] for row in assignments}}
    built = live.build_assignment_plan("calibration", assignments, fixtures, wp6_inputs["caps_record"],
        revision="all-unrun-pressure", source={"kind": "study_manifest", "study_manifest_hash": manifest["seal_hash"]},
        gate_evidence={}, study_manifest=manifest)
    root = study / "roots" / "unrun"
    live.prepare_live_root(root, built, study_directory=study)
    from .receipt_helpers import commit_receipt
    receipts = commit_receipt(root, tmp_path / "receipts", study_directory=study)
    export = tmp_path / "export"
    export_live_review(root, export, study_directory=study, scorer=score_trial, receipt_directory=receipts,
                       pressure_review_selector=pressure_review_selection)
    index = read_sealed(export / "index.json")
    assert len(index["rows"]) == 24
    assert all(row["status"] == "unrun" and row["score"] is None for row in index["rows"])
    assert index["pressure_review_selection"] is None
    assert index["pressure_review_study_directory"] == str(study.resolve())
    # A historical H2 plan omits compound_type; it still shares the registry.
    earlier = next(row for row in manifest["assignments"]
                   if row["split"] == "calibration" and row.get("compound_type") is not None)
    stored = read_sealed(study / manifest["fixtures"][earlier["fixture_id"]]["path"])
    earlier_fixture = {key: value for key, value in stored.items() if key != "seal_hash"}
    source = built[0]["source"]
    ledger = live.prior_root_ledger([root], phase="calibration", source=source, study_directory=study, receipt_directory=receipts)
    earlier_plan = live.build_assignment_plan("calibration", [earlier], {earlier["fixture_id"]: earlier_fixture},
        wp6_inputs["caps_record"], revision="earlier-archive", source=source, gate_evidence={},
        study_manifest=manifest, consumed_attempts=ledger)
    assert "compound_type" not in next(iter(earlier_plan[1].values()))["planned_order"][0]
    live.prepare_live_root(study / "roots" / "earlier", earlier_plan, study_directory=study, prior_roots=[root], receipt_directory=receipts)
    assert verify_pressure_export_evidence(export, index, receipt_directory=receipts) == []
    assert write_review_packets(export, pressure_review_selection([]), tmp_path / "packets", study_directory=study, receipt_directory=receipts)["packets"] == 0

    cli_packets = tmp_path / "cli-packets"
    arguments = ["review-packets", str(export), "--study", str(study),
                 "--receipt-directory", str(receipts), "--output", str(cli_packets)]
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["packets"] == 0
    assert list((cli_packets / "reviewer").iterdir()) == []
    assert list((cli_packets / "controller").iterdir()) == []
    assert write_review_packets(export, None, tmp_path / "default-packets", study_directory=study,
                                receipt_directory=receipts)["packets"] == 0
    (receipts / (live.read_live_plan(root)["seal_hash"] + ".json")).unlink()
    refused = tmp_path / "missing-receipt-packets"
    arguments[-1] = str(refused)
    assert main(arguments) == 2
    assert "receipt mismatch" in json.loads(capsys.readouterr().out)["error"]
    assert not refused.exists()
