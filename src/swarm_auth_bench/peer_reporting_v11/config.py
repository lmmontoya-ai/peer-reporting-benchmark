"""Packaged v1.1 contract and independently recomputed allocation counts."""

from __future__ import annotations

import json
from importlib import resources

from ..peer_reporting.config import MODELS
from . import FIXTURE_VERSION, PROTOCOL_ID, SPECIFICATION_REVISION, TOOL_SCHEMA_VERSION
from .lanes import EFFORTS, PROMPT_CONDITIONS, WORLD_MODES

SPLITS = ("collection", "calibration", "smoke")
ARM_DEFINITIONS = {
    "collection": "L0-L4 x {violation, twin} + benign + ambiguity + accusation",
    "channel_failure": "L1 violation",
    "low_effort": "L1 violation, L1 twin, L2 violation, L2 twin, ambiguity",
    "calibration": "L0, L1, L2 x 3 near-miss types, L3, L4; each violation and twin",
}


def arm_fixture_cells(protocol: dict, arm: str) -> list[tuple[str, str]]:
    """Expand the contract's supported fixture descriptions, preserving L2 multiplicity."""
    definition = protocol["arms"][arm]
    if arm not in ARM_DEFINITIONS or definition.get("fixtures") != ARM_DEFINITIONS[arm]:
        raise ValueError(f"unsupported fixture definition for {arm}")
    if arm == "collection":
        return ([(level, variant) for level in protocol["levels"] for variant in protocol["ladder_variants"]]
                + [(level, variant) for variant, level in protocol["control_variants"].items()])
    if arm == "channel_failure":
        return [("L1", "violation")]
    if arm == "low_effort":
        return [(level, variant) for level in ("L1", "L2") for variant in protocol["ladder_variants"]] + [
            (protocol["control_variants"]["ambiguity"], "ambiguity")]
    return [(level, variant) for level in protocol["levels"]
            for _ in range(3 if level == "L2" else 1) for variant in protocol["ladder_variants"]]


def _unique_strings(value: object, name: str) -> list[str]:
    if (type(value) is not list or not value or any(type(item) is not str or not item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must be a nonempty list of unique strings")
    return value


def validate_protocol(protocol: dict) -> dict:
    """Reject invalid definitions or declared counts; return the computed allocation."""
    try:
        constants = {"protocol_id": PROTOCOL_ID, "specification_revision": SPECIFICATION_REVISION,
                     "fixture_version": FIXTURE_VERSION, "tool_schema_version": TOOL_SCHEMA_VERSION}
        if any(protocol[key] != value for key, value in constants.items()):
            raise ValueError("protocol identity or version mismatch")
        models = _unique_strings(protocol["models"], "models")
        if set(models) != set(MODELS):
            raise ValueError("protocol models must be the three approved model IDs")
        for name, expected in (("levels", ["L0", "L1", "L2", "L3", "L4"]),
                               ("ladder_variants", ["violation", "twin"]),
                               ("prompt_conditions", list(PROMPT_CONDITIONS)),
                               ("world_modes", list(WORLD_MODES))):
            if protocol[name] != expected:
                raise ValueError(f"unsupported {name}")
        if protocol["control_variants"] != {"benign": "L1", "ambiguity": "L1", "accusation": "L1"}:
            raise ValueError("unsupported control variants")
        if protocol["efforts"] != {"main": "xhigh", "low_arm": "low"}:
            raise ValueError("unsupported efforts")
        if set(protocol["templates"]) != set(SPLITS) or set(protocol["seeds"]) != set(SPLITS):
            raise ValueError("templates and seeds must name exactly the three splits")
        ids = []
        for split in SPLITS:
            ids.extend(_unique_strings(protocol["templates"][split], f"templates.{split}"))
            if type(protocol["seeds"][split]) is not int:
                raise ValueError(f"seeds.{split} must be an integer")
        if len(set(ids)) != len(ids) or "release-request" not in protocol["templates"]["collection"]:
            raise ValueError("template splits must be disjoint and include the collection anchor")
        if set(protocol["arms"]) != {*ARM_DEFINITIONS, "smoke"}:
            raise ValueError("protocol must define exactly the five study arms")
        counts = {}
        for arm, definition in protocol["arms"].items():
            split = "collection" if arm in ("channel_failure", "low_effort") else arm
            if definition["split"] != split:
                raise ValueError(f"{arm}: wrong split")
            if arm == "smoke":
                cells = definition["cells"]
                if type(cells) is not list or not cells:
                    raise ValueError("smoke needs explicit cells")
                seen = set()
                for cell in cells:
                    if (set(cell) != {"level", "variant", "prompt", "effort", "world_mode"}
                            or cell["level"] not in protocol["levels"]
                            or cell["variant"] not in protocol["ladder_variants"]
                            or cell["prompt"] not in PROMPT_CONDITIONS or cell["effort"] not in EFFORTS
                            or cell["world_mode"] not in WORLD_MODES
                            or (cell["world_mode"] == "report_store_unavailable"
                                and (cell["level"], cell["variant"]) != ("L1", "violation"))):
                        raise ValueError("invalid smoke cell")
                    identity = tuple(cell[key] for key in sorted(cell))
                    if identity in seen:
                        raise ValueError("duplicate smoke cell")
                    seen.add(identity)
                per_template = len(cells)
            else:
                fixtures = arm_fixture_cells(protocol, arm)
                if ("fixtures_per_template" in definition
                        and (type(definition["fixtures_per_template"]) is not int
                             or definition["fixtures_per_template"] != len(fixtures))):
                    raise ValueError(f"{arm}: fixtures_per_template mismatch")
                prompts = _unique_strings(definition["prompts"], f"{arm}.prompts")
                if (any(prompt not in PROMPT_CONDITIONS for prompt in prompts)
                        or definition["effort"] not in EFFORTS or definition["world_mode"] not in WORLD_MODES):
                    raise ValueError(f"{arm}: invalid prompt, effort, or world mode")
                if (definition["world_mode"] == "report_store_unavailable"
                        and any(cell != ("L1", "violation") for cell in fixtures)):
                    raise ValueError(f"{arm}: unavailable report store requires L1 violation")
                per_template = len(fixtures) * len(prompts)
            counts[arm] = len(protocol["templates"][split]) * per_template * len(models)
            if type(definition["trials"]) is not int or definition["trials"] != counts[arm]:
                raise ValueError(f"{arm}: declared trials differ from computed {counts[arm]}")
        total = sum(counts.values())
        if type(protocol["total_trials"]) is not int or protocol["total_trials"] != total:
            raise ValueError(f"declared total_trials differs from computed {total}")
        return {"valid": True, "protocol_id": PROTOCOL_ID, "counts": counts, "total_trials": total}
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"malformed v1.1 protocol: {error}") from error


def load_protocol() -> dict:
    """Read the packaged contract, which must remain byte-identical to docs/v11/protocol.json."""
    protocol = json.loads(resources.files(__package__).joinpath("protocol.json").read_text(encoding="utf-8"))
    validate_protocol(protocol)
    return protocol
