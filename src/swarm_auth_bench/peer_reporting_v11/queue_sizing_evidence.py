"""Authenticate frozen Q sizing sources without importing scoring or review code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..events import content_hash
from ..peer_reporting.storage import read_sealed, safe_child
from .queue_sizing import SUBMISSION_KIND, check_sizing_record, require, sizing_record_hash


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
    require(submission["user_type"] == "human" and type(submission["user_id"]) is str
            and bool(submission["user_id"].strip()),
            "sizing choices and approval require an identified human user")
    require(submission["study_manifest_hash"] == manifest["seal_hash"]
            and submission["screening_export_hash"] == export_hash,
            "user submission names another study or screening export")
    return submission


def _submitted_bytes(validation, path, primary_directory):
    path = Path(path).resolve()
    submission = Path(validation["submission_path"])
    require(submission == path.with_name(path.stem + "-submission.json"),
            "retained submission path differs from its validation record")
    primary = Path(primary_directory) / (validation["submission_sha256"] + ".json")
    require(primary.is_file() and submission.is_file(), "retained primary submission is missing")
    raw = submission.read_bytes()
    require(primary.read_bytes() == raw and hashlib.sha256(raw).hexdigest() == validation["submission_sha256"],
            "sizing codes, choices or approval differ from retained primary submission")
    return json.loads(raw)


def verify_submission(reference, *, study_directory, manifest, export_hash):
    record = _check_reference(reference)
    require(set(record) == {"kind", "study_directory", "study_manifest_hash", "submission_path", "submission_sha256",
                            "submission", "seal_hash"}, "retained sizing validation fields differ")
    require(record["kind"] == "peer_reporting_v14_queue_retained_sizing_submission"
            and record["study_directory"] == str(Path(study_directory).resolve())
            and record["study_manifest_hash"] == manifest["seal_hash"],
            "retained sizing submission belongs to another study")
    submitted = _submitted_bytes(record, reference["path"], Path(study_directory) / "sizing-submissions")
    require(submitted == record["submission"], "sizing validation differs from retained submission")
    return validate_submission(submitted, manifest=manifest, export_hash=export_hash)


def credibility_inventory(directory, packet_index, uploads, adjudications):
    """Require every retained submission for P3 packets, including disagreements."""
    identifiers = {row["assignment_id"] for row in packet_index["selection"]["rows"]
                   if "screening_credibility" in row["reasons"]}
    hashes = {row["review_packet_hash"] for row in packet_index["rows"] if row["assignment_id"] in identifiers}
    require(len(identifiers) == len(hashes) == 8, "all eight P3 credibility packets are required")
    primary = set()
    for path in (Path(directory) / "submissions").glob("*.json"):
        raw = path.read_bytes()
        if json.loads(raw)["review_packet_hash"] in hashes:
            require(hashlib.sha256(raw).hexdigest() == path.stem, "credibility primary submission hash differs")
            primary.add(path.stem)
    relevant = []
    for path in (*uploads, *adjudications):
        retained = read_sealed(Path(path))
        if retained["review_packet_hash"] in hashes:
            relevant.append(retained["submission_sha256"])
    require(set(relevant) == primary and len(relevant) == len(primary),
            "credibility must include every retained P3 episode submission exactly once")


def _check_review_sources(resolution):
    directory = Path(resolution["packet_directory"])
    index = read_sealed(directory / "index.json")
    require(index["part"] == "owner_communications" and index["source_export_hash"] == resolution["source_export_hash"],
            "owner coding packet source differs")
    packets = {row["review_packet_id"]: row for row in index["rows"]}
    inventory = resolution["submission_inventory"]
    require(read_sealed(directory / "resolution-inventories" / (inventory["seal_hash"] + ".json")) == inventory,
            "owner coding submission inventory differs from its retained version")
    retained = {}
    for entry in inventory["submissions"]:
        validation = read_sealed(Path(entry["path"]))
        require(validation["packet_directory"] == str(directory)
                and validation["submission_sha256"] == entry["submission_sha256"]
                and validation["review_packet_id"] in packets
                and validation["review_packet_hash"] == packets[validation["review_packet_id"]]["review_packet_hash"],
                "owner coding retained submission binding differs")
        upload = _submitted_bytes(validation, entry["path"], directory / "submissions")
        require(upload["review_packet_hash"] == validation["review_packet_hash"],
                "owner coding submission packet hash differs")
        retained.setdefault(validation["review_packet_id"], []).append((entry["role"], upload))
    for name, codes in resolution["resolutions"].items():
        submissions = retained.get(name, [])
        initial = [upload for role, upload in submissions if role == "upload" and upload["status"] == "final"]
        adjudications = [upload for role, upload in submissions if role == "adjudication"]
        corrections = [upload for role, upload in submissions if role == "correction"]
        require(codes["initial_reviews"] == initial and codes.get("corrections", []) == corrections,
                "owner coding differs from retained human codes")
        final = corrections[-1] if corrections else adjudications[0] if adjudications else None
        adjudication = ({key: value for key, value in final.items() if key != "supersedes_resolution_hash"}
                        if final is not None else None)
        require(len(adjudications) <= 1 and codes["adjudication"] == adjudication,
                "owner coding adjudication differs from retained human codes")
        if codes["status"] == "final":
            require(bool(initial) and codes["labels"] == (final or initial[0])["labels"]
                    and (final is not None or all(upload["labels"] == initial[0]["labels"] for upload in initial)),
                    "owner coding final labels differ from retained human codes")
    return {row["assignment_id"]: resolution["resolutions"][row["review_packet_id"]] for row in index["rows"]}


def verify_execution_sizing(record, *, study_directory, receipt_directory, compatibility_directories=(),
                            repair_evidence_directory=None):
    """Bind approved arithmetic to retained human bytes and receipt-bound primary evidence.

    Offline builders rederive scores. Execution authenticates that frozen result through
    the whole derived record hash in the primary approval and checks its cited source bindings.
    """
    from . import live
    from .receipts import check_receipt

    manifest = live.read_study_manifest(study_directory)
    sources = record["sources"]
    require(sources["study_directory"] == str(Path(study_directory).resolve()),
            "sizing evidence belongs to another study")
    submitted = verify_submission(record["retained_submission"], study_directory=study_directory,
                                  manifest=manifest, export_hash=record["screening_export_hash"])
    require(submitted == record["submission"] and submitted["approval"]["sizing_record_hash"] == sizing_record_hash(record),
            "sizing record or submission differ from retained human approval")
    check_sizing_record(record, study_manifest_hash=manifest["seal_hash"])
    export = _check_reference(sources["screening_export"])
    resolution = _check_reference(sources["coding_resolution"])
    packets = _check_reference(sources["episode_packets"])
    require(export["seal_hash"] == record["screening_export_hash"] and export["study_manifest"] == manifest
            and export["phase"] == "screening", "sizing screening export binding differs")
    require(resolution["source_export_hash"] == packets["source_export_hash"] == export["seal_hash"]
            and packets["coding_resolution"] == sources["coding_resolution"]["path"],
            "sizing coding or credibility evidence binding differs")
    owner_codes = _check_review_sources(resolution)
    for row in record["screening_inputs"]:
        if row["pressure"] == "neutral":
            endpoint = resolution["endpoints"].get(row["assignment_id"], {}).get("owner_informed", {})
            codes = owner_codes.get(row["assignment_id"])
            expected_codes = codes if codes is not None else {
                "status": "not_applicable", "reason": endpoint.get("reason", row["ineligible_reason"])}
            require(row["owner_codes"] == expected_codes, "sizing codes differ from retained owner coding")
            value = (any(item["owner_informed"] is True for item in codes["labels"].values()) if codes else False
                     ) if row["eligible"] and row["reached"] else None
            require(row["owner_informed"] == endpoint.get("value") == value,
                    "sizing endpoint differs from retained owner coding")
    directory = Path(sources["episode_packets"]["path"]).parent
    reviewed, adjudicated = {}, {}
    for reference in (*sources["episode_uploads"], *sources["episode_adjudications"]):
        retained = _check_reference(reference)
        require(retained["packet_directory"] == str(directory), "credibility submission packet directory differs")
        upload = _submitted_bytes(retained, reference["path"], directory / "submissions")
        require(upload["review_packet_hash"] == retained["review_packet_hash"], "credibility submission packet hash differs")
        name = retained["review_packet_id"]
        if retained["adjudication"]:
            require(name not in adjudicated, "duplicate credibility adjudication")
            adjudicated[name] = upload
        elif upload["status"] == "final":
            reviewed.setdefault(name, []).append(upload)
    credibility_inventory(directory, packets, [row["path"] for row in sources["episode_uploads"]],
                          [row["path"] for row in sources["episode_adjudications"]])
    by_assignment = {row["assignment_id"]: row for row in packets["rows"]}
    for row in record["screening_inputs"]:
        if row["pressure"] == "P3":
            require(row["assignment_id"] in by_assignment, "P3 credibility packet is missing")
            packet = by_assignment[row["assignment_id"]]
            name, initial = packet["review_packet_id"], reviewed.get(packet["review_packet_id"], [])
            final = adjudicated.get(name) or (initial[0] if initial else None)
            require(final is not None and final["status"] == "final" and (name in adjudicated
                    or all(upload["labels"] == final["labels"] for upload in initial)),
                    "P3 credibility episode review is incomplete")
            require(row["credibility_review"] == {key: final["labels"][key] for key in (
                "test_awareness", "scripting_remarks")}, "sizing credibility differs from retained human codes")
    root_hashes = {export["source_plan_hash"], *(row.get("source_root_plan_hash", export["source_plan_hash"])
                                              for row in export["rows"])}
    registrations = {row["plan_hash"]: row for row in live.registered_roots(study_directory)}
    roots = {}
    for root_hash in sorted(root_hashes):
        require(root_hash in registrations, "sizing export has no registered primary root")
        root = live.registered_root_path(study_directory, registrations[root_hash])
        roots[root_hash] = root, live.read_live_plan(root)
        check_receipt(root, receipt_directory, study_directory=study_directory)
        live.verify_live_root(root, study_directory=study_directory, receipt_directory=receipt_directory,
                              compatibility_directories=compatibility_directories,
                              repair_evidence_directory=repair_evidence_directory)
    export_directory = Path(sources["screening_export"]["path"]).parent
    for row in export["rows"]:
        if row.get("attempt_path") is not None:
            attempt = read_sealed(safe_child(export_directory, row["attempt_path"]))
            require(content_hash({key: value for key, value in attempt.items() if key != "seal_hash"}) == row["attempt_hash"],
                    "sizing exported attempt differs from its screening export")
            root, plan = roots[row.get("source_root_plan_hash", export["source_plan_hash"])]
            lane = next(lane for lane in plan["lanes"] if lane["lane_id"] == attempt["lane_id"])
            primary = read_sealed(safe_child(root, lane["path"] + "/attempts/" + attempt["attempt_id"] + "/attempt.json"))
            require(content_hash({key: value for key, value in primary.items() if key != "seal_hash"})
                    == attempt["source_attempt_hash"], "sizing exported attempt primary binding differs")
    return record
