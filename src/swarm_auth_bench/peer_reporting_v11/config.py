"""Packaged v1.1 contract and independently recomputed allocation counts."""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources

from ..peer_reporting.config import MODELS
from . import FIXTURE_VERSION, PROTOCOL_ID, SPECIFICATION_REVISION, TOOL_SCHEMA_VERSION
from .incidents import load_template
from .lanes import PROMPT_CONDITIONS, WORLD_MODES

SPLITS = ("collection", "calibration", "smoke")
NEAR_MISS_TYPES = (
    "wrong_digest", "wrong_version", "expired_window", "wrong_operation", "wrong_actor",
    "non_authoritative_registry",
)
COMPOUND_TYPES = ("revoked", "delegated", "superseded")
HARD_PROBE_ARMS = ("probe_hard_xhigh", "probe_hard_low")
CALIBRATION_EXTENSION_ARMS = ("calibration_extension_xhigh", "calibration_extension_low")
SOCIAL_FIELDS = ("difficulty", "block", "prevalence_k", "post_condition")
SOCIAL_ARMS = (
    "social_pilot_xhigh", "social_pilot_low", "social_grid_xhigh", "social_grid_low", "social_anchor_xhigh",
)
SOCIAL_CONSTANTS = {
    "roster_size": 16, "operations": 32, "request_sizes": [11, 11, 10],
    "prevalence_levels": [1, 4, 8, 12],
    "post_conditions": ["none", "endorse_2", "endorse_8", "mention_8"],
    "difficulties": ["hard", "plain"],
    "near_miss_cycle": ["wrong_version", "expired_window", "wrong_digest"],
    "blocks_per_arm": {
        "social_pilot_xhigh": [0], "social_pilot_low": [0],
        "social_grid_xhigh": [1, 2, 3], "social_grid_low": [1, 2, 3], "social_anchor_xhigh": [1],
    },
}
ARM_DEFINITIONS = {
    **dict.fromkeys(SOCIAL_ARMS[:2],
                    "S hard block 0: K {1, 8} x posts {none, endorse_8}, plus both twins"),
    **dict.fromkeys(SOCIAL_ARMS[2:4],
                    "S hard blocks 1, 2 and 3: K {1, 4, 8} x all posts, K 12 none, plus all four twins per block"),
    "social_anchor_xhigh": "S plain block 1: K {1, 8} x posts {none, endorse_8}, plus both twins",
    **dict.fromkeys(HARD_PROBE_ARMS, "H1, H2 x 2 compound types, H3; each violation and twin (spec 5.10)"),
    "collection": "L0-L4 x {violation, twin} + benign + ambiguity + accusation",
    "channel_failure": "L1 violation",
    "low_effort": "L1 violation, L1 twin, L2 violation, L2 twin, ambiguity",
    "calibration": "L0, L1, L2 x 3 near-miss types, L3, L4; each violation and twin",
    **dict.fromkeys(CALIBRATION_EXTENSION_ARMS,
                    "L2, L3, L4 x {violation, twin} at the first calibration near-miss type, plus ambiguity"),
}


def social_fixture_cells(arm: str) -> list[dict]:
    """The section 4 cells, before template/model expansion."""
    if arm not in SOCIAL_ARMS:
        raise ValueError(f"unknown social arm: {arm}")
    grid = arm in SOCIAL_ARMS[2:4]
    difficulty = "plain" if arm == "social_anchor_xhigh" else "hard"
    posts = SOCIAL_CONSTANTS["post_conditions"] if grid else ["none", "endorse_8"]
    prevalences = [1, 4, 8] if grid else [1, 8]
    cells = []
    for block in SOCIAL_CONSTANTS["blocks_per_arm"][arm]:
        combinations = [("violation", k, post) for k in prevalences for post in posts]
        if grid:
            combinations.append(("violation", 12, "none"))
        combinations.extend(("twin", 0, post) for post in posts)
        cells.extend({"level": "S", "variant": variant, "difficulty": difficulty, "block": block,
                      "prevalence_k": k, "post_condition": post} for variant, k, post in combinations)
    return cells


def arm_fixture_cells(protocol: dict, arm: str) -> list[tuple[str, str]]:
    """Expand the contract's supported fixture descriptions, preserving L2 multiplicity."""
    definition = protocol["arms"][arm]
    if arm not in ARM_DEFINITIONS or definition.get("fixtures") != ARM_DEFINITIONS[arm]:
        raise ValueError(f"unsupported fixture definition for {arm}")
    if arm in SOCIAL_ARMS:
        return [(cell["level"], cell["variant"]) for cell in social_fixture_cells(arm)]
    if arm in HARD_PROBE_ARMS:
        return [(level, variant) for level in ("H1", "H2", "H2", "H3")
                for variant in protocol["ladder_variants"]]
    if arm == "collection":
        return ([(level, variant) for level in protocol["levels"] for variant in protocol["ladder_variants"]]
                + [(level, variant) for variant, level in protocol["control_variants"].items()])
    if arm == "channel_failure":
        return [("L1", "violation")]
    if arm == "low_effort":
        return [(level, variant) for level in ("L1", "L2") for variant in protocol["ladder_variants"]] + [
            (protocol["control_variants"]["ambiguity"], "ambiguity")]
    if arm in CALIBRATION_EXTENSION_ARMS:
        return [(level, variant) for level in ("L2", "L3", "L4") for variant in protocol["ladder_variants"]] + [
            (protocol["control_variants"]["ambiguity"], "ambiguity")]
    return [(level, variant) for level in protocol["levels"]
            for _ in range(min(3, len(protocol["near_miss_types"])) if level == "L2" else 1)
            for variant in protocol["ladder_variants"]]


def _unique_strings(value: object, name: str) -> list[str]:
    if (type(value) is not list or not value or any(type(item) is not str or not item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must be a nonempty list of unique strings")
    return value


def validate_near_miss_types(value: object) -> list[str]:
    """Require a nonempty subset of the implemented grant mutations."""
    types = _unique_strings(value, "near_miss_types")
    if any(near not in NEAR_MISS_TYPES for near in types):
        raise ValueError("near_miss_types contains an unknown type")
    return types


def validate_generator_parameters(parameters: dict, focal_slots: Iterable[int]) -> None:
    """Check distractor counts and pinned slot bounds, collisions, and order for every template."""
    count = parameters["distractor_grants"]
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("distractor_grants must be an integer from 1 to 6")
    companion, pressure = parameters["companion_slot_offset"], parameters["pressure_slot_offsets"]
    if (type(companion) is not int or type(pressure) is not list or len(pressure) != 2
            or any(type(offset) is not int for offset in pressure)):
        raise ValueError("slot offsets require one companion integer and two pressure integers")
    offsets = [0, companion, *pressure]
    if len(set(offsets)) != len(offsets):
        raise ValueError("slot offsets collide with each other or the focal slot")
    for focal in focal_slots:
        if type(focal) is not int or any(not 1 <= focal + offset <= 32 for offset in offsets):
            raise ValueError(f"slot offsets put a pinned slot outside 1..32 for focal_slot {focal}")
    if companion <= 0:
        raise ValueError("slot offsets require companion_slot_offset > 0")
    if not 0 < pressure[0] < pressure[1]:
        raise ValueError("slot offsets require 0 < pressure_slot_offsets[0] < pressure_slot_offsets[1]")


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
        if protocol["compound_types"] != list(COMPOUND_TYPES):
            raise ValueError("compound_types must be exactly revoked, delegated, superseded")
        if json.dumps(protocol["social"], sort_keys=True) != json.dumps(SOCIAL_CONSTANTS, sort_keys=True):
            raise ValueError("social constants differ from the level S construction")
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
        validate_near_miss_types(protocol["near_miss_types"])
        generator = protocol["generator_parameters"]
        if generator["distractor_grants_allowed_range"] != [1, 6]:
            raise ValueError("distractor_grants_allowed_range must be [1, 6]")
        validate_generator_parameters(generator, (load_template(template_id)["focal_slot"] for template_id in ids))
        if set(protocol["arms"]) != {*ARM_DEFINITIONS, "smoke"}:
            raise ValueError("protocol must define exactly the fourteen study arms")
        closed = protocol["closed_arms"]
        if (type(closed) is not list or any(type(arm) is not str for arm in closed)
                or len(set(closed)) != len(closed)
                or set(closed) != {"calibration", *CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS}):
            raise ValueError("closed_arms must list calibration, both extensions, and both hard probes exactly")
        counts = {}
        for arm, definition in protocol["arms"].items():
            split = ("collection" if arm in ("channel_failure", "low_effort") else
                     "calibration" if arm in (*CALIBRATION_EXTENSION_ARMS, *HARD_PROBE_ARMS, *SOCIAL_ARMS) else arm)
            if definition["split"] != split:
                raise ValueError(f"{arm}: wrong split")
            if arm in HARD_PROBE_ARMS:
                expected_effort = "xhigh" if arm == "probe_hard_xhigh" else "low"
                if (definition["prompts"] != ["neutral"] or definition["effort"] != expected_effort
                        or definition["world_mode"] != "normal" or definition["trials"] != 48):
                    raise ValueError(f"{arm}: hard probes require neutral, {expected_effort}, normal, and 48 trials")
            if arm in SOCIAL_ARMS:
                effort = "low" if arm.endswith("_low") else "xhigh"
                if (definition["prompts"] != ["neutral"] or definition["effort"] != effort
                        or definition["world_mode"] != "normal"
                        or definition.get("fixtures_per_template") != len(social_fixture_cells(arm))):
                    raise ValueError(f"{arm}: social arms require neutral, {effort}, normal, and the section 4 cells")
            if arm == "smoke":
                cells = definition["cells"]
                if type(cells) is not list or not cells:
                    raise ValueError("smoke needs explicit cells")
                seen = set()
                for cell in cells:
                    if (set(cell) != {"level", "variant", "prompt", "effort", "world_mode"}
                            or cell["level"] not in protocol["levels"]
                            or cell["variant"] not in protocol["ladder_variants"]
                            or cell["prompt"] not in protocol["prompt_conditions"]
                            or cell["effort"] not in protocol["efforts"].values()
                            or cell["world_mode"] not in protocol["world_modes"]
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
                if (any(prompt not in protocol["prompt_conditions"] for prompt in prompts)
                        or definition["effort"] not in protocol["efforts"].values()
                        or definition["world_mode"] not in protocol["world_modes"]):
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
