"""Offline fixture expansion, sealed allocation, and independent archive checks."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from ..events import content_hash
from .config import CELLS, CONDITIONS, MODELS, StudyConfig, load_contract, validate_contract
from .fixtures import audit_packet_lengths, build_collection_fixtures, build_fixture, field_differences
from .prompts import COMMON_INSTRUCTIONS, MONITORING_BLOCKS, build_instructions
from .storage import atomic_json, read_sealed, safe_child, seal


def _rank(values: list, seed: int, *parts: Any) -> list:
    return sorted(values, key=lambda item: content_hash(["sha256-sort-v1", seed, *parts, item]))


def tool_manifest() -> dict[str, Any]:
    # Import here so configuration-only commands need no runtime or world instance.
    from .schemas import INPUT_SCHEMAS, OUTPUT_SCHEMAS, SCHEMA_VERSION, TOOL_DESCRIPTORS

    return {"version": SCHEMA_VERSION, "input_schemas": INPUT_SCHEMAS,
            "output_schemas": OUTPUT_SCHEMAS, "descriptors": TOOL_DESCRIPTORS}


def assignment_identity(row: dict[str, Any]) -> str:
    return "pa-" + content_hash(row["input_identity"])


def _assignments(fixtures: list[dict], config: StudyConfig, tools: dict) -> list[dict]:
    combinations = [(model, condition) for model in MODELS for condition in CONDITIONS]
    groups = {fixture["fixture_id"]: fixture for fixture in fixtures}
    permutations = {key: _rank(combinations, config.seed, "conditions", key) for key in groups}
    result = []
    for round_index in range(9):
        for key in _rank(list(groups), config.seed, "fixture-round", round_index):
            fixture = groups[key]
            model, condition = permutations[key][round_index]
            p = fixture["parameters"]
            instructions = build_instructions(condition, config.caps_for(p["split"]))
            messages = [{"role": "system", "content": instructions},
                        {"role": "user", "content": fixture["packet"]}]
            identity = {
                "protocol_id": config.protocol_id, "collection_revision": config.collection_revision,
                "fixture_id": key, "public_hash": fixture["public_hash"],
                "complete_world_hash": fixture["complete_world_hash"],
                "instructions_and_roles_hash": content_hash(messages),
                "requested_model": model, "prompt_condition": condition, "reasoning_effort": "xhigh",
                "tool_manifest_hash": content_hash(tools), "execution_config": config.to_dict(),
            }
            row = {
                "input_identity": identity, "fixture_id": key,
                "fixture_path": f"fixtures/{key}.json", "split": p["split"],
                "N": p["n"], "K": fixture["truth"]["curve_K"], "block": p["block"],
                "variant": p["variant"], "model": model, "prompt_condition": condition,
                "instructions": instructions, "messages": messages,
                "round": round_index, "planned_order": len(result),
                "status": "unrun", "attempts": [],
            }
            row["assignment_id"] = assignment_identity(row)
            result.append(row)
    return result


def _comparison_audits(fixtures: list[dict]) -> list[dict]:
    main = {(f["parameters"]["n"], f["parameters"]["k"], f["parameters"]["block"]): f
            for f in fixtures if f["parameters"]["variant"] == "main"}
    comparisons = []
    for block in (1, 2):
        comparisons.extend([(main[16, 1, block], main[100, 1, block], "fixed_K1_N16_N100"),
                            (main[100, 0, block], main[100, 1, block], "N100_K0_K1_false_alert_control")])
    return [{"left_fixture_id": left["fixture_id"], "right_fixture_id": right["fixture_id"],
             "comparison": label, "changes": field_differences(left["public"], right["public"]),
             "left_offsets": left["audit"]["byte_ranges"], "right_offsets": right["audit"]["byte_ranges"],
             "benign_cue_review": "pending"} for left, right, label in comparisons]


def build_collection(directory: Path, config: StudyConfig) -> dict[str, Any]:
    validate_contract()
    # Validate copied limits again in case a caller mutated a nested config dict.
    config = StudyConfig.from_dict(config.to_dict())
    fixtures = build_collection_fixtures(config.seed)
    smoke = build_fixture(100, 1, split="smoke", seed=config.seed)
    tools = tool_manifest()
    rows = _assignments(fixtures, config, tools) + _assignments([smoke], config, tools)
    counts = Counter(row["split"] for row in rows)
    if counts != {"collection": 54, "smoke": 9}:
        raise ValueError("internal allocation error")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "fixtures").mkdir()
    fixture_index = {}
    for fixture in fixtures + [smoke]:
        relative = f"fixtures/{fixture['fixture_id']}.json"
        atomic_json(directory / relative, seal(fixture))
        fixture_index[fixture["fixture_id"]] = {"path": relative, "content_hash": content_hash(fixture)}
    source_files = sorted(Path(__file__).parent.glob("*.py")) + [Path(__file__).with_name("protocol.json")]
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    length_audit = audit_packet_lengths(fixtures + [smoke], config.packet_byte_target)
    manifest = seal({
        "kind": "peer_reporting_offline_plan", "protocol_id": config.protocol_id,
        "config": config.to_dict(), "contract": load_contract(), "tool_manifest": tools,
        "common_instructions": COMMON_INSTRUCTIONS, "monitoring_blocks": MONITORING_BLOCKS,
        "prompt_strings": {condition: build_instructions(condition, config.caps_for("collection"))
                           for condition in CONDITIONS},
        "fixtures": fixture_index, "assignments": rows, "counts": dict(counts),
        "order_generator": "sha256-sort-v1/nine-interleaved-rounds",
        "packet_length_audit": length_audit,
        "comparison_audits": _comparison_audits(fixtures), "implementation_hashes": sources,
        "live_ready": False,
        "blockers": ["packet_length_matching_infeasible"] if not length_audit["common_target_feasible"] else [],
        "pending": ["numerical_caps" if config.caps_for("collection") is None
                    else "live_cap_approval_and_qualification",
                    *(["common_packet_length_target"] if config.packet_byte_target is None else []),
                    "benign_text_cue_review",
                    "three_model_qualification", "nine_live_smoke_records", "human_review_sample_plan"],
    })
    atomic_json(directory / "collection-manifest.json", manifest)
    atomic_json(directory / "collection-index.json", seal({
        "plan_hash": manifest["seal_hash"],
        "assignments": {row["assignment_id"]: {"status": "unrun", "attempts": [], "offline_examples": []}
                        for row in rows},
    }))
    # The sealed plan stores planned order. This separate log starts empty.
    (directory / "realized-order.jsonl").touch(exist_ok=False)
    return {"directory": str(directory), "plan_hash": manifest["seal_hash"],
            "counts": dict(counts), "live_ready": False, "pending": manifest["pending"],
            "blockers": manifest["blockers"]}


def verify_collection(directory: Path) -> dict[str, Any]:
    """Reject corrupt plans; report candidate readiness separately from validity."""
    from .fixtures import verify_fixture

    directory = Path(directory)
    manifest = read_sealed(directory / "collection-manifest.json")
    validate_contract(manifest["contract"])
    config = StudyConfig.from_dict(manifest["config"])
    if (manifest["contract"] != load_contract() or manifest["common_instructions"] != COMMON_INSTRUCTIONS
            or manifest["monitoring_blocks"] != MONITORING_BLOCKS
            or manifest["prompt_strings"] != {
                condition: build_instructions(condition, config.caps_for("collection"))
                for condition in CONDITIONS}
            or manifest["tool_manifest"] != tool_manifest()):
        raise ValueError("declared contract, instructions or tools differ from the frozen implementation")
    index = read_sealed(directory / "collection-index.json")
    if index["plan_hash"] != manifest["seal_hash"]:
        raise ValueError("index belongs to another plan")
    fixtures = {}
    for key, reference in manifest["fixtures"].items():
        fixture = read_sealed(safe_child(directory, reference["path"]))
        payload = {k: v for k, v in fixture.items() if k != "seal_hash"}
        if content_hash(payload) != reference["content_hash"] or fixture["fixture_id"] != key:
            raise ValueError("fixture checkpoint mismatch")
        errors = verify_fixture(payload)
        if errors:
            raise ValueError(f"invalid fixture {key}: {errors}")
        fixtures[key] = payload
    rows = manifest["assignments"]
    if len(rows) != 63 or len({row["assignment_id"] for row in rows}) != 63:
        raise ValueError("assignment count or uniqueness mismatch")
    if set(index["assignments"]) != {row["assignment_id"] for row in rows}:
        raise ValueError("index omits or adds an assignment")
    for entry in index["assignments"].values():
        if (not isinstance(entry, dict) or entry.get("status") != "unrun" or entry.get("attempts") != []
                or not isinstance(entry.get("offline_examples"), list)):
            raise ValueError("offline index cannot claim a live attempt or a resolved assignment")
    length_audit = audit_packet_lengths(list(fixtures.values()), config.packet_byte_target)
    blockers = ["packet_length_matching_infeasible"] if not length_audit["common_target_feasible"] else []
    if manifest["packet_length_audit"] != length_audit or manifest["blockers"] != blockers:
        raise ValueError("packet length audit or blockers differ from the verified fixtures")
    expected = Counter()
    for n, k in CELLS:
        for block in (1, 2):
            for model in MODELS:
                for condition in CONDITIONS:
                    expected[("collection", "main", n, k, block, model, condition)] += 1
    for model in MODELS:
        for condition in CONDITIONS:
            expected[("smoke", "main", 100, 1, 1, model, condition)] += 1
    actual = Counter((r["split"], r["variant"], r["N"], r["K"], r["block"], r["model"],
                      r["prompt_condition"]) for r in rows)
    if actual != expected:
        raise ValueError("assignment matrix differs from the approved allocation")
    expected_fixture_cells = {
        ("collection", "main", n, k, block) for n, k in ((16, 1), (100, 0), (100, 1)) for block in (1, 2)
    } | {("smoke", "main", 100, 1, 1)}
    actual_fixture_cells = {
        (f["parameters"]["split"], f["parameters"]["variant"], f["parameters"]["n"],
         f["truth"]["curve_K"], f["parameters"]["block"]) for f in fixtures.values()
    }
    if len(fixtures) != 7 or actual_fixture_cells != expected_fixture_cells:
        raise ValueError("fixture inventory differs from the six main plus separate smoke fixtures")
    if set(fixtures) != {row["fixture_id"] for row in rows}:
        raise ValueError("assignment references do not cover the exact fixture inventory")
    if manifest["counts"] != {"collection": 54, "smoke": 9} or manifest["protocol_id"] != config.protocol_id:
        raise ValueError("manifest count or protocol claim differs from the allocation")
    if manifest["comparison_audits"] != _comparison_audits(
            [fixture for fixture in fixtures.values() if fixture["parameters"]["split"] == "collection"]):
        raise ValueError("comparison audits differ from verified fixtures")
    for row in rows:
        fixture = fixtures[row["fixture_id"]]
        if assignment_identity(row) != row["assignment_id"]:
            raise ValueError("assignment identity mismatch")
        identity = row["input_identity"]
        parameters = fixture["parameters"]
        if row.get("status") != "unrun" or row.get("attempts") != []:
            raise ValueError("sealed assignment cannot claim execution or replacement attempts")
        if (row["fixture_path"] != manifest["fixtures"][row["fixture_id"]]["path"]
                or identity["fixture_id"] != row["fixture_id"]
                or row["model"] != identity["requested_model"]
                or row["prompt_condition"] != identity["prompt_condition"]
                or identity["reasoning_effort"] != "xhigh"
                or identity["protocol_id"] != config.protocol_id
                or identity["collection_revision"] != config.collection_revision
                or row["N"] != parameters["n"] or row["K"] != fixture["truth"]["curve_K"]
                or row["block"] != parameters["block"] or row["split"] != parameters["split"]
                or row["variant"] != parameters["variant"]
                or row["instructions"] != build_instructions(row["prompt_condition"], config.caps_for(row["split"]))
                or row["instructions"] != manifest["prompt_strings"][row["prompt_condition"]]):
            raise ValueError("assignment labels, fixture path, or instructions differ from sealed identity")
        if (identity["instructions_and_roles_hash"] != content_hash(row["messages"])
                or row["messages"] != [{"role": "system", "content": row["instructions"]},
                                       {"role": "user", "content": fixture["packet"]}]
                or identity["complete_world_hash"] != fixture["complete_world_hash"]
                or identity["public_hash"] != fixture["public_hash"]
                or identity["execution_config"] != config.to_dict()
                or identity["tool_manifest_hash"] != content_hash(manifest["tool_manifest"])):
            raise ValueError("assignment input binding mismatch")
    for split, size in (("collection", 6), ("smoke", 1)):
        subset = [row for row in rows if row["split"] == split]
        all_fixtures = {row["fixture_id"] for row in subset}
        for position, row in enumerate(subset):
            if row["planned_order"] != position or row["round"] != position // size:
                raise ValueError("planned order or round does not match the sealed interleaving")
        for round_index in range(9):
            group = subset[round_index * size:(round_index + 1) * size]
            expected_order = _rank(list(all_fixtures), config.seed, "fixture-round", round_index)
            if [row["fixture_id"] for row in group] != expected_order:
                raise ValueError("fixture group order does not match the declared seed")
            for row in group:
                combination = _rank([(model, condition) for model in MODELS for condition in CONDITIONS],
                                    config.seed, "conditions", row["fixture_id"])[round_index]
                if (row["model"], row["prompt_condition"]) != combination:
                    raise ValueError("model/prompt order differs from the seeded fixture allocation")
    return {"valid": True, "live_ready": False, "counts": {"collection": 54, "smoke": 9},
            "fixtures": len(fixtures), "plan_hash": manifest["seal_hash"], "pending": manifest["pending"],
            "blockers": manifest["blockers"], "packet_length_audit": manifest["packet_length_audit"]}
