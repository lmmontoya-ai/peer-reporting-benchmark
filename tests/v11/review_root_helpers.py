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
        entries = [{**{key: value for key, value in row.items() if key not in ("assignment_id", "effort")},
                    "entry_id": row["assignment_id"], "reasoning_effort": row["effort"]} for row in rows]
        plan = seal({"phase": "calibration", "planned_order": entries})
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


def bind_review_root_observations(monkeypatch, root, plan, rows):
    """Inject primary archive observations for plan-only unit doubles, never export rows."""
    from copy import deepcopy
    from pathlib import Path
    from swarm_auth_bench.peer_reporting_v11 import live_review

    def patch(name, primary, position=0):
        original = getattr(live_review, name)
        def call(*args, **kwargs):
            if Path(args[position]).resolve() == root.resolve():
                return primary(*args, **kwargs)
            return original(*args, **kwargs)
        monkeypatch.setattr(live_review, name, call)

    patch("check_abandoned_root", lambda *a, **k: None)
    patch("lane_journals", lambda *a, **k: {lane["lane_id"]: [] for lane in plan["lanes"]})
    patch("check_start_claims", lambda *a, **k: [], position=2)
    patch("verify_consumed_ledger", lambda *a, **k: [])
    def inspect(*args, scorer=None, **kwargs):
        observations = deepcopy(rows())
        if scorer is not None:
            for row in observations:
                if row["attempt"] is not None and row["score"] is not None:
                    row["score"] = scorer(row["attempt"])
        return {"phase": "calibration", "plan_hash": plan["seal_hash"], "amendment_errors": [], "lane_errors": [],
                "authorization_evidence": {}, "status_counts": {"archived": len(observations)},
                "verified_model_observations": 0, "planned_count": len(observations), "rows": observations}
    patch("inspect_live_root", inspect)
