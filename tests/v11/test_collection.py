"""W07: real allocation, identity, spread, sealing, corruption detection, and WP1 compatibility."""

import json
import shutil
from collections import Counter, defaultdict
from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import collection
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST, build_study, verify_study
from swarm_auth_bench.peer_reporting_v11.live import load_study, validate_assignment_rows

from .test_config import COUNTS


def test_real_counts_and_all_fixtures_verify(wp6_study, wp6_inputs, monkeypatch):
    directory, manifest, result = wp6_study
    assert result["counts"] == COUNTS
    assert result["total_trials"] == len(manifest["assignments"]) == 1224
    assert Counter(row["arm"] for row in manifest["assignments"]) == COUNTS
    assert result["split_counts"] == {"collection": 1128, "calibration": 84, "smoke": 12}
    assert result["fixtures"] == len(manifest["fixtures"]) == 135
    seen = set()
    original = collection.verify_fixture

    def verify(fixture, template):
        seen.add(fixture["fixture_id"])
        assert template == wp6_inputs["templates"][fixture["parameters"]["template_id"]]
        return original(fixture, template)

    monkeypatch.setattr(collection, "verify_fixture", verify)
    verified = verify_study(directory, **wp6_inputs)
    assert verified["valid"], verified["errors"]
    assert seen == set(manifest["fixtures"])
    assert verified["counts"] == COUNTS


def test_same_inputs_same_manifest_and_fixture_bytes(tmp_path, wp6_study, wp6_inputs):
    directory, manifest, result = wp6_study
    other = tmp_path / "other"
    rebuilt = build_study(other, **wp6_inputs)
    assert result["seal_hash"] == rebuilt["seal_hash"] == manifest["seal_hash"]
    assert (directory / STUDY_MANIFEST).read_bytes() == (other / STUDY_MANIFEST).read_bytes()
    for reference in manifest["fixtures"].values():
        assert (directory / reference["path"]).read_bytes() == (other / reference["path"]).read_bytes()


def test_identities_bind_all_inputs_and_stay_bounded(wp6_study):
    _, manifest, _ = wp6_study
    rows = manifest["assignments"]
    assert len({row["assignment_id"] for row in rows}) == 1224
    assert all(len(row["assignment_id"]) <= 90 for row in rows)
    bindings = {"protocol_id": manifest["protocol_id"], "caps_hash": manifest["caps_hash"],
                "tool_manifest_hash": manifest["tool_manifest_hash"]}
    assert all(collection.assignment_identity(row, **bindings) == row["assignment_id"] for row in rows)
    row = rows[0]
    for key in ("arm", "split", "fixture_id", "model", "effort", "prompt_condition", "world_mode", "instructions"):
        assert collection.assignment_identity({**row, key: row[key] + "changed"}, **bindings) != row["assignment_id"]
    for key in bindings:
        assert collection.assignment_identity(row, **{**bindings, key: bindings[key] + "changed"}) != row[
            "assignment_id"]


def test_small_groups_are_spread_over_split_rounds(wp6_study):
    _, manifest, _ = wp6_study
    for split in ("collection", "calibration", "smoke"):
        rows = [row for row in manifest["assignments"] if row["split"] == split]
        assert [row["planned_order"] for row in rows] == list(range(len(rows)))
        groups = defaultdict(list)
        for row in rows:
            groups[row["fixture_id"], row["world_mode"], row["effort"]].append(row)
        rounds = max(map(len, groups.values()))
        # Reconstruct each cell's round from its seeded position, then check the global order.
        row_rounds = {}
        for group, cells in groups.items():
            ordered = sorted(cells, key=lambda row: content_hash([
                collection.ORDER_VERSION, manifest["protocol"]["seeds"][split], split, "cells", group,
                (row["model"], row["prompt_condition"])]))
            allocated = [index * rounds // len(cells) for index in range(len(cells))]
            if split == "collection" and len(cells) == 3:
                assert allocated == [0, 3, 6]
            for row, round_index in zip(ordered, allocated, strict=True):
                row_rounds[row["assignment_id"]] = round_index
        actual = [row_rounds[row["assignment_id"]] for row in rows]
        assert actual == sorted(actual)
        assert set(actual) == set(range(rounds))


@pytest.mark.parametrize("split", ["collection", "calibration", "smoke"])
def test_wp1_live_reader_accepts_real_manifest(split, wp6_study, wp6_inputs):
    directory, manifest, _ = wp6_study
    rows, fixtures, source = load_study(directory, split)
    entries = validate_assignment_rows(split, rows, fixtures, wp6_inputs["caps_record"], load_bundle())
    assert len(entries) == manifest["split_counts"][split]
    assert source["study_manifest_hash"] == manifest["seal_hash"]
    assert all(entry["instructions"] == row["instructions"] for entry, row in zip(entries, rows, strict=True))


@pytest.mark.parametrize("tamper", ["row", "fixture", "count", "missing_row", "seal", "malformed"])
def test_verify_returns_content_errors(tmp_path, wp6_study, wp6_inputs, tamper):
    source, _, _ = wp6_study
    directory = tmp_path / "tampered"
    shutil.copytree(source, directory)
    path = directory / STUDY_MANIFEST
    manifest = read_sealed(path)
    manifest.pop("seal_hash")
    if tamper == "row":
        manifest["assignments"][0]["instructions"] += " tampered"
    elif tamper == "fixture":
        fixture_id, reference = next(iter(manifest["fixtures"].items()))
        fixture_path = directory / reference["path"]
        fixture = read_sealed(fixture_path)
        fixture.pop("seal_hash")
        fixture["truth"]["expected_task"]["total_size_kib"] += 1
        atomic_json(fixture_path, seal(fixture))
        manifest["fixtures"][fixture_id]["content_hash"] = content_hash(fixture)
    elif tamper == "count":
        manifest["counts"]["collection"] -= 1
    elif tamper == "missing_row":
        manifest["assignments"].pop()
    elif tamper == "malformed":
        manifest["assignments"] = [None]
    atomic_json(path, seal(manifest))
    if tamper == "seal":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["seal_hash"] = "0" * 64
        atomic_json(path, value)
    verified = verify_study(directory, **wp6_inputs)
    assert verified["valid"] is False
    assert verified["errors"]
    if tamper in ("count", "missing_row"):
        assert any("count" in error for error in verified["errors"])


def test_build_refuses_unfrozen_caps_without_writing(tmp_path, wp6_inputs):
    inputs = deepcopy(wp6_inputs)
    inputs["caps_record"]["caps_status"] = "candidate"
    with pytest.raises(ValueError, match="frozen"):
        build_study(tmp_path / "study", **inputs)
    assert not (tmp_path / "study").exists()


def test_verify_missing_manifest_returns_errors(tmp_path, wp6_inputs):
    assert verify_study(tmp_path / "missing", **wp6_inputs)["valid"] is False
