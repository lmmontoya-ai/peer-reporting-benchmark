"""Register sealed offline plan doubles for review population regressions."""

from collections import defaultdict

from swarm_auth_bench.peer_reporting.storage import atomic_json, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.config import PROTOCOL_ID
from swarm_auth_bench.peer_reporting_v11.lanes import lane_id
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P


def register_review_root(study, manifest, assignments, name):
    root = study / "roots" / name
    groups = defaultdict(list)
    for assignment in assignments:
        groups[lane_id(assignment["model"], assignment["effort"])].append(assignment)
    lanes = []
    for identifier, rows in sorted(groups.items()):
        relative = f"lanes/{identifier}"
        plan = seal({"phase": "calibration", "planned_order": rows})
        (root / relative).mkdir(parents=True)
        atomic_json(root / relative / "phase-plan.json", plan)
        lanes.append({"lane_id": identifier, "path": relative, "plan_hash": plan["seal_hash"]})
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_P)
    plan = seal({"kind": live.TOP_PLAN_KIND, "protocol_id": PROTOCOL_ID, "phase": "calibration", "lanes": lanes,
        "source": {"study_manifest_hash": manifest["seal_hash"]}, "maximum_live_calls": len(assignments),
        "root_instance_nonce": name, "tool_schema_version": bundle.schema_version,
        "tool_manifest_hash": bundle.tool_manifest_hash, "tool_descriptors_hash": bundle.tool_descriptors_hash,
        "wire_tool_specs_hash": bundle.wire_tool_specs_hash})
    atomic_json(root / live.LIVE_PLAN_FILE, plan)
    (study / live.STUDY_REGISTRY).mkdir(exist_ok=True)
    atomic_json(study / live.STUDY_REGISTRY / f"{plan['seal_hash']}.json", seal({
        "kind": live.REGISTRY_KIND, "plan_hash": plan["seal_hash"], "phase": "calibration",
        "study_manifest_hash": manifest["seal_hash"], "root_path": f"roots/{name}"}))
    return root, plan, {"plan_hash": plan["seal_hash"], "phase": "calibration",
                       "study_manifest_hash": manifest["seal_hash"], "root_path": f"roots/{name}"}
