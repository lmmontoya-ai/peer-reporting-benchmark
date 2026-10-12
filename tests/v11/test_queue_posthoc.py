"""Fake-transport export, scoring, two coders, adjudication and P-A4 refusals."""

from contextlib import contextmanager
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.cli import _parser, main
from swarm_auth_bench.peer_reporting_v11.collection import build_study
from swarm_auth_bench.peer_reporting_v11.queue_posthoc import (
    export_review,
    retain_upload,
    validate_review_upload,
    verify_export,
    verify_packets,
    verify_resolution,
    verify_scores,
    write_adjudication,
    write_packets,
    write_scores,
)
from swarm_auth_bench.peer_reporting_v11.queue_protocol import caps_record, load_protocol
from swarm_auth_bench.peer_reporting_v11.queue_storage import atomic_json
from swarm_auth_bench.peer_reporting_v11.queue_templates import TEMPLATES
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_Q

from .live_fakes import authorization, fake_study_verifier, run_phase
from .queue_score_helpers import upload
from .receipt_helpers import commit_receipt
from .test_queue_live import QueueHarness, steps


@pytest.fixture(scope="module")
async def chain(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("q2b-chain")
    study, compat, receipts, bundle = tmp / "study", tmp / "compat", tmp / "receipts", load_bundle()
    build_study(study, protocol=load_protocol(), templates=TEMPLATES, caps_record=caps_record())
    built = live.build_compatibility_plan(caps_record(), revision="q2b-offline", tool_schema_version=TOOL_SCHEMA_VERSION_Q)
    live.prepare_live_root(compat, built)
    fixture = next(iter(built[2].values()))
    harness = QueueHarness(tmp / "compat-homes", lambda *_: steps(fixture))
    top = live.read_live_plan(compat)
    status = await run_phase(compat, top, harness, bundle=bundle, auth=authorization(top, root=compat, deadline=12000))
    assert status["status"] == "complete", status
    commit_receipt(compat, receipts)
    for phase in ("smoke", "screening"):
        root = study / "roots" / phase
        kwargs = {"study_directory": study, "compatibility_directories": [compat], "receipt_directory": receipts}
        if phase == "screening":
            kwargs["smoke_directory"] = study / "roots/smoke"
        built = live.build_phase_plan(phase, caps_record(), revision="q2b-" + phase, bundle=bundle,
                                      study_verifier=fake_study_verifier, **kwargs)
        live.prepare_live_root(root, built, bundle=bundle,
                               **{key: value for key, value in kwargs.items() if key != "smoke_directory"})

        def script_for(model, effort):
            def script(fixture):
                if phase == "smoke":
                    return steps(fixture)
                script = steps(fixture)
                # Screening reaches v, but deliberately ends before the rest of the queue.
                limit = next(index for index, step in enumerate(script) if step[:2] == ("tool", "next_item")
                             and sum(item[:2] == ("tool", "next_item") for item in script[:index + 1]) == 21)
                script = script[:limit]
                script.insert(2, ("raw", "item/completed", {"item": {"id": "early-output", "type": "agentMessage",
                    "phase": "commentary", "text": "Early benchmark observation."}}))
                for step in script:
                    if step[:2] == ("tool", "report_incident"):
                        step[2].update(authorization_state="unauthorized", certainty="confirmed", summary="No grant on file.")
                ending = [("tool", "agent_finish", {"reason": "completed", "summary": "Queue totals submitted."}),
                          ("message", "Queue processing closed.")]
                if fixture["parameters"]["pressure"] == "neutral":
                    ending.append(("call", lambda: (root / "STOP").write_bytes(b"Offline population-prefix check.\n")))
                return script + ending
            return script

        harness = QueueHarness(tmp / (phase + "-homes"), script_for)
        top = live.read_live_plan(root)
        status = await run_phase(root, top, harness, bundle=bundle, auth=authorization(top, deadline=12000), **kwargs)
        if phase == "smoke":
            assert status["status"] == "complete", status
        else:
            assert 1 <= status["status_counts"]["archived"] <= 8, status
        commit_receipt(root, receipts, study_directory=study)
    export, packets, scores = tmp / "export", tmp / "packets", tmp / "scores.json"
    export_review(root, export, study_directory=study, receipt_directory=receipts)
    options = {"study_directory": study, "receipt_directory": receipts}
    write_scores(export, scores, **options)
    write_packets(export, packets, scores_path=scores, **options)
    return {"tmp": tmp, "study": study, "export": export, "packets": packets, "scores": scores,
            "receipts": receipts, "root": root, "options": options}


def review_options(chain):
    return {"receipt_directory": chain["receipts"]}


def first_packet(chain):
    index = read_sealed(chain["packets"] / "index.json")
    row = next(row for row in index["rows"] if row["second_review"])
    name = row["review_packet_id"]
    return chain["packets"] / "reviewer" / (name + ".json"), chain["packets"] / "controller" / (name + ".json")


@contextmanager
def altered(path, change):
    original = path.read_bytes()
    payload = read_sealed(path)
    payload.pop("seal_hash")
    change(payload)
    atomic_json(path, seal(payload))
    try:
        yield
    finally:
        path.write_bytes(original)


def test_screening_chain_scores_and_sample_drawn_at_export_seal(chain):
    data = verify_export(chain["export"], **chain["options"])
    assert 1 <= len(data["scores"]) <= 8
    assert len(data["index"]["rows"]) == 40
    assert all(score["eligibility"]["eligible"] for score in data["scores"])
    assert all(score["endpoints"]["owner_informed"]["value"] is True for score in data["scores"])
    assert all(score["usage"]["per_call_usage"] for score in data["scores"])
    assert all(score["usage"]["max_call_input_tokens"] == max(call["input_tokens"]
               for call in score["usage"]["per_call_usage"]) for score in data["scores"])
    assert all(score["usage"]["wall_seconds"] >= score["usage"]["generation_seconds"] for score in data["scores"])
    selected = data["index"]["queue_coding_selection"]
    n = len(selected["rows"])
    assert 1 <= n <= 8
    assert len(selected["second_review_by_arm"]["queue_screen"]) == -(-n // 4)
    assert read_sealed(chain["packets"] / "index.json")["selection"] == selected
    assert len(verify_scores(chain["scores"], **review_options(chain))["scores"]) == len(data["scores"])
    assert len(verify_packets(chain["packets"], **review_options(chain))["bundles"]) == n


@pytest.mark.parametrize("target", ["export_label", "export_score", "export_sample", "attempt", "scores", "selection",
                                    "controller", "packet", "html"])
def test_altered_derived_records_refused_at_every_step(chain, target):
    packet_path, controller_path = first_packet(chain)
    packet = read_sealed(packet_path)
    submitted = upload(packet)
    if target == "html":
        path = packet_path.with_suffix(".html")
        original = path.read_bytes()
        path.write_bytes(original + b"altered")
        try:
            with pytest.raises(ValueError, match="rendered"):
                validate_review_upload(submitted, packet, controller=controller_path, **review_options(chain))
        finally:
            path.write_bytes(original)
        return
    if target.startswith("export_"):
        path = chain["export"] / "index.json"
        def change(record):
            if target == "export_label":
                record["rows"][0]["pressure"] = "altered"
            elif target == "export_score":
                record["rows"][0]["score"] = {"invented": True}
            else:
                record["queue_coding_selection"]["rows"][0]["second_review"] ^= True
                record["queue_coding_selection"] = seal({key: value for key, value in record["queue_coding_selection"].items()
                                                        if key != "seal_hash"})
        with altered(path, change):
            for step in (lambda: write_scores(chain["export"], chain["tmp"] / "rejected-score.json", **chain["options"]),
                         lambda: write_packets(chain["export"], chain["tmp"] / "rejected-packets", **chain["options"]),
                         lambda: write_packets(chain["export"], chain["tmp"] / "rejected-episode", part="episode", **chain["options"]),
                         lambda: validate_review_upload(submitted, packet, controller=controller_path, **review_options(chain)),
                         lambda: write_adjudication(chain["packets"], chain["tmp"] / "rejected-resolution.json", **review_options(chain))):
                with pytest.raises(ValueError):
                    step()
        return
    if target == "attempt":
        row = read_sealed(chain["export"] / "index.json")["rows"][0]
        path = chain["export"] / row["attempt_path"]
        def change(record):
            return record["observer_outputs"][0].update(text="altered")
        def operation():
            return verify_export(chain["export"], **chain["options"])
    elif target == "scores":
        path = chain["scores"]
        def change(record):
            return record["scores"][0]["endpoints"]["owner_informed"].update(value=False)
        def operation():
            return write_packets(chain["export"], chain["tmp"] / "wrong-scores", scores_path=path, **chain["options"])
    elif target == "selection":
        path = chain["packets"] / "index.json"
        def change(record):
            return record["rows"][0].update(second_review=not record["rows"][0]["second_review"])
        def operation():
            return validate_review_upload(submitted, packet, controller=controller_path, **review_options(chain))
    elif target == "controller":
        path = controller_path
        def change(record):
            return record["selection_row"].update(second_review=False)
        def operation():
            return validate_review_upload(submitted, packet, controller=controller_path, **review_options(chain))
    else:
        path = packet_path
        def change(record):
            record["owner_message"] += " altered"
            record["review_packet_hash"] = __import__("swarm_auth_bench.events", fromlist=["content_hash"]).content_hash(
                {key: value for key, value in record.items() if key != "review_packet_hash"})
        def operation():
            return validate_review_upload(submitted, read_sealed(path), controller=controller_path, **review_options(chain))
    with altered(path, change), pytest.raises(ValueError):
        operation()


def test_two_coders_episode_review_and_correction_preserve_verifiable_resolution_versions(chain):
    packet_path, controller_path = first_packet(chain)
    packet = read_sealed(packet_path)
    retained = []
    for index, submitted in enumerate((upload(packet, "coder-1", True), upload(packet, "coder-2", False))):
        path = chain["tmp"] / f"upload-{index}.json"
        atomic_json(path, submitted)
        validated = chain["tmp"] / f"validated-{index}.json"
        retain_upload(path, packet_path, controller_path, validated, **review_options(chain))
        assert validated.with_name(validated.stem + "-submission.json").read_bytes() == path.read_bytes()
        retained.append(validated)
    disputed_path = chain["tmp"] / "disputed.json"
    write_adjudication(chain["packets"], disputed_path, uploads=retained, **review_options(chain))
    disputed = verify_resolution(disputed_path, **review_options(chain))
    assignment = read_sealed(controller_path)["assignment_id"]
    assert disputed["endpoints"][assignment]["owner_informed"]["value"] is None
    submitted = upload(packet, "adjudicator", True, adjudication=True)
    path, validated = chain["tmp"] / "adjudicator.json", chain["tmp"] / "validated-adjudicator.json"
    atomic_json(path, submitted)
    retain_upload(path, packet_path, controller_path, validated, adjudication=True, **review_options(chain))
    resolution_path = chain["tmp"] / "resolved.json"
    write_adjudication(chain["packets"], resolution_path, uploads=retained, adjudications=[validated], **review_options(chain))
    final = verify_resolution(resolution_path, **review_options(chain))
    assert final["endpoints"][assignment]["owner_informed"]["value"] is True
    assert final["endpoints"][assignment]["disclosure_position"]["value"] == 0
    assert final["agreement"]["n"] == 1 and final["agreement"]["percent_agreement"] == 0
    assert final["mechanical_adjudicated_agreement"]["percent_agreement"] == 100
    episode = chain["tmp"] / "episode"
    write_packets(chain["export"], episode, part="episode", coding_resolution=resolution_path, **chain["options"])
    checked = verify_packets(episode, **review_options(chain))
    assert len(checked["bundles"]) == len(read_sealed(chain["scores"])["scores"])
    episode_packet = next(iter(checked["bundles"].values()))["packet"]
    assert any(row.get("text") == "Early benchmark observation." for row in episode_packet["timeline"])
    assert episode_packet["reasoning_summaries"]["items"]
    episode_name = episode_packet["review_packet_id"]
    assert validate_review_upload(upload(episode_packet), episode_packet,
        controller=episode / "controller" / (episode_name + ".json"), **review_options(chain))["bindings_verified"]
    with altered(resolution_path, lambda record: record["endpoints"][assignment]["owner_informed"].update(value=False)):
        with pytest.raises(ValueError, match="retained submissions"):
            verify_resolution(resolution_path, **review_options(chain))
    with altered(retained[0], lambda record: record["validation"].update(status="pending")):
        with pytest.raises(ValueError, match="retained submission"):
            verify_resolution(resolution_path, **review_options(chain))
    with pytest.raises(ValueError, match="every retained submission"):
        write_adjudication(chain["packets"], chain["tmp"] / "omitted.json", uploads=retained[:1], **review_options(chain))
    # Episode review follows the completed owner coding and records a conflict.
    episode_upload = upload(episode_packet, "episode-reviewer")
    episode_upload["rationale"] = "Episode review found that the owner text did not identify the change."
    episode_input, episode_retained = chain["tmp"] / "episode-review.json", chain["tmp"] / "retained-episode-review.json"
    atomic_json(episode_input, episode_upload)
    retain_upload(episode_input, episode / "reviewer" / (episode_name + ".json"),
                  episode / "controller" / (episode_name + ".json"), episode_retained, **review_options(chain))
    originals = {path: path.read_bytes() for path in [resolution_path, disputed_path,
        *(record.with_name(record.stem + "-submission.json") for record in [*retained, validated]),
        *(chain["packets"] / "submissions").glob("*.json")]}
    correction = upload(packet, "correction-adjudicator", False, adjudication=True)
    correction.update(record_conflict=True, supersedes_resolution_hash=final["seal_hash"],
                      rationale="Episode review found that the owner communications did not identify the change.")
    correction_input, correction_retained = chain["tmp"] / "correction.json", chain["tmp"] / "validated-correction.json"
    atomic_json(correction_input, correction)
    retain_upload(correction_input, packet_path, controller_path, correction_retained,
                  correction_of=resolution_path, **review_options(chain))
    record = read_sealed(correction_retained)
    assert record["kind"] == "peer_reporting_v14_queue_correction"
    assert record["supersedes_resolution_hash"] == final["seal_hash"]
    assert verify_resolution(resolution_path, **review_options(chain)) == final
    assert verify_resolution(disputed_path, **review_options(chain)) == disputed
    corrected_path = chain["tmp"] / "corrected.json"
    write_adjudication(chain["packets"], corrected_path, uploads=retained, adjudications=[validated],
                       corrections=[correction_retained], **review_options(chain))
    corrected = verify_resolution(corrected_path, **review_options(chain))
    assert corrected["endpoints"][assignment]["owner_informed"]["value"] is False
    assert corrected["endpoints"][assignment]["disclosure_position"]["value"] is None
    assert {key: value for key, value in corrected["endpoints"].items() if key != assignment} == {
        key: value for key, value in final["endpoints"].items() if key != assignment}
    assert corrected["resolution_version"] != final["resolution_version"]
    assert len(final["submission_inventory"]["submissions"]) == 3
    assert len(corrected["submission_inventory"]["submissions"]) == 4
    assert corrected["resolutions"][packet["review_packet_id"]]["corrections"] == [correction]
    assert corrected["agreement"] == final["agreement"]
    assert verify_resolution(resolution_path, **review_options(chain)) == final
    assert verify_packets(episode, **review_options(chain))["index"] == checked["index"]
    assert all(path.read_bytes() == data for path, data in originals.items())
    for field in ("uploads", "adjudications", "corrections"):
        with altered(corrected_path, lambda record: record[field].pop()):
            with pytest.raises(ValueError, match="submission inventory"):
                verify_resolution(corrected_path, **review_options(chain))
    with altered(corrected_path, lambda record: record.update(submission_inventory=seal({
        **{key: value for key, value in record["submission_inventory"].items() if key != "seal_hash"},
        "submissions": record["submission_inventory"]["submissions"][:-1]}))):
        with pytest.raises(ValueError, match="submission inventory"):
            verify_resolution(corrected_path, **review_options(chain))
    for omitted in ("upload", "adjudication", "correction"):
        with pytest.raises(ValueError, match="every retained submission"):
            write_adjudication(chain["packets"], chain["tmp"] / ("omitted-" + omitted + ".json"),
                uploads=retained[:1] if omitted == "upload" else retained,
                adjudications=[] if omitted == "adjudication" else [validated],
                corrections=[] if omitted == "correction" else [correction_retained], **review_options(chain))
    for validation in (retained[0], correction_retained):
        submission = validation.with_name(validation.stem + "-submission.json")
        original = submission.read_bytes()
        submission.write_bytes(original + b" ")
        try:
            with pytest.raises(ValueError, match="retained primary submission"):
                verify_resolution(corrected_path, **review_options(chain))
            if validation == retained[0]:
                with pytest.raises(ValueError, match="retained primary submission"):
                    verify_resolution(resolution_path, **review_options(chain))
            else:
                assert verify_resolution(resolution_path, **review_options(chain)) == final
        finally:
            submission.write_bytes(original)


def test_cli_command_arguments_and_offline_dispatch(tmp_path, capsys):
    parser = _parser()
    for command in ("score-queue", "queue-coding-packets", "queue-episode-packets"):
        args = parser.parse_args([command, "export", "--study", "study", "--output", "output"])
        assert args.command == command
    args = parser.parse_args(["validate-queue-review-upload", "upload", "--packet", "packet", "--controller", "controller",
                              "--output", "validation", "--adjudication"])
    assert args.adjudication
    args = parser.parse_args(["validate-queue-review-upload", "upload", "--packet", "packet", "--controller", "controller",
                              "--output", "validation", "--correction-of", "previous"])
    assert str(args.correction_of) == "previous"
    args = parser.parse_args(["adjudicate-queue-review", "packets", "--upload", "one", "--upload", "two", "--output", "result"])
    assert len(args.upload) == 2
    args = parser.parse_args(["adjudicate-queue-review", "packets", "--correction", "correction", "--output", "result"])
    assert len(args.correction) == 1
    assert main(["score-queue", str(tmp_path / "absent"), "--study", str(tmp_path / "study"), "--output", str(tmp_path / "out")]) == 2
    assert "error" in capsys.readouterr().out


def test_cli_export_seals_queue_coding_selection(chain, capsys):
    output = chain["tmp"] / "cli-export"
    assert main(["export-review", str(chain["root"]), "--output", str(output),
                 "--study", str(chain["study"]), "--receipt-directory", str(chain["receipts"]),
                 "--no-score"]) == 0
    assert '"live_model_calls": 0' in capsys.readouterr().out
    data = verify_export(output, **chain["options"])
    expected = read_sealed(chain["export"] / "index.json")["queue_coding_selection"]
    assert data["index"]["queue_coding_selection"] == expected
    assert data["index"]["scored"] is False


def test_agreement_uses_executed_arms_and_counts_a_shared_episode_once(tmp_path, monkeypatch):
    from swarm_auth_bench.peer_reporting_v11 import queue_posthoc
    from swarm_auth_bench.peer_reporting_v11.queue_review import build_coding_packet, coding_selection
    from swarm_auth_bench.peer_reporting_v11.queue_score import score_trial

    from .queue_score_helpers import attempt, output

    directory = tmp_path / "packets"
    directory.mkdir()
    (directory / "submissions").mkdir()
    samples = []
    for model, effort, executed in (("gpt-6-sol", "low", ["queue_main"]),
            ("gpt-6-sol", "xhigh", ["queue_main"]), ("gpt-6-luna", "xhigh", ["queue_main"]),
            ("gpt-6-luna", "low", ["queue_main", "queue_attention"])):
        sample = attempt()
        sample.update(assignment_id=model + effort, model=model, effort=effort,
                      arm_memberships=["queue_main", "queue_attention"], executed_arm_memberships=executed)
        output(sample, "Queue totals submitted.")
        samples.append(sample)
    scores = [score_trial(sample) for sample in samples]
    selected = coding_selection(scores)
    by_assignment = {sample["assignment_id"]: sample for sample in samples}
    bundles, retained = {}, []
    for row in selected["rows"]:
        sample = by_assignment[row["assignment_id"]]
        packet = build_coding_packet(sample, packet_id=row["assignment_id"])
        name = packet["review_packet_id"]
        bundles[name] = {"packet": packet, "row": row}
        for index in range(2 if row["second_review"] else 1):
            submitted = upload(packet, "coder-" + str(index), sample["model"] == "gpt-6-luna" and sample["effort"] == "low")
            validation = tmp_path / (name + "-" + str(index) + ".json")
            submission = validation.with_name(validation.stem + "-submission.json")
            atomic_json(submission, submitted)
            record = queue_posthoc._upload_record(submitted, packet, directory, submission, False)
            (directory / "submissions" / (record["submission_sha256"] + ".json")).write_bytes(submission.read_bytes())
            atomic_json(validation, record)
            retained.append(validation)
    context = {"index": {"part": "owner_communications", "source_export_hash": "authored-offline-population",
                         "selection": selected}, "bundles": bundles, "data": {"scores": scores}}
    monkeypatch.setattr(queue_posthoc, "verify_packets", lambda *args, **kwargs: deepcopy(context))
    resolved = tmp_path / "resolved.json"
    write_adjudication(directory, resolved, uploads=retained)
    final = verify_resolution(resolved)
    assert final["agreement_by_arm"]["queue_attention"]["n"] == 1
    assert final["agreement_by_arm"]["queue_attention"]["table"] == [[0, 0], [0, 1]]
    assert final["agreement_by_arm"]["queue_main"]["n"] == 1
    assert final["agreement"]["n"] == sum(row["second_review"] for row in selected["rows"])
    name = "gpt-6-lunalow"
    packet = bundles[name]["packet"]
    versions, corrections = [(resolved, final)], []
    for index, value in enumerate((False, True)):
        parent_path, parent = versions[-1]
        submitted = upload(packet, "correction-adjudicator", value, adjudication=True)
        submitted.update(record_conflict=True, supersedes_resolution_hash=parent["seal_hash"])
        validation = tmp_path / ("correction-" + str(index) + ".json")
        submission = validation.with_name(validation.stem + "-submission.json")
        atomic_json(submission, submitted)
        record = queue_posthoc._upload_record(submitted, packet, directory, submission, False, parent_path)
        (directory / "submissions" / (record["submission_sha256"] + ".json")).write_bytes(submission.read_bytes())
        atomic_json(validation, record)
        corrections.append(validation)
        corrected_path = tmp_path / ("corrected-" + str(index) + ".json")
        write_adjudication(directory, corrected_path, uploads=retained, corrections=corrections)
        corrected = verify_resolution(corrected_path)
        assert corrected["endpoints"][name]["owner_informed"]["value"] is value
        assert len(corrected["resolutions"][name]["corrections"]) == index + 1
        for version_path, version in versions:
            assert verify_resolution(version_path) == version
        with altered(validation, lambda payload: payload.update(correction_of=str(corrected_path.resolve()))):
            with pytest.raises(ValueError, match="supersession hash mismatch"):
                verify_resolution(corrected_path)
        versions.append((corrected_path, corrected))
