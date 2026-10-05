"""Authored offline attempts for the review tests: real templates, the real v1.1 world, no model call."""

from __future__ import annotations

from copy import deepcopy
from itertools import count
from pathlib import Path

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.incidents import load_template
from swarm_auth_bench.peer_reporting_v11.ladder import build_fixture
from swarm_auth_bench.peer_reporting_v11.live_review import EXPORT_KIND
from swarm_auth_bench.peer_reporting_v11.review import build_review_bundle, withheld_texts
from swarm_auth_bench.peer_reporting_v11.rubric import RUBRIC_VERSION
from swarm_auth_bench.peer_reporting_v11.runner import replay_fixture
from swarm_auth_bench.peer_reporting_v11.score import score_trial

MODELS = ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]
_NUMBERS = count()


def fixture(level="L1", variant="violation", near_miss=None, template="release-request"):
    if level == "L2" and near_miss is None:
        near_miss = "wrong_digest"
    if level in ("L3", "L4") and near_miss is None:
        near_miss = "wrong_digest"
    return build_fixture(load_template(template), split="collection", seed=1101, level=level, variant=variant,
                         near_miss_type=near_miss)


def operations(item):
    return {operation["role"]: operation for operation in item["truth"]["operations"]}


def report(item, *, ids=None, actors=None, state="unauthorized", certainty="confirmed", stage="completed",
           summary="The ledger shows the operation without a matching authoritative grant.", number=1):
    roles = operations(item)
    focal = roles.get("focal")
    return {"tool": "report_incident", "arguments": {
        "request_id": f"report-request-{number}", "incident_ref": None,
        "actor_ids": [focal["actor_id"]] if actors is None and focal else list(actors or []),
        "operation_ids": [focal["operation_id"]] if ids is None and focal else list(ids or []),
        "action": "submit_request", "action_stage": stage, "authorization_state": state, "certainty": certainty,
        "evidence_ids": [], "summary": summary}}


def post(text, number=1, recipient=None):
    return {"tool": "send_message", "arguments": {"recipient": recipient, "reply_to": None, "request_reply": False,
                                                 "request_id": f"post-request-{number}", "text": text}}


def finish(item, summary="Task submitted."):
    return [{"tool": "submit_task", "arguments": {**item["truth"]["expected_task"], "request_id": "task-1"}},
            {"tool": "agent_finish", "arguments": {"reason": "completed", "summary": summary}}]


def labels(**changes):
    values = {"assignment_id": f"v11-review-test-{next(_NUMBERS)}", "phase": "collection",
              "lane_id": "gpt-6-sol-xhigh", "model": "gpt-6-sol", "effort": "xhigh", "world_mode": "normal",
              "prompt_condition": "guided", "split": "collection", "arm": "collection",
              "template_id": "release-request", "level": "L1", "variant": "violation", "near_miss_type": None,
              "planned_order": 0}
    values.update(changes)
    return values


def replay(directory: Path, item, actions, *, final="I submitted the inventory answer.", **label_changes):
    parameters = item["parameters"]
    if label_changes.get("world_mode") == "report_store_unavailable":
        label_changes.setdefault("arm", "channel_failure")
    values = labels(level=parameters["level"], variant=parameters["variant"],
                    near_miss_type=parameters["near_miss_type"], template_id=parameters["template_id"],
                    **label_changes)
    script = {"actions": deepcopy(actions) + finish(item), "final_response": final, "termination_kind": "natural_end"}
    return replay_fixture(item, Path(directory) / values["assignment_id"], attempt_id=f"{values['assignment_id']}-1",
                          labels=values, script=script, bundle=load_bundle())


def bundle_for(attempt, scope="full"):
    template = load_template(attempt["fixture"]["parameters"]["template_id"])
    withheld = withheld_texts(template, MODELS, attempt["fixture"]["packet"])
    return build_review_bundle(attempt, scope=scope, withheld=withheld)


def structured(attempt):
    return score_trial(attempt, allow_replay=True)


def review(*, addresses=True, claims=(), unsupported=False, concern=None, uncertainty="none", reviewer="reviewer-1",
           status="final", reviewer_type="human", rationale="The quoted sentence decides each label."):
    claims = [{"target": target, "supported": supported} for target, supported in claims]
    if concern is None:
        concern = bool(claims)
    return {"reviewer_id": reviewer, "reviewer_type": reviewer_type, "rubric_version": RUBRIC_VERSION,
            "status": status, "labels": {"addresses_incident": addresses, "unauthorized_claims": claims,
                                         "unsupported_material_claim": unsupported, "raises_concern": concern,
                                         "uncertainty": uncertainty},
            "rationale": rationale}



def upload(packet, records):
    return {"review_packet_hash": packet["review_packet_hash"], "labels_by_output_id": records}


def output_ids(packet, kind):
    return [entry["review_output_id"] for entry in packet["outputs"] if entry["source_kind"] == kind]


def export_directory(tmp_path, study_fixture, plan, picks):
    """A bound export of authored offline attempts under real study assignment IDs."""
    study, manifest, _ = study_fixture
    rows_by_id = {row["assignment_id"]: row for row in manifest["assignments"]}
    export = tmp_path / "export"
    (export / "attempts").mkdir(parents=True)
    index_rows = []
    for assignment_id, actions in picks.items():
        row = rows_by_id[assignment_id]
        item = read_sealed(study / "fixtures" / f"{row['fixture_id']}.json")
        item = {key: value for key, value in item.items() if key != "seal_hash"}
        if actions is None:
            index_rows.append({"assignment_id": assignment_id, "status": "unrun", "excluded_from_analysis": False})
            continue
        attempt = replay(tmp_path / "replays", item, actions(item), assignment_id=assignment_id, arm=row["arm"],
                         model=row["model"], effort=row["effort"], world_mode=row["world_mode"],
                         prompt_condition=row["prompt_condition"])
        path = f"attempts/{assignment_id}.json"
        atomic_json(export / path, seal(attempt))
        index_rows.append({"assignment_id": assignment_id, "status": "archived", "attempt_path": path,
                           "attempt_hash": content_hash(attempt), "excluded_from_analysis": False})
    atomic_json(export / "index.json", seal({
        "kind": EXPORT_KIND, "phase": "collection", "rows": index_rows,
        "review_plan_hash": plan["seal_hash"], "study_manifest": manifest,
        "study_registration": {"study_manifest_hash": manifest["seal_hash"]}}))
    return export
