"""Deterministic study expansion, balanced assignment order, and sealed offline artifacts."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from ..events import content_hash
from ..peer_reporting.config import read_json
from ..peer_reporting.storage import atomic_json, check_seal, safe_child, seal
from .bundle import load_bundle
from .config import SPLITS, arm_fixture_cells, validate_protocol
from .incidents import validate_template
from .ladder import build_split_fixtures, verify_fixture
from .lanes import trial_policy, validate_caps_record
from .prompts import build_instructions, prompt_manifest

STUDY_MANIFEST = "collection-manifest.json"
ORDER_VERSION = "peer-reporting-v11-seeded-interleaving-v1"


def assignment_identity(row: dict, *, protocol_id: str, tool_manifest_hash: str, caps_hash: str) -> str:
    """Bind every input named by WP6 in a bounded ID; order is not part of cell identity."""
    identity = {key: row[key] for key in ("arm", "split", "fixture_id", "model", "effort",
                                         "prompt_condition", "world_mode")}
    identity.update(protocol_id=protocol_id, instructions_hash=content_hash(row["instructions"]),
                    tool_manifest_hash=tool_manifest_hash, caps_hash=caps_hash)
    return "v11-" + content_hash(identity)


def _interleave(rows: list[dict], seed: int, split: str) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["fixture_id"], row["world_mode"], row["effort"]].append(row)
    rounds = max(len(cells) for cells in groups.values())
    scheduled = []
    for group, cells in groups.items():
        def cell_key(row: dict) -> tuple:
            return row["model"], row["prompt_condition"]

        cells.sort(key=lambda row: content_hash([ORDER_VERSION, seed, split, "cells", group, cell_key(row)]))
        if len({cell_key(row) for row in cells}) != len(cells):
            raise ValueError(f"duplicate model/prompt cell in group {group}")
        for index, row in enumerate(cells):
            round_index = index * rounds // len(cells)
            rank = content_hash([ORDER_VERSION, seed, split, "round", round_index, group, cell_key(row)])
            scheduled.append((round_index, rank, row))
    ordered = [row for _, _, row in sorted(scheduled, key=lambda item: (item[0], item[1]))]
    return [{**row, "planned_order": position} for position, row in enumerate(ordered)]


def _study(protocol: dict, templates: dict[str, dict], caps_record: dict) -> tuple[dict, dict]:
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
        for fixture in split_fixtures:
            fixtures[fixture["fixture_id"]] = fixture
        split_rows = []
        for arm, definition in protocol["arms"].items():
            if definition["split"] != split:
                continue
            selected_cells = None if arm == "smoke" else set(arm_fixture_cells(protocol, arm))
            for fixture in split_fixtures:
                parameters = fixture["parameters"]
                level, variant = parameters["level"], parameters["variant"]
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
                        row.update(arm=arm, model=model, effort=cell["effort"], prompt_condition=cell["prompt"],
                                   world_mode=cell["world_mode"], fixture_id=fixture["fixture_id"],
                                   instructions=instructions)
                        row["assignment_id"] = assignment_identity(row, protocol_id=protocol["protocol_id"],
                                                                   tool_manifest_hash=tools_hash, caps_hash=caps_hash)
                        split_rows.append(row)
        rows.extend(_interleave(split_rows, protocol["seeds"][split], split))
    counts = dict(Counter(row["arm"] for row in rows))
    if counts != allocation["counts"] or len({row["assignment_id"] for row in rows}) != len(rows):
        raise ValueError("expanded assignment counts or identities differ from the protocol")
    manifest = seal({
        "kind": "peer_reporting_v11_study", "protocol_id": protocol["protocol_id"], "protocol": protocol,
        "caps": caps, "caps_hash": caps_hash, "tool_manifest": tools, "tool_manifest_hash": tools_hash,
        "prompt_manifests": {template_id: prompt_manifest(templates[template_id], policy) for template_id in ids},
        "fixtures": {key: {"path": f"fixtures/{key}.json", "content_hash": content_hash(value)}
                     for key, value in sorted(fixtures.items())},
        "assignments": rows, "counts": counts, "split_counts": dict(Counter(row["split"] for row in rows)),
        "total_trials": len(rows), "order_generator": ORDER_VERSION, "live_model_calls": 0,
    })
    return manifest, fixtures


def build_study(directory: Path, *, protocol: dict, templates: dict[str, dict], caps_record: dict) -> dict:
    """Write a fresh study with frozen caps. This operation never starts a model session."""
    manifest, fixtures = _study(protocol, templates, caps_record)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "fixtures").mkdir()
    for fixture_id, fixture in fixtures.items():
        atomic_json(directory / f"fixtures/{fixture_id}.json", seal(fixture))
    atomic_json(directory / STUDY_MANIFEST, manifest)
    return {"directory": str(directory), "seal_hash": manifest["seal_hash"], "plan_hash": manifest["seal_hash"],
            "counts": manifest["counts"], "split_counts": manifest["split_counts"],
            "total_trials": manifest["total_trials"], "fixtures": len(fixtures), "live_model_calls": 0}


def verify_study(directory: Path, *, protocol: dict, templates: dict[str, dict], caps_record: dict) -> dict:
    """Rebuild every input and run the independent fixture verifier; report content errors."""
    directory, errors = Path(directory), []
    report = {"valid": False, "errors": errors, "counts": {}, "split_counts": {}, "total_trials": 0,
              "fixtures": 0, "seal_hash": None, "live_model_calls": 0}
    try:
        expected, expected_fixtures = _study(protocol, templates, caps_record)
        manifest = read_json(directory / STUDY_MANIFEST)
        if type(manifest) is not dict:
            raise ValueError("study manifest must be an object")
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
