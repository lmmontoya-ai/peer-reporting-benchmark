"""Sealed candidate study and whole world/lane groups for level Q."""

from collections import Counter, defaultdict

from ..events import content_hash
from ..peer_reporting.storage import seal
from .bundle import load_bundle
from .collection import ORDER_VERSION, assignment_identity
from .lanes import lane_id, validate_caps_record
from .queue_fixture import build_fixture
from .queue_protocol import QUEUE_FIELDS, QUEUE_LANES, fixture_cells, validate_protocol
from .queue_text import build_instructions
from .schemas import TOOL_SCHEMA_VERSION_Q


def world(row):
    return [row["template_id"], row["block"]]


def order_rows(rows, seed, phase):
    groups = defaultdict(list)
    for row in rows:
        groups[(lane_id(row["model"], row["effort"]), tuple(world(row)))].append(row)
    result = []
    for model, effort in QUEUE_LANES:
        lane = lane_id(model, effort)
        keys = sorted((key for key in groups if key[0] == lane), key=lambda key:
                      content_hash([ORDER_VERSION, seed, phase, list(key[1]), lane]))
        for key in keys:
            result.extend(sorted(groups[key], key=lambda row: content_hash(row["fixture_id"])))
    return [{**row, "round": 0, "planned_order": index} for index, row in enumerate(result)]


def verify_order(rows, seed, phase):
    identities = Counter((row["fixture_id"], row["model"], row["effort"]) for row in rows)
    if any(count != 1 for count in identities.values()):
        raise ValueError("queue order must run every fixture-lane once")
    if rows != order_rows(rows, seed, phase):
        raise ValueError("queue groups must run whole and consecutively in the declared hash order")


def study(protocol, templates, caps_record, *, instance_nonce):
    allocation = validate_protocol(protocol)
    caps = validate_caps_record(caps_record)
    if "trial_input_token_stop" not in caps["trial"]:
        raise ValueError("queue study requires queue caps")
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    tools, fixtures, assignments, generated = bundle.tool_manifest(), {}, [], {}
    for phase in ("compatibility", "smoke", "screening", "gate", "main"):
        rows = []
        for arm, definition in protocol["arms"].items():
            if definition["phase"] != phase:
                continue
            split = definition["split"]
            for model, effort in QUEUE_LANES:
                for cell in fixture_cells(arm, effort=effort):
                    template = templates[cell["template_id"]]
                    key = content_hash([split, protocol["seeds"][split], cell])
                    if key not in generated:
                        generated[key] = build_fixture(template, split=split, seed=protocol["seeds"][split],
                            **{key: value for key, value in cell.items() if key not in ("template_id", "level")})
                    fixture = generated[key]
                    fixtures[fixture["fixture_id"]] = fixture
                    row = {key: fixture["parameters"].get(key) for key in
                           ("split", "template_id", "level", "variant", "near_miss_type", *QUEUE_FIELDS)}
                    row.update(phase=phase, arm=arm, model=model, effort=effort, prompt_condition="neutral",
                               world_mode="normal", fixture_id=fixture["fixture_id"],
                               instructions=build_instructions(template, caps["trial"]))
                    row["arm_memberships"] = [arm]
                    if (arm == "queue_main" and row["block"] <= 20 and row["variant"] == "violation"
                            and row["pressure"] == "neutral"):
                        row["arm_memberships"].append("queue_attention")
                    row["assignment_id"] = assignment_identity(row, protocol_id=protocol["protocol_id"],
                                                               tool_manifest_hash=content_hash(tools),
                                                               caps_hash=content_hash(caps))
                    rows.append(row)
        seed = protocol["seeds"]["smoke" if phase in ("compatibility", "smoke") else "calibration"]
        ordered = order_rows(rows, seed, phase)
        verify_order(ordered, seed, phase)
        assignments.extend(ordered)
    counts = dict(Counter(row["arm"] for row in assignments))
    if counts != allocation["counts"]:
        raise ValueError("queue assignments differ from the specified cells")
    return seal({"kind": "peer_reporting_v11_study", "protocol_id": protocol["protocol_id"],
                 "instance_nonce": instance_nonce, "protocol": protocol, "caps": caps,
                 "caps_hash": content_hash(caps), "tool_manifest": tools, "tool_manifest_hash": content_hash(tools),
                 "runtime_options": protocol["runtime_options"],
                 "fixtures": {key: {"path": f"fixtures/{key}.json", "content_hash": content_hash(value)}
                              for key, value in sorted(fixtures.items())},
                 "assignments": assignments, "counts": counts,
                 "split_counts": dict(Counter(row["split"] for row in assignments)),
                 "total_trials": len(assignments), "order_generator": ORDER_VERSION, "live_model_calls": 0}), fixtures
