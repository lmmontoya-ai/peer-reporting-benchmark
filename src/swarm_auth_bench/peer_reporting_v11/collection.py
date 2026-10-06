"""Deterministic study expansion, paired round order, and sealed offline artifacts.

Each build seals a fresh random ``instance_nonce`` into the manifest (spec 10),
so a rebuild is a different study instance with a different seal, even from the
same inputs. Fixtures, assignment IDs, and the order stay deterministic.
``verify_study`` recomputes everything from the inputs and the recorded nonce.
"""

from __future__ import annotations

import re
import secrets
from collections import Counter, defaultdict
from pathlib import Path

from ..events import content_hash
from ..peer_reporting.config import read_json
from ..peer_reporting.storage import atomic_json, check_seal, safe_child, seal
from .bundle import load_bundle
from .config import (
    CALIBRATION_EXTENSION_ARMS,
    SOCIAL_ARMS,
    SOCIAL_CONSTANTS,
    SOCIAL_FIELDS,
    SPLITS,
    arm_fixture_cells,
    social_fixture_cells,
    validate_protocol,
)
from .incidents import validate_template
from .ladder import build_split_fixtures, calibration_near_miss_types, verify_fixture
from .lanes import trial_policy, validate_caps_record
from .prompts import build_instructions, prompt_manifest

STUDY_MANIFEST = "collection-manifest.json"
ORDER_VERSION = "peer-reporting-v11-paired-rounds-v3"
ORDER_OFFSETS = ((0, 0), (1, 1), (2, 2), (0, 1), (1, 2), (2, 0), (0, 2), (1, 0), (2, 1))
# The three-prompt cycle modulo two would give the extension a 10/4 prompt split.
# These six model/prompt offsets give each paired group of six blocks a 6/6 split.
TWO_PROMPT_ORDER_OFFSETS = ((0, 0), (1, 1), (2, 0), (0, 1), (1, 0), (2, 1))
INSTANCE_NONCE = re.compile(r"[0-9a-f]{32}")


def assignment_identity(row: dict, *, protocol_id: str, tool_manifest_hash: str, caps_hash: str) -> str:
    """Bind every input named by WP6 in a bounded ID; order is not part of cell identity."""
    identity = {key: row[key] for key in ("arm", "split", "fixture_id", "model", "effort",
                                         "prompt_condition", "world_mode")}
    identity.update(protocol_id=protocol_id, instructions_hash=content_hash(row["instructions"]),
                    tool_manifest_hash=tool_manifest_hash, caps_hash=caps_hash)
    return "v11-" + content_hash(identity)


def _social_worlds(protocol: dict, arm: str) -> list[tuple]:
    difficulty = "plain" if arm == "social_anchor_xhigh" else "hard"
    return [(template, difficulty, block) for template in protocol["templates"]["calibration"]
            for block in SOCIAL_CONSTANTS["blocks_per_arm"][arm]]


def _social_group(row: dict, grid: bool) -> int:
    return ({1: 0, 4: 1, 8: 2}.get(row["prevalence_k"], 3) if grid
            else {1: 0, 8: 1}.get(row["prevalence_k"], 2))


def _verify_social_order(rows: list[dict], protocol: dict) -> None:
    """Require A1's complete cells, whole contrast groups, rotation, lane order and balance."""
    models, seed = protocol["models"], protocol["seeds"]["calibration"]
    for arm in SOCIAL_ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        if not selected:
            continue
        grid = arm in SOCIAL_ARMS[2:4]
        group_count = 4 if grid else 3
        worlds = _social_worlds(protocol, arm)
        cells = social_fixture_cells(arm)
        expected = Counter((template, model, *(cell[key] for key in ("variant", *SOCIAL_FIELDS)))
                           for template in protocol["templates"]["calibration"] for model in models for cell in cells)
        actual = Counter((row["template_id"], row["model"], *(row[key] for key in ("variant", *SOCIAL_FIELDS)))
                         for row in selected)
        identities = Counter((row["fixture_id"], row["model"]) for row in selected)
        if actual != expected or set(identities.values()) != {1}:
            raise ValueError(f"{arm}: S round order must cover every fixture-model exactly once")
        if {row["round"] for row in selected} != set(range(group_count)):
            raise ValueError(f"{arm}: S round indices differ from the group count")
        for row in selected:
            w = worlds.index(tuple(row[key] for key in ("template_id", "difficulty", "block")))
            m = models.index(row["model"])
            if _social_group(row, grid) != (row["round"] + m + w) % group_count:
                raise ValueError(f"{arm}: S group must run whole with one model in one round under the rotation")
        for round_index in range(group_count):
            counts = Counter(row["model"] for row in selected if row["round"] == round_index)
            totals = [counts[model] for model in models]
            allowed = {25, 26} if grid else {4}
            if not set(totals) <= allowed or max(totals) - min(totals) > 1:
                raise ValueError(f"{arm}: S per-round model counts must balance within one")
            for m, model in enumerate(models):
                lane = [row for row in selected if row["round"] == round_index and row["model"] == model]
                keys = [content_hash([ORDER_VERSION, seed, arm,
                                      worlds.index(tuple(row[key] for key in ("template_id", "difficulty", "block"))),
                                      m, row["fixture_id"]]) for row in lane]
                if keys != sorted(keys):
                    raise ValueError(f"{arm}: S within-lane order differs from the content hash order")


def _interleave(rows: list[dict], protocol: dict, split: str) -> list[dict]:
    """Run inherited spec 9 rounds, with the A1 rotation and lane order for S only."""
    cells = {}
    arm_fixtures: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        key = tuple(row[name] for name in ("arm", "fixture_id", "model", "prompt_condition",
                                          "effort", "world_mode"))
        if key in cells:
            raise ValueError(f"duplicate model/prompt cell: {key}")
        cells[key] = row
        arm_fixtures[row["arm"]][row["fixture_id"]] = row
    seed, models = protocol["seeds"][split], protocol["models"]
    scheduled = []
    for arm_index, (arm, definition) in enumerate(protocol["arms"].items()):
        if definition["split"] != split:
            continue
        fixtures = arm_fixtures[arm]
        if arm == "smoke":
            for round_index in range(3):
                for cell_index, cell in enumerate(definition["cells"]):
                    model = models[(cell_index + round_index) % 3]
                    for template_index, template_id in enumerate(protocol["templates"][split]):
                        matching = [fixture_id for fixture_id, row in fixtures.items()
                                    if (row["template_id"], row["level"], row["variant"])
                                    == (template_id, cell["level"], cell["variant"])]
                        if len(matching) != 1:
                            raise ValueError("smoke cell must select exactly one fixture per template")
                        row = cells[arm, matching[0], model, cell["prompt"], cell["effort"], cell["world_mode"]]
                        scheduled.append((round_index, arm_index, cell_index, template_index, row))
            continue
        if arm in SOCIAL_ARMS:
            grid = arm in SOCIAL_ARMS[2:4]
            worlds = _social_worlds(protocol, arm)
            for round_index in range(4 if grid else 3):
                for m, model in enumerate(models):
                    for fixture_id, fixture in fixtures.items():
                        w = worlds.index(tuple(fixture[key] for key in ("template_id", "difficulty", "block")))
                        if _social_group(fixture, grid) != (round_index + m + w) % (4 if grid else 3):
                            continue
                        row = cells[arm, fixture_id, model, "neutral", definition["effort"], definition["world_mode"]]
                        order_key = content_hash([ORDER_VERSION, seed, arm, w, m, fixture_id])
                        scheduled.append((round_index, arm_index, m, order_key, row))
            continue
        groups: dict[tuple, list[str]] = defaultdict(list)
        for fixture_id, row in fixtures.items():
            if arm != "channel_failure" and row["variant"] in ("violation", "twin"):
                block_key = ("pair", row["template_id"], row["level"], row["near_miss_type"],
                             row.get("compound_type"))
            else:
                block_key = ("fixture", fixture_id)
            groups[block_key].append(fixture_id)
        blocks = [tuple(sorted(group, key=lambda fixture_id: (fixtures[fixture_id]["variant"] != "violation",
                                                              fixture_id))) for group in groups.values()]
        blocks.sort(key=lambda block: (-len(block), content_hash([ORDER_VERSION, seed, arm, block]), block))
        prompts = definition["prompts"]
        if len(prompts) not in (1, 2, 3):
            raise ValueError(f"{arm}: order requires one, two, or three prompts")
        rounds = range(0, 9, 3) if len(prompts) == 1 else range(3 * len(prompts))
        offsets = TWO_PROMPT_ORDER_OFFSETS if len(prompts) == 2 else ORDER_OFFSETS
        for block_index, block in enumerate(blocks):
            a_offset, c_offset = offsets[block_index % len(offsets)]
            for round_index in rounds:
                if len(prompts) == 3:
                    model = models[(round_index + a_offset) % 3]
                    prompt = protocol["prompt_conditions"][(round_index // 3 + c_offset) % 3]
                elif len(prompts) == 2:
                    model = models[(round_index + a_offset) % 3]
                    prompt = prompts[(round_index // 3 + c_offset) % 2]
                else:
                    model = models[(round_index // 3 + a_offset) % 3]
                    prompt = prompts[0]
                for fixture_index, fixture_id in enumerate(block):
                    row = cells[arm, fixture_id, model, prompt, definition["effort"], definition["world_mode"]]
                    scheduled.append((round_index, arm_index, block_index, fixture_index, row))
    ordered = sorted(scheduled, key=lambda item: item[:-1])
    if len(ordered) != len(rows):
        raise ValueError("round order does not cover every assignment cell")
    # The round is explicit so the live dispatcher can apply the spec 9 round barrier.
    result = [{**item[-1], "planned_order": position, "round": item[0]} for position, item in enumerate(ordered)]
    _verify_social_order(result, protocol)
    return result


def _study(protocol: dict, templates: dict[str, dict], caps_record: dict, *, instance_nonce: str
           ) -> tuple[dict, dict]:
    if type(instance_nonce) is not str or not INSTANCE_NONCE.fullmatch(instance_nonce):
        raise ValueError("the study instance nonce must be 32 lowercase hexadecimal characters")
    allocation = validate_protocol(protocol)
    caps = validate_caps_record(caps_record)
    ids = [template_id for split in SPLITS for template_id in protocol["templates"][split]]
    for template_id in ids:
        template = templates[template_id]
        if template.get("template_id") != template_id:
            raise ValueError(f"template mapping key differs from template_id: {template_id}")
        errors = validate_template(template)
        if errors:
            raise ValueError(f"invalid template {template_id}: {errors}")
    tools = load_bundle().tool_manifest()
    tools_hash, caps_hash = content_hash(tools), content_hash(caps)
    policy = trial_policy(caps)
    fixtures, rows = {}, []
    for split in SPLITS:
        split_fixtures = build_split_fixtures(protocol, templates, split)
        calibration_types = (calibration_near_miss_types(protocol["templates"][split], protocol["seeds"][split],
                                                        protocol=protocol) if split == "calibration" else {})
        for fixture in split_fixtures:
            fixtures[fixture["fixture_id"]] = fixture
        split_rows = []
        for arm, definition in protocol["arms"].items():
            if definition["split"] != split:
                continue
            selected_cells = None if arm == "smoke" else set(arm_fixture_cells(protocol, arm))
            social_cells = social_fixture_cells(arm) if arm in SOCIAL_ARMS else []
            for fixture in split_fixtures:
                parameters = fixture["parameters"]
                level, variant = parameters["level"], parameters["variant"]
                if arm in SOCIAL_ARMS and not any(all(parameters[key] == value for key, value in cell.items())
                                                 for cell in social_cells):
                    continue
                if (arm in CALIBRATION_EXTENSION_ARMS and variant != "ambiguity"
                        and parameters["near_miss_type"] != calibration_types[parameters["template_id"]][0]):
                    continue
                if arm == "smoke":
                    cells = [cell for cell in definition["cells"]
                             if (cell["level"], cell["variant"]) == (level, variant)]
                elif (level, variant) in selected_cells:
                    cells = [{"prompt": prompt, "effort": definition["effort"],
                              "world_mode": definition["world_mode"]} for prompt in definition["prompts"]]
                else:
                    continue
                for cell in cells:
                    instructions = build_instructions(cell["prompt"], templates[parameters["template_id"]], policy)
                    for model in protocol["models"]:
                        row = {key: parameters[key] for key in ("split", "template_id", "level", "variant",
                                                                "near_miss_type")}
                        if "compound_type" in parameters:
                            row["compound_type"] = parameters["compound_type"]
                        if level == "S":
                            row.update({key: parameters[key] for key in SOCIAL_FIELDS})
                        row.update(arm=arm, model=model, effort=cell["effort"], prompt_condition=cell["prompt"],
                                   world_mode=cell["world_mode"], fixture_id=fixture["fixture_id"],
                                   instructions=instructions)
                        row["assignment_id"] = assignment_identity(row, protocol_id=protocol["protocol_id"],
                                                                   tool_manifest_hash=tools_hash, caps_hash=caps_hash)
                        split_rows.append(row)
        rows.extend(_interleave(split_rows, protocol, split))
    counts = dict(Counter(row["arm"] for row in rows))
    if counts != allocation["counts"] or len({row["assignment_id"] for row in rows}) != len(rows):
        raise ValueError("expanded assignment counts or identities differ from the protocol")
    manifest = seal({
        "kind": "peer_reporting_v11_study", "protocol_id": protocol["protocol_id"], "instance_nonce": instance_nonce,
        "protocol": protocol,
        "caps": caps, "caps_hash": caps_hash, "tool_manifest": tools, "tool_manifest_hash": tools_hash,
        "prompt_manifests": {template_id: prompt_manifest(templates[template_id], policy) for template_id in ids},
        "fixtures": {key: {"path": f"fixtures/{key}.json", "content_hash": content_hash(value)}
                     for key, value in sorted(fixtures.items())},
        "assignments": rows, "counts": counts, "split_counts": dict(Counter(row["split"] for row in rows)),
        "total_trials": len(rows), "order_generator": ORDER_VERSION, "live_model_calls": 0,
    })
    return manifest, fixtures


def build_study(directory: Path, *, protocol: dict, templates: dict[str, dict], caps_record: dict) -> dict:
    """Write a fresh study instance with frozen caps. This operation never starts a model session.

    The manifest seals a new random instance nonce, so every build is a new
    study with its own consumed-attempt ledger and its own smoke.
    """
    manifest, fixtures = _study(protocol, templates, caps_record, instance_nonce=secrets.token_hex(16))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "fixtures").mkdir()
    for fixture_id, fixture in fixtures.items():
        atomic_json(directory / f"fixtures/{fixture_id}.json", seal(fixture))
    atomic_json(directory / STUDY_MANIFEST, manifest)
    return {"directory": str(directory), "seal_hash": manifest["seal_hash"], "plan_hash": manifest["seal_hash"],
            "instance_nonce": manifest["instance_nonce"], "counts": manifest["counts"],
            "split_counts": manifest["split_counts"],
            "total_trials": manifest["total_trials"], "fixtures": len(fixtures), "live_model_calls": 0}


def verify_study(directory: Path, *, protocol: dict, templates: dict[str, dict], caps_record: dict) -> dict:
    """Recompute the Latin-square order and sealed inputs under the recorded instance nonce; independently verify
    fixtures."""
    directory, errors = Path(directory), []
    report = {"valid": False, "errors": errors, "counts": {}, "split_counts": {}, "total_trials": 0,
              "fixtures": 0, "seal_hash": None, "instance_nonce": None, "live_model_calls": 0}
    try:
        manifest = read_json(directory / STUDY_MANIFEST)
        if type(manifest) is not dict:
            raise ValueError("study manifest must be an object")
        report["instance_nonce"] = manifest.get("instance_nonce")
        expected, expected_fixtures = _study(protocol, templates, caps_record,
                                             instance_nonce=manifest.get("instance_nonce"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        errors.append(f"study inputs or manifest: {error}")
        return report
    report["seal_hash"] = manifest.get("seal_hash")
    try:
        check_seal(manifest)
    except ValueError as error:
        errors.append(f"manifest: {error}")
    for key in sorted(set(expected) | set(manifest)):
        if key not in manifest or key not in expected or manifest[key] != expected[key]:
            errors.append(f"manifest {key} differs from recomputed study")
    rows = manifest.get("assignments")
    if type(rows) is list and all(type(row) is dict for row in rows):
        counts = dict(Counter(str(row.get("arm")) for row in rows))
        split_counts = dict(Counter(str(row.get("split")) for row in rows))
        report.update(counts=counts, split_counts=split_counts, total_trials=len(rows))
        if counts != expected["counts"] or manifest.get("counts") != counts:
            errors.append("per-arm assignment count mismatch")
        if split_counts != expected["split_counts"] or manifest.get("split_counts") != split_counts:
            errors.append("per-split assignment count mismatch")
        if len(rows) != expected["total_trials"] or manifest.get("total_trials") != len(rows):
            errors.append("total assignment count mismatch")
    else:
        errors.append("assignments must be a list of objects")
    references = manifest.get("fixtures")
    if type(references) is not dict:
        errors.append("fixtures must be an object")
        return report
    report["fixtures"] = len(references)
    for fixture_id, reference in references.items():
        try:
            stored = read_json(safe_child(directory, reference["path"]))
            try:
                check_seal(stored)
            except ValueError as error:
                errors.append(f"fixture {fixture_id}: {error}")
            fixture = {key: value for key, value in stored.items() if key != "seal_hash"}
            if fixture.get("fixture_id") != fixture_id or content_hash(fixture) != reference["content_hash"]:
                errors.append(f"fixture {fixture_id}: sealed reference mismatch")
            if fixture != expected_fixtures.get(fixture_id):
                errors.append(f"fixture {fixture_id}: differs from recomputed fixture")
            template = templates[fixture["parameters"]["template_id"]]
            errors.extend(f"fixture {fixture_id}: {error}" for error in verify_fixture(fixture, template))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            errors.append(f"fixture {fixture_id}: {error}")
    actual_files = {path.name for path in (directory / "fixtures").glob("*.json")}
    if actual_files != {f"{fixture_id}.json" for fixture_id in expected_fixtures}:
        errors.append("fixture file set differs from recomputed study")
    report["valid"] = not errors
    return report
