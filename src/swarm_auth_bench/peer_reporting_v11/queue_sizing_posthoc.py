"""Receipt-bound screening inputs and retained user sizing submissions, offline only."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from ..peer_reporting.config import read_json
from ..peer_reporting.storage import read_sealed, seal
from . import queue_posthoc
from .lanes import lane_id
from .queue_review import resolve_reviews
from .queue_sizing import (
    SUBMISSION_KIND,
    check_sizing_record,
    require,
    screening_measures,
    sizing_decision,
)
from .queue_storage import atomic_json
from .structured import test_awareness_flag


def _reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "seal_hash": read_sealed(path)["seal_hash"]}


def _check_reference(reference):
    require(type(reference) is dict and set(reference) == {"path", "seal_hash"}
            and Path(reference["path"]).is_absolute(), "invalid sizing evidence reference")
    record = read_sealed(Path(reference["path"]))
    require(record["seal_hash"] == reference["seal_hash"], "sizing evidence reference hash differs")
    return record


def validate_submission(submission, *, manifest, export_hash):
    fields = {"kind", "study_manifest_hash", "screening_export_hash", "user_id", "user_type",
              "flag_resolutions", "choices", "W", "prices_per_million", "approval"}
    require(type(submission) is dict and set(submission) == fields and submission["kind"] == SUBMISSION_KIND,
            "invalid user sizing submission fields")
    require(submission["user_type"] == "human" and type(submission["user_id"]) is str and bool(submission["user_id"].strip()),
            "sizing choices and approval require an identified human user")
    require(submission["study_manifest_hash"] == manifest["seal_hash"]
            and submission["screening_export_hash"] == export_hash, "user submission names another study or screening export")
    return submission


def retain_submission(submission_path, output, *, study_directory):
    """Keep exact submitted bytes beside validation and in an independent primary copy."""
    from .live import read_study_manifest

    manifest = read_study_manifest(study_directory)
    raw = Path(submission_path).read_bytes()
    submitted = json.loads(raw)
    validate_submission(submitted, manifest=manifest, export_hash=submitted["screening_export_hash"])
    output = Path(output).resolve()
    companion = output.with_name(output.stem + "-submission.json")
    require(not output.exists() and not companion.exists(), "retained sizing submission output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = queue_posthoc.retain_submission_bytes(raw, companion, Path(study_directory).resolve() / "sizing-submissions")
    record = seal({"kind": "peer_reporting_v14_queue_retained_sizing_submission",
                   "study_directory": str(Path(study_directory).resolve()), "study_manifest_hash": manifest["seal_hash"],
                   "submission_path": str(companion), "submission_sha256": digest, "submission": submitted})
    atomic_json(output, record)
    return _reference(output)


def verify_submission(reference, *, study_directory, manifest, export_hash):
    record = _check_reference(reference)
    require(set(record) == {"kind", "study_directory", "study_manifest_hash", "submission_path", "submission_sha256",
                            "submission", "seal_hash"}, "retained sizing validation fields differ")
    require(record["kind"] == "peer_reporting_v14_queue_retained_sizing_submission"
            and record["study_directory"] == str(Path(study_directory).resolve())
            and record["study_manifest_hash"] == manifest["seal_hash"], "retained sizing submission belongs to another study")
    path = Path(record["submission_path"])
    validation = Path(reference["path"])
    require(path == validation.with_name(validation.stem + "-submission.json"), "retained sizing submission path differs")
    primary = Path(study_directory) / "sizing-submissions" / (record["submission_sha256"] + ".json")
    require(primary.is_file() and path.is_file() and primary.read_bytes() == path.read_bytes()
            and hashlib.sha256(path.read_bytes()).hexdigest() == record["submission_sha256"],
            "sizing choices or approval differ from retained primary submission")
    submitted = read_json(path)
    require(submitted == record["submission"], "sizing validation differs from retained submission")
    return validate_submission(submitted, manifest=manifest, export_hash=export_hash)


def _censored_caps(attempt):
    mapping = {"tool_request_limit": "max_tool_requests_per_trial", "trial_wall_limit": "max_trial_wall_seconds",
               "trial_input_token_limit": "trial_input_token_stop", "trial_output_token_limit": "trial_output_token_stop"}
    reasons = []
    if attempt["eligibility_inputs"]["cap_ended"]:
        reasons.append(attempt["boundary"]["reason"])
        reasons.extend(event["reason"] for event in attempt.get("generation_stops", [])
                       if event["generation_active"] and event["termination_kind"] == "per_trial_limit")
        # One response can cross both stops. The second notification can follow
        # the first stop's cancellation, but both statistics are censored.
        reasons.extend("trial_" + event["token_kind"] + "_token_limit" for event in attempt["eligibility_inputs"].get("token_stops", []))
    return sorted({mapping[reason] for reason in reasons if reason in mapping})


def screening_inputs(*, export_directory, study_directory, coding_resolution, episode_packets,
                     episode_uploads=(), episode_adjudications=(), **verification):
    """P3 reporting endpoints are deliberately absent from the sizing projection."""
    cache = queue_posthoc._verification_cache(None)
    resolution = queue_posthoc.verify_resolution(coding_resolution, _cache=cache, **verification)
    coding_context = queue_posthoc._verified_packets(resolution["packet_directory"], cache, **verification)
    require(coding_context["index"]["export_directory"] == str(Path(export_directory).resolve())
            and coding_context["index"]["study_directory"] == str(Path(study_directory).resolve()),
            "owner coding belongs to another screening export or study")
    data = coding_context["data"]
    index = data["index"]
    require(index["phase"] == "screening", "sizing requires a screening export")
    manifest = index["study_manifest"]
    planned = [row for row in manifest["assignments"] if row["phase"] == "screening"]
    require({row["assignment_id"] for row in index["rows"]} == {row["assignment_id"] for row in planned},
            "screening export omits planned assignments")
    require(resolution["source_export_hash"] == index["seal_hash"], "owner coding belongs to another screening export")
    owner_codes = {bundle["row"]["assignment_id"]: deepcopy(resolution["resolutions"][name])
                   for name, bundle in coding_context["bundles"].items()}
    context = queue_posthoc._verified_packets(episode_packets, cache, **verification)
    require(context["index"]["part"] == "episode" and context["index"]["source_export_hash"] == index["seal_hash"],
            "credibility requires episode packets from this screening export")
    require(context["index"]["coding_resolution"] == str(Path(coding_resolution).resolve()),
            "screening episode packets must use the supplied complete owner coding")
    context["directory"] = Path(episode_packets).resolve()
    uploads, adjudications = {}, {}
    for path in episode_uploads:
        name, uploaded, adjudication, correction = queue_posthoc._checked_upload(path, context, cache)
        require(not adjudication and correction is None, "credibility initial upload cannot be an adjudication or correction")
        uploads.setdefault(name, []).append(uploaded)
    for path in episode_adjudications:
        name, uploaded, adjudication, correction = queue_posthoc._checked_upload(path, context, cache)
        require(adjudication and correction is None and name not in adjudications, "invalid credibility adjudication")
        adjudications[name] = uploaded
    # All retained submissions for a reviewed episode must be included, even if disputed.
    included = {read_sealed(Path(path))["submission_sha256"] for path in (*episode_uploads, *episode_adjudications)}
    primary = {path.stem for path in (context["directory"] / "submissions").glob("*.json")}
    require(included == primary and len(included) == len(episode_uploads) + len(episode_adjudications),
            "credibility must include every retained episode submission exactly once")
    credibility = {}
    p3_ids = {row["assignment_id"] for row in planned if row["pressure"] == "P3"}
    for name, bundle in context["bundles"].items():
        identifier = bundle["row"]["assignment_id"]
        if identifier not in p3_ids:
            continue
        reviewed = resolve_reviews(bundle["packet"], uploads.get(name, []), second_review=False,
                                   adjudication=adjudications.get(name))
        require(reviewed["status"] == "final", "P3 credibility episode review is incomplete")
        credibility[identifier] = {key: reviewed["labels"][key] for key in ("test_awareness", "scripting_remarks")}
    require(set(credibility) == p3_ids, "all eight P3 credibility episodes need complete review")
    scores = {score["assignment_id"]: score for score in data["scores"]}
    rows = []
    for assignment in sorted(planned, key=lambda row: row["assignment_id"]):
        identifier = assignment["assignment_id"]
        score = scores.get(identifier)
        eligible = score is not None and score["eligibility"]["eligible"]
        neutral = assignment["pressure"] == "neutral"
        codes = owner_codes.get(identifier)
        endpoint = resolution["endpoints"].get(identifier, {}).get("owner_informed", {})
        complete = codes is None or codes["status"] == "final"
        if neutral:
            require(complete, "neutral screening owner coding is incomplete")
        row = {"assignment_id": identifier, "lane": lane_id(assignment["model"], assignment["effort"]),
               "pressure": assignment["pressure"], "eligible": eligible,
               "ineligible_reason": score["eligibility"]["reason"] if score is not None else "missing",
               "test_awareness_flag": score is not None and any(test_awareness_flag(output["text"])
                   for output in data["attempts"][identifier]["observer_outputs"]),
               "usage": {key: score["usage"].get(key) for key in ("tool_requests", "generation_seconds", "input_tokens",
                         "uncached_input_tokens", "output_tokens")} if score is not None else {},
               "censored_caps": _censored_caps(data["attempts"][identifier]) if score is not None else [],
               "credibility_review": credibility.get(identifier)}
        if neutral:
            row.update(reached=score["reached"] if score is not None else False,
                       focal_engaged=score["endpoints"]["focal_engaged"]["value"] if score is not None else None,
                       item_accuracy=score["endpoints"]["item_accuracy"]["value"] if score is not None else None,
                       owner_informed=endpoint.get("value"), coding_complete=complete,
                       owner_codes=codes if codes is not None else {"status": "not_applicable", "reason":
                           endpoint.get("reason", row["ineligible_reason"])},
                       mechanical_owner_informed=score["endpoints"]["owner_informed"]["value"] if score is not None else None)
        rows.append(row)
    sources = {"study_directory": str(Path(study_directory).resolve()),
               "screening_export": _reference(Path(export_directory) / "index.json"),
               "coding_resolution": _reference(coding_resolution), "episode_packets": _reference(Path(episode_packets) / "index.json"),
               "episode_uploads": [_reference(path) for path in episode_uploads],
               "episode_adjudications": [_reference(path) for path in episode_adjudications]}
    return rows, manifest, sources


def build_screening_measures(*, submission, **options):
    rows, manifest, _ = screening_inputs(**options)
    validate_submission(submission, manifest=manifest, export_hash=rows_export_hash(options["export_directory"]))
    return screening_measures(rows, manifest["caps"], submission)


def rows_export_hash(directory):
    return read_sealed(Path(directory) / "index.json")["seal_hash"]


def build_sizing_record(*, retained_submission, **options):
    rows, manifest, sources = screening_inputs(**options)
    submission = verify_submission(retained_submission, study_directory=options["study_directory"], manifest=manifest,
                                   export_hash=sources["screening_export"]["seal_hash"])
    return sizing_decision(inputs=rows, provisional_caps=manifest["caps"], submission=submission,
                           sources=sources, retained_submission=retained_submission)


def validate_sizing_record(record, *, study_manifest_hash, receipt_directory=None, repair_evidence_directory=None):
    check_sizing_record(record, study_manifest_hash=study_manifest_hash)
    sources = record["sources"]
    for reference in (sources["screening_export"], sources["coding_resolution"], sources["episode_packets"],
                      *sources["episode_uploads"], *sources["episode_adjudications"]):
        _check_reference(reference)
    expected = build_sizing_record(retained_submission=record["retained_submission"],
        export_directory=Path(sources["screening_export"]["path"]).parent, study_directory=Path(sources["study_directory"]),
        coding_resolution=Path(sources["coding_resolution"]["path"]), episode_packets=Path(sources["episode_packets"]["path"]).parent,
        episode_uploads=[Path(row["path"]) for row in sources["episode_uploads"]],
        episode_adjudications=[Path(row["path"]) for row in sources["episode_adjudications"]],
        receipt_directory=receipt_directory, repair_evidence_directory=repair_evidence_directory)
    require(record == expected, "sizing record differs from receipt-bound screening evidence or retained submissions")
    return check_sizing_record(record, study_manifest_hash=study_manifest_hash)
