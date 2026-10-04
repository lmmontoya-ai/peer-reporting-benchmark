"""Strict candidate configuration and immutable study allocation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import MAXIMUM_PACKET_DEVIATION_FRACTION, PROTOCOL_ID, SPECIFICATION_REVISION

MODELS = ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")
CONDITIONS = ("none", "soft", "active")
CELLS = ((16, 1), (100, 0), (100, 1))
CAP_FIELDS = frozenset({
    "max_trial_wall_seconds", "drain_grace_seconds", "max_tool_requests_per_trial",
    "trial_observed_token_stop_target", "reserved_tokens_per_trial",
    "collection_wall_seconds", "collection_observed_token_stop_target", "max_concurrency",
})
INTEGER_CAPS = CAP_FIELDS - {"max_trial_wall_seconds", "drain_grace_seconds", "collection_wall_seconds"}
INDIVIDUAL_CAP_FIELDS = frozenset({
    "max_trial_wall_seconds", "drain_grace_seconds", "max_tool_requests_per_trial",
    "trial_observed_token_stop_target", "reserved_tokens_per_trial",
})
PHASES = frozenset({"smoke", "collection"})
INDIVIDUAL_LIMITS = {
    "max_trial_wall_seconds": 180, "drain_grace_seconds": 10, "max_tool_requests_per_trial": 16,
    "trial_observed_token_stop_target": 150000, "reserved_tokens_per_trial": 190000, "max_concurrency": 1,
}
PHASE_LIMITS = {
    "smoke": {**INDIVIDUAL_LIMITS, "collection_wall_seconds": 1965,
              "collection_observed_token_stop_target": 1710000},
    "collection": {**INDIVIDUAL_LIMITS, "collection_wall_seconds": 11190,
                   "collection_observed_token_stop_target": 10260000},
}
EXPECTED_COUNTS = {"main_cells": 3, "main_assignments": 54, "positive_main_assignments": 36,
                   "zero_violation_main_assignments": 18, "diagnostic_assignments": 0,
                   "collection_assignments": 54, "smoke_assignments": 9}
FROZEN_PACKET_BYTE_TARGET = 31735


def reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)


def load_contract() -> dict[str, Any]:
    return json.loads(files(__package__).joinpath("protocol.json").read_text(encoding="utf-8"))


def validate_caps(caps: dict[str, Any]) -> None:
    if not isinstance(caps, dict) or set(caps) != CAP_FIELDS:
        raise ValueError("caps must have exactly the eight declared resource fields")
    for key, value in caps.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a positive number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
        if key in INTEGER_CAPS and type(value) is not int:
            raise ValueError(f"{key} must be an integer")
    if caps["reserved_tokens_per_trial"] < caps["trial_observed_token_stop_target"]:
        raise ValueError("reservation is smaller than the individual token stop")
    if caps["collection_observed_token_stop_target"] < caps["reserved_tokens_per_trial"]:
        raise ValueError("collection target cannot admit one reservation")


@dataclass(frozen=True)
class StudyConfig:
    """Freeze both phase budgets before smoke under one collection plan.

    Changing either phase's budget requires a new plan and new smoke evidence;
    separate aggregate caps do not permit reusing smoke from another plan.
    """

    protocol_id: str = PROTOCOL_ID
    collection_revision: str = "offline-candidate-1"
    seed: int = 173
    caps: dict[str, Any] | None = None
    phase_caps: dict[str, dict[str, Any]] | None = None
    packet_byte_target: int | None = None

    def __post_init__(self) -> None:
        if self.protocol_id != PROTOCOL_ID:
            raise ValueError("unsupported protocol version")
        if not isinstance(self.collection_revision, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.collection_revision
        ):
            raise ValueError("collection_revision must be a bounded identifier")
        if type(self.seed) is not int or not 0 <= self.seed < 2**63:
            raise ValueError("seed must be an integer in [0, 2**63)")
        if self.caps is not None and self.phase_caps is not None:
            raise ValueError("caps and phase_caps are mutually exclusive")
        if self.caps is not None:
            raise ValueError("N100 requires separate frozen phase_caps; shared caps cannot represent both phases")
        if self.phase_caps is not None:
            if not isinstance(self.phase_caps, dict) or set(self.phase_caps) != PHASES:
                raise ValueError("phase_caps must contain exactly smoke and collection")
            for caps in self.phase_caps.values():
                validate_caps(caps)
            if self.phase_caps != PHASE_LIMITS:
                raise ValueError("phase_caps differ from the immutable N100 candidate limits")
            if any(self.phase_caps["smoke"][field] != self.phase_caps["collection"][field]
                   for field in INDIVIDUAL_CAP_FIELDS):
                raise ValueError("smoke and collection must have the same individual trial caps")
            object.__setattr__(self, "phase_caps", {phase: dict(caps) for phase, caps in self.phase_caps.items()})
        if self.packet_byte_target is not None and (
            type(self.packet_byte_target) is not int or self.packet_byte_target <= 0
        ):
            raise ValueError("packet_byte_target must be a positive integer")
        if self.packet_byte_target is not None and self.packet_byte_target != FROZEN_PACKET_BYTE_TARGET:
            raise ValueError("packet_byte_target differs from the frozen N100 target")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StudyConfig:
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        unknown = set(value) - {"protocol_id", "collection_revision", "seed", "caps",
                               "phase_caps", "packet_byte_target"}
        if unknown:
            raise ValueError(f"unknown configuration fields: {sorted(unknown)}")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for optional in ("phase_caps", "packet_byte_target"):
            if value[optional] is None:
                value.pop(optional)
        return value

    def caps_for(self, split: str) -> dict[str, Any] | None:
        """Return the split's frozen limits, falling back to legacy shared caps."""
        if split not in PHASES:
            raise ValueError("split must be smoke or collection")
        selected = self.caps if self.phase_caps is None else self.phase_caps[split]
        return dict(selected) if selected is not None else None

    def require_matching_caps(self, supplied: dict[str, Any], split: str = "collection") -> None:
        validate_caps(supplied)
        frozen = self.caps_for(split)
        if frozen is None or supplied != frozen:
            raise ValueError("caps do not match the sealed execution configuration")


def validate_contract(contract: dict[str, Any] | None = None) -> dict[str, int]:
    value = load_contract() if contract is None else contract
    if type(value) is not dict:
        raise ValueError("protocol contract must be an object")
    if value.get("protocol_id") != PROTOCOL_ID or value.get("specification_revision") != SPECIFICATION_REVISION:
        raise ValueError("protocol contract revision mismatch")
    if value.get("pairing", {}).get("maximum_packet_deviation_fraction") != MAXIMUM_PACKET_DEVIATION_FRACTION:
        raise ValueError("protocol packet tolerance differs from the approved revision")
    cells = [(row["N"], k) for row in value["main_grid"] for k in row["K"]]
    if tuple(cells) != CELLS or tuple(value["models"]) != MODELS:
        raise ValueError("contract changed the approved grid or models")
    if set(value["monitoring_blocks"]) != set(CONDITIONS) or value["main_repetitions"] != [1, 2]:
        raise ValueError("contract changed conditions or fixture blocks")
    counts = dict(EXPECTED_COUNTS)
    if value["expected_counts"] != counts:
        raise ValueError("contract allocation counts do not match the approved grid")
    if value["diagnostic_controls"] != [] or value["smoke"] != {
            "split": "smoke", "N": 100, "K": 1, "repetitions": [1],
            "fixture_distinct_from_collection": True}:
        raise ValueError("N100 has no diagnostic variants and smoke must be the distinct 100/K1 fixture")
    if (value.get("reasoning_effort") != "xhigh" or value["world"].get("initial_peer_posts") != 100
            or value["pairing"].get("master_roster_size") != 100
            or value["resource_contract"].get("candidate_phase_caps") != PHASE_LIMITS):
        raise ValueError("N100 reasoning, roster, posts or immutable limits differ from contract")
    if (value["world"].get("posts_per_peer_by_N") != {
            "16": {"peers_with_six_posts": 12, "peers_with_seven_posts": 4},
            "100": {"peers_with_one_post": 100}}
            or value["pairing"].get("focal_post_slot_one_based_by_block") != {"1": 16, "2": 66}
            or value["pairing"].get("common_packet_byte_target") != 31735):
        raise ValueError("N100 repetition, focal slot or frozen byte target differs from contract")
    return counts
