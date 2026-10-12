"""Verified Q export-to-score-to-review route and retained human submissions."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from ..events import content_hash
from ..peer_reporting.config import read_json
from ..peer_reporting.storage import check_seal, read_sealed, safe_child, seal
from .queue_review import (
    adjudicated_endpoints,
    agreement,
    build_coding_packet,
    build_episode_packet,
    coding_selection,
    episode_selection,
    render_packet_html,
    require,
    resolve_reviews,
    validate_upload,
)
from .queue_score import score_trial
from .queue_storage import atomic_json
from .schemas import TOOL_SCHEMA_VERSION_Q


def plain(value):
    return {key: child for key, child in value.items() if key != "seal_hash"}


def export_coding_selection(attempts):
    """Callback injected into archive export, keeping review imports out of execution."""
    return coding_selection([score_trial(attempt) for attempt in attempts])


def export_review(directory, output, **options):
    from .live_review import export_live_review

    return export_live_review(directory, output, queue_review_selector=export_coding_selection, **options)


def verify_registry_snapshot(snapshot, current, required_roots):
    """Registry copies describe export time; later phases do not invalidate an old export."""
    require(type(snapshot) is dict and set(snapshot) == set(current), "Q export registry snapshot fields differ")
    for key in ("study_manifest_hash", "instance_nonce"):
        require(snapshot[key] == current[key], "Q export registry snapshot differs from its study")
    actual = {row["plan_hash"]: row for row in current["roots"]}
    copied = {row["plan_hash"]: row for row in snapshot["roots"]}
    require(len(copied) == len(snapshot["roots"]) and set(copied) <= set(actual) and required_roots <= set(copied),
            "Q export registry snapshot omits a source root or names an unknown root")
    for identifier, row in copied.items():
        primary = actual[identifier]
        require(set(row) == set(primary) and {key: value for key, value in row.items() if key not in (
            "state", "finalized", "abandoned")} == {key: value for key, value in primary.items() if key not in (
                "state", "finalized", "abandoned")}, "Q export registry registration differs from primary evidence")
        for marker in ("finalized", "abandoned"):
            require(row[marker] is None or row[marker] == primary[marker], "Q export registry marker differs from primary evidence")
        state = "abandoned" if row["abandoned"] else "finalized" if row["finalized"] else "pending"
        require(row["state"] == state, "Q export registry state differs from its retained markers")
    require(all(amendment in current["amendments"] for amendment in snapshot["amendments"]),
            "Q export registry amendment differs from retained primary evidence")


def verify_export(export_directory: Path, *, study_directory: Path,
                  receipt_directory: Path | None = None, repair_evidence_directory=None) -> dict:
    """Rebuild the entire export, including excluded/unrun rows and its seal-time sample."""
    from . import live, live_review
    from .receipts import check_receipt

    export, study = Path(export_directory), Path(study_directory)
    index = read_sealed(export / "index.json")
    require("queue_coding_selection" in index, "Q review requires an export sealed with the Q coding selector; re-export via export-review")
    require(index.get("tool_schema_version") == TOOL_SCHEMA_VERSION_Q and index.get("phase") != "compatibility",
            "Q behavioral scoring/review requires a registered behavioral export")
    manifest = live.read_study_manifest(study)
    require(index.get("study_manifest") == manifest, "Q export study manifest differs from primary evidence")
    registrations = [record for record in live.registered_roots(study) if record["plan_hash"] == index["source_plan_hash"]]
    require(len(registrations) == 1, "Q export has no unique registered root")
    root = live.registered_root_path(study, registrations[0])
    plan = live.read_live_plan(root)
    check_receipt(root, receipt_directory, study_directory=study)
    live.check_root_assignment_binding(root, plan, study)
    data, repairs = live_review.inspect_pressure_chain(root, plan, study,
        receipt_directory=receipt_directory, repair_evidence_directory=repair_evidence_directory)
    visible, attempts, timelines = [], {}, {}
    root_paths = {record["plan_hash"]: live.registered_root_path(study, record) for record in live.registered_roots(study)}
    for row in data["rows"]:
        exported_row = {key: value for key, value in row.items() if key != "attempt"}
        if row["attempt"] is not None:
            attempt = deepcopy(row["attempt"])
            attempt.update(behavioral_observation=True, count_in_any_behavioral_denominator=True)
            identifier = row["assignment_id"]
            path = f"attempts/{identifier}.json"
            require(plain(read_sealed(safe_child(export, path))) == attempt,
                    f"{identifier}: Q exported attempt differs from primary evidence")
            exported_row.update(attempt_path=path, attempt_hash=content_hash(attempt))
            attempts[identifier] = attempt
            source_root = root_paths[row.get("source_root_plan_hash", plan["seal_hash"])]
            source_plan = live.read_live_plan(source_root)
            lane = next(lane for lane in source_plan["lanes"] if lane["lane_id"] == attempt["lane_id"])
            archive = read_sealed(safe_child(source_root, lane["path"] + "/attempts/" + attempt["attempt_id"] + "/attempt.json"))
            require(content_hash(plain(archive)) == attempt["source_attempt_hash"], "Q primary attempt hash mismatch")
            # inspect_pressure_chain has already checked this exact controller copy
            # against the durable controller journal and committed receipt.
            result = archive["observer_result"]
            timelines[identifier] = result["events"] if result is not None else []
        visible.append(exported_row)
    scores = [score_trial(attempts[identifier], controller_events=timelines[identifier]) for identifier in attempts]
    required_roots = {plan["seal_hash"], *(row.get("source_root_plan_hash", plan["seal_hash"]) for row in data["rows"]),
                      *(row["plan_hash"] for row in (plan.get("consumed_attempts") or {}).get("prior_roots", []))}
    verify_registry_snapshot(index.get("study_registry"), live.study_registry_listing(study), required_roots)
    expected = seal({"kind": live_review.EXPORT_KIND, "phase": plan["phase"], "tool_schema_version": plan["tool_schema_version"],
                     "source_plan_hash": plan["seal_hash"], "rows": visible, "scored": False,
                     "planned_count": data["planned_count"], "status_counts": data["status_counts"],
                     "behavioral_observation": True, "count_in_any_behavioral_denominator": True,
                     "runtime_options": plan["runtime_options"], "caps": plan["caps"],
                     "authorization_evidence": data["authorization_evidence"], "ledger_repairs": repairs,
                     "study_registry": index["study_registry"], "study_manifest": manifest,
                     "receipt_plan_hash": plan["seal_hash"], "live_model_calls": 0,
                     "queue_coding_selection": coding_selection(scores)})
    require(index == expected, "Q export index or seal-time coding selection differs from primary evidence")
    return {"index": index, "attempts": attempts, "scores": scores, "timelines": timelines}


def _source(export_directory, study_directory):
    return {"export_directory": str(Path(export_directory).resolve()),
            "study_directory": str(Path(study_directory).resolve())}


def _score_record(data, source):
    return seal({"kind": "peer_reporting_v14_queue_scores", **source,
                 "source_export_hash": data["index"]["seal_hash"], "scores": data["scores"],
                 "planned_count": len(data["index"]["rows"]), "live_model_calls": 0})


def write_scores(export_directory, output, *, study_directory, **verification):
    data = verify_export(export_directory, study_directory=study_directory, **verification)
    output = Path(output)
    require(not output.exists(), "Q score output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    record = _score_record(data, _source(export_directory, study_directory))
    atomic_json(output, record)
    return {"output": str(output), "scored": len(data["scores"]), "seal_hash": record["seal_hash"], "live_model_calls": 0}


def verify_scores(path, **verification):
    record = read_sealed(Path(path))
    data = verify_export(record["export_directory"], study_directory=record["study_directory"], **verification)
    require(record == _score_record(data, _source(record["export_directory"], record["study_directory"])),
            "Q scores differ from primary evidence")
    return data


def _selection(data, part, coding_resolution=None, **verification):
    if part == "owner_communications":
        require(coding_resolution is None, "owner packets do not use an adjudication result")
        return data["index"]["queue_coding_selection"]
    require(part == "episode", "unknown Q review part")
    human = {score["assignment_id"]: adjudicated_endpoints(score) for score in data["scores"]}
    if coding_resolution is not None:
        resolution = verify_resolution(coding_resolution, **verification)
        require(resolution["source_export_hash"] == data["index"]["seal_hash"], "Q resolution belongs to another export")
        human = resolution["endpoints"]
    return episode_selection(data["scores"], human)


def _bundle(data, row, part, source, packet_id, coding_resolution=None):
    identifier = row["assignment_id"]
    attempt = data["attempts"][identifier]
    packet = (build_coding_packet(attempt, packet_id=packet_id) if part == "owner_communications" else
              build_episode_packet(attempt, row, controller_events=data["timelines"][identifier], packet_id=packet_id))
    controller = seal({"kind": "peer_reporting_v14_queue_review_controller", **source, "part": part,
                       "coding_resolution": str(Path(coding_resolution).resolve()) if coding_resolution else None,
                       "assignment_id": identifier, "source_export_hash": data["index"]["seal_hash"],
                       "attempt_content_hash": content_hash(attempt), "selection_row": row,
                       "review_packet": packet, "review_packet_hash": packet["review_packet_hash"]})
    return packet, controller


def _packet_index(data, selection, part, source, entries, coding_resolution=None):
    return seal({"kind": "peer_reporting_v14_queue_review_packets", **source, "part": part,
                 "source_export_hash": data["index"]["seal_hash"], "selection": selection,
                 "coding_resolution": str(Path(coding_resolution).resolve()) if coding_resolution else None,
                 "rows": entries, "live_model_calls": 0,
                 "note": "Researcher-only. Share reviewer/ only; second coding must be blind to first codes."})


def write_packets(export_directory, output, *, study_directory, part="owner_communications",
                  selection=None, scores_path=None, coding_resolution=None, **verification):
    data = verify_export(export_directory, study_directory=study_directory, **verification)
    if scores_path is not None:
        checked = verify_scores(scores_path, **verification)
        require(checked["index"] == data["index"], "Q scoring record belongs to another export")
    expected = _selection(data, part, coding_resolution, **verification)
    if selection is not None:
        check_seal(selection)
        require(selection == expected, "Q review selection differs from primary evidence")
    source, output = _source(export_directory, study_directory), Path(output)
    require(not output.resolve().is_relative_to(Path(export_directory).resolve()), "Q packets must be outside the export")
    require(not output.exists(), "Q packet output already exists")
    output.mkdir(parents=True)
    for child in ("reviewer", "second_reviewer", "controller"):
        (output / child).mkdir()
    entries = []
    for row in expected["rows"]:
        packet, controller = _bundle(data, row, part, source, None, coding_resolution)
        name = packet["review_packet_id"]
        atomic_json(output / "reviewer" / (name + ".json"), seal(packet))
        (output / "reviewer" / (name + ".html")).write_bytes(render_packet_html(packet).encode("ascii"))
        if row.get("second_review", False):
            atomic_json(output / "second_reviewer" / (name + ".json"), seal(packet))
            (output / "second_reviewer" / (name + ".html")).write_bytes(render_packet_html(packet).encode("ascii"))
        atomic_json(output / "controller" / (name + ".json"), controller)
        entries.append({"assignment_id": row["assignment_id"], "review_packet_id": name,
                        "review_packet_hash": packet["review_packet_hash"], "second_review": row.get("second_review", False)})
    for child, members in (("reviewer", entries), ("second_reviewer", [row for row in entries if row["second_review"]])):
        atomic_json(output / child / "order.json", seal({"packet_order": [row["review_packet_id"] for row in members]}))
    atomic_json(output / "index.json", _packet_index(data, expected, part, source, entries, coding_resolution))
    return {"output": str(output), "packets": len(entries), "second_review_packets": sum(
        row["second_review"] for row in entries), "live_model_calls": 0}


def verify_packets(directory, **verification):
    directory = Path(directory)
    index = read_sealed(directory / "index.json")
    data = verify_export(index["export_directory"], study_directory=index["study_directory"], **verification)
    source = _source(index["export_directory"], index["study_directory"])
    selected = _selection(data, index["part"], index["coding_resolution"], **verification)
    require(len(index["rows"]) == len(selected["rows"]), "Q packet population differs from primary evidence")
    identifiers = [row["review_packet_id"] for row in index["rows"]]
    require(len(set(identifiers)) == len(identifiers), "duplicate Q packet IDs")
    entries, bundles = [], {}
    for retained, row in zip(index["rows"], selected["rows"]):
        name = retained["review_packet_id"]
        packet, controller = _bundle(data, row, index["part"], source, name, index["coding_resolution"])
        require(read_sealed(safe_child(directory, "reviewer/" + name + ".json")) == seal(packet),
                "Q reviewer packet differs from primary evidence")
        require(safe_child(directory, "reviewer/" + name + ".html").read_bytes() == render_packet_html(packet).encode("ascii"),
                "Q rendered packet differs from primary evidence")
        require(read_sealed(safe_child(directory, "controller/" + name + ".json")) == controller,
                "Q controller differs from primary evidence")
        if row.get("second_review", False):
            require(read_sealed(safe_child(directory, "second_reviewer/" + name + ".json")) == seal(packet)
                    and safe_child(directory, "second_reviewer/" + name + ".html").read_bytes()
                    == render_packet_html(packet).encode("ascii"), "Q second-coder packet differs from primary evidence")
        entries.append({"assignment_id": row["assignment_id"], "review_packet_id": name,
                        "review_packet_hash": packet["review_packet_hash"], "second_review": row.get("second_review", False)})
        bundles[name] = {"packet": packet, "controller": controller, "row": row}
    for child, members in (("reviewer", entries), ("second_reviewer", [row for row in entries if row["second_review"]])):
        require(read_sealed(directory / child / "order.json") == seal({"packet_order": [
            row["review_packet_id"] for row in members]}), "Q reviewer ordering differs from primary evidence")
    require(index == _packet_index(data, selected, index["part"], source, entries, index["coding_resolution"]),
            "Q packet index or selection differs from primary evidence")
    return {"index": index, "data": data, "bundles": bundles}


def validate_review_upload(upload, packet, *, controller, receipt_directory=None, repair_evidence_directory=None):
    """Public validation also verifies the complete packet population and source export."""
    controller_path = Path(controller)
    context = verify_packets(controller_path.parent.parent, receipt_directory=receipt_directory,
                             repair_evidence_directory=repair_evidence_directory)
    name = packet["review_packet_id"]
    require(name in context["bundles"], "Q upload packet absent from verified packet set")
    bundle = context["bundles"][name]
    require(plain(packet) == bundle["packet"] and read_sealed(controller_path) == bundle["controller"],
            "Q upload packet/controller differs from primary evidence")
    return {**validate_upload(upload, packet), "bindings_verified": True, "live_model_calls": 0}


def _superseded_resolution(upload, path, directory, **verification):
    # Check the target before following its history. An altered reference back
    # to the current version must fail without recursively following itself.
    previous = read_sealed(Path(path))
    require(previous["packet_directory"] == str(Path(directory).resolve()), "Q correction belongs to another packet set")
    require(previous["seal_hash"] == upload["supersedes_resolution_hash"], "Q correction supersession hash mismatch")
    return verify_resolution(path, **verification)


def retain_upload(upload_path, packet_path, controller_path, output, *, adjudication=False,
                  correction_of=None, **verification):
    import hashlib

    upload_path, output = Path(upload_path), Path(output)
    packet, upload = read_sealed(Path(packet_path)), read_json(upload_path)
    require(not adjudication or correction_of is None, "Q correction and adjudication modes are distinct")
    if not adjudication and correction_of is None:
        validate_review_upload(upload, packet, controller=controller_path, **verification)
    else:
        context = verify_packets(Path(controller_path).parent.parent, **verification)
        require(context["bundles"][packet["review_packet_id"]]["packet"] == plain(packet), "Q adjudication packet mismatch")
        require(read_sealed(Path(controller_path)) == context["bundles"][packet["review_packet_id"]]["controller"],
                "Q adjudication controller mismatch")
    validate_upload(upload, packet, adjudication=adjudication, correction=correction_of is not None)
    if correction_of is not None:
        previous = _superseded_resolution(upload, correction_of, Path(controller_path).parent.parent, **verification)
        name = packet["review_packet_id"]
        require(previous["resolutions"][name]["status"] == "final", "Q correction requires a final earlier resolution")
        resolve_reviews(packet, previous["resolutions"][name]["initial_reviews"],
                        second_review=context["bundles"][name]["row"]["second_review"],
                        adjudication={key: value for key, value in upload.items() if key != "supersedes_resolution_hash"})
    submission = output.with_name(output.stem + "-submission.json")
    require(not output.exists() and not submission.exists(), "Q retained review output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    # The submitted file is primary evidence. Keep its exact bytes, separately
    # from the derived validation record, so resealing a copy cannot change codes.
    with submission.open("xb") as stream:
        stream.write(upload_path.read_bytes())
    retained = Path(controller_path).parent.parent / "submissions"
    retained.mkdir(exist_ok=True)
    primary = retained / (hashlib.sha256(submission.read_bytes()).hexdigest() + ".json")
    if primary.exists():
        require(primary.read_bytes() == submission.read_bytes(), "Q retained primary submission mismatch")
    else:
        with primary.open("xb") as stream:
            stream.write(submission.read_bytes())
    record = _upload_record(upload, packet, Path(controller_path).parent.parent, submission, adjudication, correction_of)
    atomic_json(output, record)
    return {"output": str(output), "valid": True, "reviewer_id": upload["reviewer_id"], "live_model_calls": 0}


def _upload_record(upload, packet, packet_directory, submission, adjudication, correction_of=None):
    import hashlib

    return seal({"kind": "peer_reporting_v14_queue_correction" if correction_of is not None else
                    "peer_reporting_v14_queue_retained_review", "packet_directory": str(Path(packet_directory).resolve()),
                 "review_packet_id": packet["review_packet_id"], "review_packet_hash": packet["review_packet_hash"],
                 "submission_path": str(Path(submission).resolve()), "submission_sha256": hashlib.sha256(
                     Path(submission).read_bytes()).hexdigest(), "adjudication": adjudication,
                 **({"correction_of": str(Path(correction_of).resolve()),
                     "supersedes_resolution_hash": upload["supersedes_resolution_hash"]} if correction_of is not None else {}),
                 "validation": validate_upload(upload, packet, adjudication=adjudication, correction=correction_of is not None)})


def _checked_upload(path, context):
    record = read_sealed(Path(path))
    require(record["packet_directory"] == str(context["directory"]), "Q retained review belongs to another packet set")
    name = record["review_packet_id"]
    require(name in context["bundles"], "Q retained review names an unknown packet")
    packet = context["bundles"][name]["packet"]
    submission = Path(record["submission_path"])
    require(submission == Path(path).resolve().with_name(Path(path).stem + "-submission.json"),
            "Q retained submission path differs from its validation record")
    primary = context["directory"] / "submissions" / (record["submission_sha256"] + ".json")
    require(primary.is_file() and submission.is_file() and primary.read_bytes() == submission.read_bytes(),
            "Q review differs from retained primary submission")
    upload = read_json(submission)
    correction_of = record.get("correction_of")
    require(record["adjudication"] == ("record_conflict" in upload and correction_of is None),
            "Q retained adjudication type mismatch")
    require(record == _upload_record(upload, packet, context["directory"], submission, record["adjudication"], correction_of),
            "Q review validation differs from retained submission")
    return name, upload, record["adjudication"], correction_of


def _submission_inventory(directory, upload_paths, adjudication_paths, correction_paths):
    entries = [{"path": str(Path(path).resolve()), "role": role,
                "submission_sha256": read_sealed(Path(path))["submission_sha256"]}
               for role, paths in (("upload", upload_paths), ("adjudication", adjudication_paths),
                                   ("correction", correction_paths)) for path in paths]
    require(len({row["path"] for row in entries}) == len(entries), "duplicate Q retained review paths")
    hashes = [row["submission_sha256"] for row in entries]
    require(len(set(hashes)) == len(hashes), "Q resolution must include every retained submission exactly once")
    return seal({"kind": "peer_reporting_v14_queue_submission_inventory",
                 "packet_directory": str(Path(directory).resolve()), "submissions": entries})


def _resolution_record(directory, upload_paths, adjudication_paths, correction_paths=(), *, inventory=None, **verification):
    context = verify_packets(directory, **verification)
    require(context["index"]["part"] == "owner_communications", "Q adjudicated endpoints require owner coding packets")
    context["directory"] = Path(directory).resolve()
    uploads, adjudications = {}, {}
    expected_inventory = _submission_inventory(directory, upload_paths, adjudication_paths, correction_paths)
    if inventory is None:
        primary_submissions = {path.stem for path in (context["directory"] / "submissions").glob("*.json")}
        require({row["submission_sha256"] for row in expected_inventory["submissions"]} == primary_submissions,
                "Q resolution must include every retained submission exactly once")
        inventory = expected_inventory
    else:
        check_seal(inventory)
        primary_inventory = context["directory"] / "resolution-inventories" / (inventory["seal_hash"] + ".json")
        require(primary_inventory.is_file() and read_sealed(primary_inventory) == inventory == expected_inventory,
                "Q resolution submission inventory differs from its retained version")
    for path in upload_paths:
        name, upload, adjudication, correction_of = _checked_upload(path, context)
        require(not adjudication and correction_of is None, "initial Q upload is an adjudication or correction")
        uploads.setdefault(name, []).append(upload)
    for path in adjudication_paths:
        name, upload, adjudication, correction_of = _checked_upload(path, context)
        require(adjudication and correction_of is None and name not in adjudications, "invalid or duplicate Q adjudication")
        adjudications[name] = upload
    by_assignment, resolutions, agreement_by_arm = {}, {}, {}
    for name, bundle in context["bundles"].items():
        resolution = resolve_reviews(bundle["packet"], uploads.get(name, []), second_review=bundle["row"]["second_review"],
                                     adjudication=adjudications.get(name))
        resolutions[name] = resolution
        initial = resolution["initial_reviews"]
        if bundle["row"]["second_review"] and len(initial) >= 2 and "owner_informed" in next(
                iter(bundle["packet"]["label_template"].values()), {}):
            pair = tuple(any(codes["owner_informed"] for codes in upload["labels"].values()) for upload in initial[:2])
            for arm in bundle["row"]["arms"]:
                if bundle["row"]["assignment_id"] in context["index"]["selection"]["second_review_by_arm"][arm]:
                    agreement_by_arm.setdefault(arm, []).append(pair)
    for path in correction_paths:
        name, upload, adjudication, correction_of = _checked_upload(path, context)
        require(not adjudication and correction_of is not None, "invalid Q correction")
        previous = _superseded_resolution(upload, correction_of, context["directory"], **verification)
        require(previous["resolutions"][name]["status"] == "final"
                and previous["resolutions"][name] == resolutions[name], "Q correction does not supersede the current episode resolution")
        bundle = context["bundles"][name]
        corrected = resolve_reviews(bundle["packet"], uploads.get(name, []), second_review=bundle["row"]["second_review"],
            adjudication={key: value for key, value in upload.items() if key != "supersedes_resolution_hash"})
        corrected["corrections"] = [*resolutions[name].get("corrections", []), deepcopy(upload)]
        resolutions[name] = corrected
    for name, bundle in context["bundles"].items():
        by_assignment[bundle["row"]["assignment_id"]] = (bundle["packet"], resolutions[name])
    endpoints = {score["assignment_id"]: adjudicated_endpoints(score, *by_assignment.get(score["assignment_id"], (None, None)))
                 for score in context["data"]["scores"]}
    mechanical = {score["assignment_id"]: {name: score["endpoints"][name] for name in (
        "owner_informed", "focal_false_alarm", "twin_any_allegation")} for score in context["data"]["scores"]}
    comparisons = [{"assignment_id": identifier, "mechanical": mechanical[identifier]["owner_informed"]["value"],
                    "adjudicated": values["owner_informed"]["value"], "disagrees":
                    mechanical[identifier]["owner_informed"]["value"] != values["owner_informed"]["value"]}
                   for identifier, values in endpoints.items() if values["owner_informed"]["applicable"]]
    mechanical_pairs = [(row["mechanical"], row["adjudicated"]) for row in comparisons
                        if type(row["mechanical"]) is bool and type(row["adjudicated"]) is bool]
    # Deduplicate shared-arm packets in pooled agreement.
    pooled_pairs = []
    for name, bundle in context["bundles"].items():
        initial = resolutions[name]["initial_reviews"]
        if bundle["row"]["second_review"] and len(initial) >= 2 and "owner_informed" in next(
                iter(bundle["packet"]["label_template"].values()), {}):
            pooled_pairs.append(tuple(any(codes["owner_informed"] for codes in upload["labels"].values()) for upload in initial[:2]))
    return seal({"kind": "peer_reporting_v14_queue_adjudication", "packet_directory": str(context["directory"]),
                 "source_export_hash": context["index"]["source_export_hash"],
                 "uploads": [str(Path(path).resolve()) for path in upload_paths],
                 "adjudications": [str(Path(path).resolve()) for path in adjudication_paths],
                 "corrections": [str(Path(path).resolve()) for path in correction_paths],
                 "submission_inventory": inventory, "resolution_version": inventory["seal_hash"],
                 "resolutions": resolutions, "endpoints": endpoints, "agreement": agreement(pooled_pairs),
                 "mechanical_endpoints": mechanical, "owner_informed_comparisons": comparisons,
                 "mechanical_adjudicated_agreement": {**agreement(mechanical_pairs),
                     "unresolved_pairs": len(comparisons) - len(mechanical_pairs)},
                 "agreement_by_arm": {arm: agreement(pairs) for arm, pairs in agreement_by_arm.items()},
                 "live_model_calls": 0})


def write_adjudication(directory, output, *, uploads=(), adjudications=(), corrections=(), **verification):
    record = _resolution_record(directory, uploads, adjudications, corrections, **verification)
    output = Path(output)
    require(not output.exists(), "Q adjudication output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = record["submission_inventory"]
    retained = Path(directory) / "resolution-inventories"
    retained.mkdir(exist_ok=True)
    primary = retained / (inventory["seal_hash"] + ".json")
    if primary.exists():
        require(read_sealed(primary) == inventory, "Q retained submission inventory mismatch")
    else:
        with primary.open("xb") as stream:
            stream.write(json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n")
    atomic_json(output, record)
    return {"output": str(output), "seal_hash": record["seal_hash"], "resolution_version": record["resolution_version"],
            "agreement": record["agreement"], "live_model_calls": 0}


def verify_resolution(path, **verification):
    record = read_sealed(Path(path))
    expected = _resolution_record(record["packet_directory"], record["uploads"], record["adjudications"], record["corrections"],
                                  inventory=record["submission_inventory"], **verification)
    require(record == expected, "Q adjudication differs from retained submissions or primary evidence")
    return expected
