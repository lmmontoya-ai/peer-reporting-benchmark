"""W07: real allocation, identity, spread, sealing, corruption detection, and WP1 compatibility."""

import json
import shutil
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import groupby

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import collection
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST, build_study, verify_study
from swarm_auth_bench.peer_reporting_v11.config import (
    CALIBRATION_EXTENSION_ARMS,
    HARD_PROBE_ARMS,
    SOCIAL_ARMS,
    SPLITS,
    arm_fixture_cells,
    social_fixture_cells,
)
from swarm_auth_bench.peer_reporting_v11.ladder import calibration_near_miss_types
from swarm_auth_bench.peer_reporting_v11.live import load_study, validate_assignment_rows

from .test_config import COUNTS

PAIRED_ARMS = ("collection", "calibration", "low_effort", *CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS)


def test_real_counts_and_all_fixtures_verify(wp6_study, wp6_inputs, monkeypatch):
    directory, manifest, result = wp6_study
    assert result["counts"] == COUNTS
    assert result["total_trials"] == len(manifest["assignments"]) == 1962
    assert Counter(row["arm"] for row in manifest["assignments"]) == COUNTS
    assert result["split_counts"] == {"collection": 1128, "calibration": 822, "smoke": 12}
    assert result["fixtures"] == len(manifest["fixtures"]) == 245
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


def test_a_rebuild_is_a_new_study_instance_with_the_same_content(tmp_path, wp6_study, wp6_inputs):
    # Spec 10 (W09 R2-M3): each build seals a fresh instance nonce, so a rebuild has another seal.
    directory, manifest, result = wp6_study
    other = tmp_path / "other"
    rebuilt = build_study(other, **wp6_inputs)
    assert result["seal_hash"] == manifest["seal_hash"] != rebuilt["seal_hash"]
    again = read_sealed(other / STUDY_MANIFEST)
    assert manifest["instance_nonce"] != again["instance_nonce"] == rebuilt["instance_nonce"]
    assert len(again["instance_nonce"]) == 32
    content = {key: value for key, value in manifest.items() if key not in {"instance_nonce", "seal_hash"}}
    assert content == {key: value for key, value in again.items() if key not in {"instance_nonce", "seal_hash"}}
    for reference in manifest["fixtures"].values():
        assert (directory / reference["path"]).read_bytes() == (other / reference["path"]).read_bytes()
    assert verify_study(other, **wp6_inputs)["valid"]
    tampered = tmp_path / "tampered"
    shutil.copytree(other, tampered)
    edited = read_sealed(tampered / STUDY_MANIFEST)
    edited.pop("seal_hash")
    edited["instance_nonce"] = manifest["instance_nonce"]  # resealed under the original study's nonce
    atomic_json(tampered / STUDY_MANIFEST, seal(edited))
    verified = verify_study(tampered, **wp6_inputs)
    assert verified["valid"] and verified["seal_hash"] == manifest["seal_hash"]  # a copy is the same instance
    edited.pop("instance_nonce")
    atomic_json(tampered / STUDY_MANIFEST, seal(edited))
    assert verify_study(tampered, **wp6_inputs)["valid"] is False


def test_custom_protocol_generator_parameters_build_and_verify_study(tmp_path, wp6_inputs):
    inputs = deepcopy(wp6_inputs)
    protocol = inputs["protocol"]
    protocol["generator_parameters"].update(
        distractor_grants=5, companion_slot_offset=4, pressure_slot_offsets=[2, 6])
    protocol["near_miss_types"] = ["wrong_actor", "expired_window", "wrong_digest"]
    directory = tmp_path / "custom"
    built = build_study(directory, **inputs)
    assert built["counts"] == COUNTS
    result = verify_study(directory, **inputs)
    assert result["valid"], result["errors"]


@pytest.mark.parametrize("split,expected_hash", [
    ("collection", "83a4277ac13a7cb8c9adeefdca4ea2e30b56f6c44591f20975eee97ca5fc83de"),
    ("calibration", "6cfb4280ccc30d34549cc2f1ec61f11cc912016465de1563b23fd7317cd9ff68"),
    ("smoke", "d885dac24cf251c7d424520ab2c604280d8eeb1d12de68358ca8fc4f65803fd7"),
])
def test_existing_assignments_keep_their_content_and_relative_order(split, expected_hash, wp6_study):
    # Captured from the original ordering implementation with the same revision 3 inputs,
    # omitting extension arms. Calibration's planned positions change when arms interleave.
    _, manifest, _ = wp6_study
    rows = [{key: value for key, value in row.items() if key != "planned_order"}
            for row in manifest["assignments"]
            if row["split"] == split and row["arm"] not in (*CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS, *SOCIAL_ARMS)]
    assert content_hash(rows) == expected_hash


def test_identities_bind_all_inputs_and_stay_bounded(wp6_study):
    _, manifest, _ = wp6_study
    rows = manifest["assignments"]
    assert len({row["assignment_id"] for row in rows}) == 1962
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


def _round_chunks(manifest, split, protocol):
    """Locate round/arm boundaries from fixture counts, independent of the order generator."""
    # Use the source protocol: canonical manifest JSON sorts object keys, including arms.
    rows = [row for row in manifest["assignments"] if row["split"] == split]
    assert [row["planned_order"] for row in rows] == list(range(len(rows)))
    arm_sizes = {arm: len({row["fixture_id"] for row in rows if row["arm"] == arm})
                 for arm, definition in protocol["arms"].items() if definition["split"] == split}
    position = 0
    for round_index in range(len(protocol["models"])) if split == "smoke" else range(9):
        for arm, definition in protocol["arms"].items():
            if definition["split"] != split:
                continue
            if arm == "smoke":
                size = len(definition["cells"]) * len(protocol["templates"][split])
            else:
                if len(definition["prompts"]) == 1 and round_index not in (0, 3, 6):
                    continue
                if len(definition["prompts"]) == 2 and round_index >= 6:
                    continue
                size = arm_sizes[arm]
            chunk = rows[position:position + size]
            assert len(chunk) == size
            assert all(row["arm"] == arm for row in chunk)
            assert all(row["round"] == round_index for row in chunk)
            yield round_index, arm, chunk
            position += size
    assert position == len(rows)


def _pair_key(row):
    return tuple(row[key] for key in ("arm", "template_id", "level", "near_miss_type", "model",
                                     "prompt_condition", "effort", "world_mode", "round")) + (row.get("compound_type"),)


def test_every_fixture_model_prompt_effort_world_cell_appears_once(wp6_study):
    directory, manifest, _ = wp6_study
    protocol = manifest["protocol"]
    calibration_types = calibration_near_miss_types(protocol["templates"]["calibration"],
                                                   protocol["seeds"]["calibration"], protocol=protocol)
    expected = Counter()
    for fixture_id, reference in manifest["fixtures"].items():
        parameters = read_sealed(directory / reference["path"])["parameters"]
        for arm, definition in protocol["arms"].items():
            if definition["split"] != parameters["split"]:
                continue
            if (arm in CALIBRATION_EXTENSION_ARMS and parameters["variant"] != "ambiguity"
                    and parameters["near_miss_type"] != calibration_types[parameters["template_id"]][0]):
                continue
            if arm in SOCIAL_ARMS and not any(all(parameters[key] == value for key, value in cell.items())
                                                 for cell in social_fixture_cells(arm)):
                continue
            if arm == "smoke":
                cells = [cell for cell in definition["cells"]
                         if (cell["level"], cell["variant"]) == (parameters["level"], parameters["variant"])]
            elif (parameters["level"], parameters["variant"]) in arm_fixture_cells(protocol, arm):
                cells = [{"prompt": prompt, "effort": definition["effort"], "world_mode": definition["world_mode"]}
                         for prompt in definition["prompts"]]
            else:
                continue
            for cell in cells:
                for model in protocol["models"]:
                    expected[arm, fixture_id, model, cell["prompt"], cell["effort"], cell["world_mode"]] += 1
    actual = Counter(tuple(row[key] for key in ("arm", "fixture_id", "model", "prompt_condition", "effort", "world_mode"))
                     for row in manifest["assignments"])
    assert actual == expected
    assert set(actual.values()) == {1}
    assert len(actual) == sum(COUNTS.values())
    assert Counter(cell[0] for cell in actual) == COUNTS


@pytest.mark.parametrize("arm", CALIBRATION_EXTENSION_ARMS)
def test_extension_cells_use_first_calibration_type_and_ambiguity_once(arm, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    protocol = wp6_inputs["protocol"]
    types = calibration_near_miss_types(protocol["templates"]["calibration"], protocol["seeds"]["calibration"],
                                       protocol=protocol)
    definition = protocol["arms"][arm]
    expected = Counter()
    for template_id, near_types in types.items():
        cells = [(level, variant, near_types[0]) for level in ("L2", "L3", "L4")
                 for variant in ("violation", "twin")] + [("L1", "ambiguity", None)]
        for level, variant, near in cells:
            for model in protocol["models"]:
                for prompt in definition["prompts"]:
                    expected[template_id, level, variant, near, model, prompt,
                             definition["effort"], "normal", "calibration"] += 1
    rows = [row for row in manifest["assignments"] if row["arm"] == arm]
    actual = Counter(tuple(row[key] for key in ("template_id", "level", "variant", "near_miss_type", "model",
                                               "prompt_condition", "effort", "world_mode", "split")) for row in rows)
    assert actual == expected
    assert set(actual.values()) == {1}
    assert len({row["fixture_id"] for row in rows}) == 14
    existing = {row["fixture_id"] for row in manifest["assignments"] if row["arm"] == "calibration"}
    assert {row["fixture_id"] for row in rows if row["variant"] != "ambiguity"} <= existing
    assert len({row["fixture_id"] for row in rows if row["variant"] == "ambiguity"} - existing) == 2


@pytest.mark.parametrize("split", SPLITS)
def test_pairs_are_adjacent_in_the_same_model_prompt_and_round(split, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    for _, arm, chunk in _round_chunks(manifest, split, wp6_inputs["protocol"]):
        if arm not in PAIRED_ARMS:
            continue
        for index, row in enumerate(chunk):
            if row["variant"] == "violation":
                twin = chunk[index + 1]
                assert twin["variant"] == "twin"
                assert _pair_key(twin) == _pair_key(row)
            elif row["variant"] == "twin":
                assert index > 0 and chunk[index - 1]["variant"] == "violation"
                assert _pair_key(chunk[index - 1]) == _pair_key(row)


@pytest.mark.parametrize("split", SPLITS)
def test_every_round_boundary_prefix_contains_complete_pairs(split, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    prefix = []
    for _, chunks in groupby(_round_chunks(manifest, split, wp6_inputs["protocol"]), key=lambda chunk: chunk[0]):
        for _, _, rows in chunks:
            prefix.extend(rows)
        violations, twins = Counter(), Counter()
        for row in prefix:
            if row["arm"] not in PAIRED_ARMS:
                continue
            if row["variant"] == "violation":
                violations[_pair_key(row)] += 1
            elif row["variant"] == "twin":
                twins[_pair_key(row)] += 1
        assert violations == twins


def _assert_protocol_round_sequence(manifest, protocol, split):
    for round_index, arm, chunk in _round_chunks(manifest, split, protocol):
        if arm == "smoke":
            expected = [(template_id, cell["level"], cell["variant"], protocol["models"][(index + round_index) % 3],
                         cell["prompt"], cell["effort"], cell["world_mode"])
                        for index, cell in enumerate(protocol["arms"][arm]["cells"])
                        for template_id in protocol["templates"][split]]
            assert [tuple(row[key] for key in ("template_id", "level", "variant", "model", "prompt_condition",
                                              "effort", "world_mode")) for row in chunk] == expected
            continue
        blocks = defaultdict(list)
        for row in chunk:
            if row["level"] == "S":
                key = (row["template_id"], row["difficulty"], row["block"], row["post_condition"])
            elif arm == "channel_failure" or row["variant"] not in ("violation", "twin"):
                key = (row["fixture_id"],)
            else:
                key = (row["template_id"], row["level"], row["near_miss_type"], row.get("compound_type"))
            blocks[key].append(row)
        ordered = [sorted(block, key=lambda row: (row["variant"] != "violation", row["fixture_id"]))
                   for block in blocks.values()]
        ordered.sort(key=lambda block: (-len(block), content_hash([collection.ORDER_VERSION, protocol["seeds"][split], arm,
                                                      [row["fixture_id"] for row in block]]),
                                       tuple(row["fixture_id"] for row in block)))
        assert [row["fixture_id"] for row in chunk] == [row["fixture_id"] for block in ordered for row in block]
        for index, block in enumerate(ordered):
            if len(protocol["arms"][arm]["prompts"]) == 2:
                offsets = ((0, 0), (1, 1), (2, 0), (0, 1), (1, 0), (2, 1))
            else:
                offsets = ((0, 0), (1, 1), (2, 2), (0, 1), (1, 2), (2, 0), (0, 2), (1, 0), (2, 1))
            a_offset, c_offset = offsets[index % len(offsets)]
            if len(protocol["arms"][arm]["prompts"]) == 3:
                model = protocol["models"][(round_index + a_offset) % 3]
                prompt = protocol["prompt_conditions"][(round_index // 3 + c_offset) % 3]
            elif len(protocol["arms"][arm]["prompts"]) == 2:
                assert 0 <= round_index < 6
                model = protocol["models"][(round_index + a_offset) % 3]
                prompt = protocol["arms"][arm]["prompts"][(round_index // 3 + c_offset) % 2]
            else:
                assert round_index in (0, 3, 6)
                model = protocol["models"][(round_index // 3 + a_offset) % 3]
                prompt = protocol["arms"][arm]["prompts"][0]
            assert all(row["model"] == model and row["prompt_condition"] == prompt for row in block)


@pytest.mark.parametrize("split", SPLITS)
def test_protocol_block_offsets_and_round_sequence(split, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    _assert_protocol_round_sequence(manifest, wp6_inputs["protocol"], split)


@pytest.mark.parametrize("split", SPLITS)
def test_round_order_is_independent_of_input_row_order(split, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    rows = [row for row in manifest["assignments"] if row["split"] == split]
    assert collection._interleave(list(reversed(rows)), wp6_inputs["protocol"], split) == rows


@pytest.mark.parametrize("split", SPLITS)
def test_per_arm_and_round_model_and_prompt_row_counts_differ_by_at_most_two(split, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    protocol = wp6_inputs["protocol"]
    for round_index, arm, chunk in _round_chunks(manifest, split, protocol):
        if arm in SOCIAL_ARMS:
            # Revision 5 post groups can be larger than the inherited <=2 row spread.
            continue
        model_counts = Counter(row["model"] for row in chunk)
        prompt_counts = Counter(row["prompt_condition"] for row in chunk)
        prompts = protocol["arms"][arm].get("prompts", protocol["prompt_conditions"])
        models = tuple(model_counts[model] for model in protocol["models"])
        conditions = tuple(prompt_counts[prompt] for prompt in prompts)
        assert max(models) - min(models) <= 2, (arm, round_index, models)
        assert max(conditions) - min(conditions) <= 2, (arm, round_index, conditions)


def test_smoke_models_rotate_across_protocol_cells(wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    protocol = wp6_inputs["protocol"]
    expected_counts = ((2, 1, 1), (1, 2, 1), (1, 1, 2))
    models_by_cell = defaultdict(list)
    for round_index, _, chunk in _round_chunks(manifest, "smoke", protocol):
        counts = Counter(row["model"] for row in chunk)
        assert tuple(counts[model] for model in protocol["models"]) == expected_counts[round_index]
        for index, row in enumerate(chunk):
            assert row["model"] == protocol["models"][(index + round_index) % 3]
            models_by_cell[index].append(row["model"])
    assert all(set(models) == set(protocol["models"]) for models in models_by_cell.values())


def test_verify_rejects_a_sealed_study_using_the_previous_offset_cycle(tmp_path, wp6_inputs, monkeypatch):
    directory = tmp_path / "previous-cycle"
    with monkeypatch.context() as previous:
        previous.setattr(collection, "ORDER_OFFSETS", tuple(divmod(index, 3) for index in range(9)))
        result = build_study(directory, **wp6_inputs)
    manifest = read_sealed(directory / STUDY_MANIFEST)
    assert manifest["seal_hash"] == result["seal_hash"]
    assert result["counts"] == COUNTS
    verified = verify_study(directory, **wp6_inputs)
    assert verified["valid"] is False
    assert "manifest assignments differs from recomputed study" in verified["errors"]


@pytest.mark.parametrize("change", ["models", "arms", "smoke_cells", "seed", "calibration_arms", "extension_prompts"])
def test_order_uses_protocol_list_arm_and_seed_order(change, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    protocol = deepcopy(wp6_inputs["protocol"])
    split = ("smoke" if change == "smoke_cells" else
             "calibration" if change in ("calibration_arms", "extension_prompts") else "collection")
    if change == "models":
        protocol["models"].reverse()
    elif change in ("arms", "calibration_arms"):
        protocol["arms"] = dict(reversed(list(protocol["arms"].items())))
    elif change == "smoke_cells":
        protocol["arms"]["smoke"]["cells"].reverse()
    elif change == "extension_prompts":
        protocol["arms"]["calibration_extension_xhigh"]["prompts"].reverse()
    else:
        protocol["seeds"][split] += 1
    original = [row for row in manifest["assignments"] if row["split"] == split]
    rows = collection._interleave(original, protocol, split)
    assert rows != original
    reordered = {**manifest, "assignments": rows}
    # Reuse the independent round/formula checks on a real protocol permutation.
    _assert_protocol_round_sequence(reordered, protocol, split)


def test_order_rejects_duplicate_cells(wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    rows = [row for row in manifest["assignments"] if row["split"] == "collection"]
    with pytest.raises(ValueError, match="duplicate model/prompt cell"):
        collection._interleave(rows + [rows[0]], wp6_inputs["protocol"], "collection")


@pytest.mark.parametrize("arm", CALIBRATION_EXTENSION_ARMS)
@pytest.mark.parametrize("tamper", ["prompt", "round", "ambiguity", "twin_order"])
def test_verify_rejects_resealed_extension_row_tampering(tmp_path, wp6_study, wp6_inputs, arm, tamper):
    source, _, _ = wp6_study
    directory = tmp_path / "tampered-extension"
    shutil.copytree(source, directory)
    path = directory / STUDY_MANIFEST
    manifest = read_sealed(path)
    manifest.pop("seal_hash")
    rows = manifest["assignments"]
    index = next(index for index, row in enumerate(rows) if row["arm"] == arm
                 and row["variant"] == ("ambiguity" if tamper == "ambiguity" else "violation"))
    if tamper == "prompt":
        rows[index]["prompt_condition"] = "guided"
    elif tamper == "round":
        rows[index]["round"] += 1
    elif tamper == "ambiguity":
        rows[index]["instructions"] += " tampered"
    else:
        positions = rows[index]["planned_order"], rows[index + 1]["planned_order"]
        rows[index], rows[index + 1] = rows[index + 1], rows[index]
        rows[index]["planned_order"], rows[index + 1]["planned_order"] = positions
    atomic_json(path, seal(manifest))
    verified = verify_study(directory, **wp6_inputs)
    assert verified["valid"] is False
    assert "manifest assignments differs from recomputed study" in verified["errors"]


@pytest.mark.parametrize("split", ["collection", "calibration", "smoke"])
def test_wp1_live_reader_accepts_real_manifest(split, wp6_study, wp6_inputs):
    directory, manifest, _ = wp6_study
    rows, fixtures, source = load_study(directory, split)
    entries = validate_assignment_rows(split, rows, fixtures, wp6_inputs["caps_record"], load_bundle())
    assert len(entries) == manifest["split_counts"][split]
    assert source["study_manifest_hash"] == manifest["seal_hash"]
    assert all(entry["instructions"] == row["instructions"] for entry, row in zip(entries, rows, strict=True))


@pytest.mark.parametrize("tamper", ["row", "fixture", "count", "missing_row", "seal", "malformed",
                                  "order", "planned_order", "twin_order"])
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
    elif tamper == "order":
        manifest["assignments"].reverse()
        positions = Counter()
        for row in manifest["assignments"]:
            row["planned_order"] = positions[row["split"]]
            positions[row["split"]] += 1
    elif tamper == "planned_order":
        manifest["assignments"][0]["planned_order"] += 1
    elif tamper == "twin_order":
        rows = manifest["assignments"]
        index = next(index for index, row in enumerate(rows) if row["arm"] == "collection"
                     and row["variant"] == "violation")
        rows[index], rows[index + 1] = rows[index + 1], rows[index]
        rows[index]["planned_order"], rows[index + 1]["planned_order"] = index, index + 1
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
    if tamper in ("order", "planned_order", "twin_order"):
        assert "manifest assignments differs from recomputed study" in verified["errors"]


def test_build_refuses_unfrozen_caps_without_writing(tmp_path, wp6_inputs):
    inputs = deepcopy(wp6_inputs)
    inputs["caps_record"]["caps_status"] = "candidate"
    with pytest.raises(ValueError, match="frozen"):
        build_study(tmp_path / "study", **inputs)
    assert not (tmp_path / "study").exists()


def test_verify_missing_manifest_returns_errors(tmp_path, wp6_inputs):
    assert verify_study(tmp_path / "missing", **wp6_inputs)["valid"] is False


@pytest.mark.parametrize("arm", HARD_PROBE_ARMS)
def test_hard_probe_cells_pairs_and_one_prompt_rounds(arm, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    protocol = wp6_inputs["protocol"]
    rows = [row for row in manifest["assignments"] if row["arm"] == arm]
    assert len(rows) == 48
    assert {row["round"] for row in rows} == {0, 3, 6}
    assert {row["prompt_condition"] for row in rows} == {"neutral"}
    assert {row["effort"] for row in rows} == {"xhigh" if arm.endswith("xhigh") else "low"}
    assert {row["world_mode"] for row in rows} == {"normal"}
    assert len({row["fixture_id"] for row in rows}) == 16
    for template_id, compounds in zip(protocol["templates"]["calibration"],
                                      [("revoked", "delegated"), ("superseded", "revoked")], strict=True):
        template_rows = [row for row in rows if row["template_id"] == template_id]
        assert {row["compound_type"] for row in template_rows if row["level"] == "H2"} == set(compounds)
        assert Counter(row["level"] for row in template_rows) == {"H1": 6, "H2": 12, "H3": 6}
        for fixture_id in {row["fixture_id"] for row in template_rows}:
            assert {row["model"] for row in template_rows if row["fixture_id"] == fixture_id} == set(protocol["models"])
