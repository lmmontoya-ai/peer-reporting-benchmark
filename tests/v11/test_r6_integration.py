"""Merged revision 6 contract checks. All execution is offline."""

from copy import deepcopy

import pytest

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, pressure_text
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.ladder import verify_fixture
from swarm_auth_bench.peer_reporting_v11.prompts import (
    HELD_SUBMISSIONS_SENTENCE, PRIVATE_REPORT_SENTENCE, VISIBLE_REPORT_SENTENCE,
    build_instructions,
)
from swarm_auth_bench.peer_reporting_v11.review import build_review_bundle
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

from .live_fakes import Harness, qualifier_steps, run_phase


def study_fixture(study, row):
    directory, manifest, _ = study
    stored = read_sealed(directory / manifest["fixtures"][row["fixture_id"]]["path"])
    return {key: value for key, value in stored.items() if key != "seal_hash"}


@pytest.mark.parametrize("routing", ["private", "visible", "P4"])
def test_live_instructions_equal_study_and_routing_manifest(routing, wp6_study, wp6_inputs):
    _, manifest, _ = wp6_study
    row = next(row for row in manifest["assignments"] if row["level"] == "P"
               and (row["pressure"] == "P4" if routing == "P4" else
                    row["visibility"] == routing and row["pressure"] == "neutral"))
    fixture = study_fixture(wp6_study, row)
    entries = live.validate_assignment_rows("calibration", [row], {row["fixture_id"]: fixture},
                                            wp6_inputs["caps_record"], load_bundle())
    text = entries[0]["instructions"]
    archived = manifest["pressure_prompt_manifests"][row["template_id"]][routing]
    assert text.encode() == row["instructions"].encode() == archived["instructions"]["neutral"].encode()
    assert content_hash(text) == archived["hashes"]["neutral"]
    template = wp6_inputs["templates"][row["template_id"]]
    old = build_instructions("neutral", template, wp6_inputs["caps_record"]["trial"])
    replacement = VISIBLE_REPORT_SENTENCE + (" " + HELD_SUBMISSIONS_SENTENCE if routing == "P4" else "")
    assert text == (old if routing == "private" else old.replace(PRIVATE_REPORT_SENTENCE, replacement))
    packet = build_review_bundle({"fixture": fixture, "accepted_reports": [], "observer_outputs": []}, scope="full")["packet"]
    assert (PRIVATE_REPORT_SENTENCE in packet["common_instructions"]) == (routing == "private")
    assert (VISIBLE_REPORT_SENTENCE in packet["common_instructions"]) == (routing != "private")
    assert (HELD_SUBMISSIONS_SENTENCE in packet["common_instructions"]) == (routing == "P4")


@pytest.mark.parametrize("field,value", [("visibility", "private"), ("pressure", "P1"),
                                         ("prompt_condition", "guided"), ("world_mode", "report_store_unavailable")])
def test_pressure_assignment_rejects_inconsistent_rows(field, value, wp6_study, wp6_inputs):
    row = deepcopy(next(row for row in wp6_study[1]["assignments"]
                        if row["level"] == "P" and row["pressure"] == "P4"))
    fixture = study_fixture(wp6_study, row)
    row[field] = value
    with pytest.raises(ValueError, match="labels differ|requires neutral"):
        live.validate_assignment_rows("calibration", [row], {row["fixture_id"]: fixture},
                                      wp6_inputs["caps_record"], load_bundle())


@pytest.mark.parametrize("kind", ["reaction", "operation_id", "notice"])
def test_fixture_verifier_checks_rendered_reaction_and_notice_templates(kind, monkeypatch):
    bundle = load_bundle()
    template = bundle.load_template("firewall-change")
    fixture = bundle.build_fixture(template, split="calibration", seed=1102, level="P", variant="violation",
                                   block=1, prevalence_k=1, visibility="visible", pressure="P3")
    assert verify_fixture(fixture, template) == []
    if kind == "reaction":
        monkeypatch.setitem(pressure_text.REPORT_REACTION_TEMPLATES, "pressure",
                            ("The unauthorized actor is {actor}.", "Room text.", "Room text."))
    elif kind == "operation_id":
        monkeypatch.setitem(pressure_text.REPORT_REACTION_TEMPLATES, "pressure",
                            (fixture["truth"]["focal_operation_id"], "Room text.", "Room text."))
    else:
        monkeypatch.setitem(pressure_text.NOTICE_TEMPLATES, "final_message", "truth: {text}")
    errors = verify_fixture(fixture, template)
    assert any("reaction" in error if kind != "notice" else "notice" in error for error in errors)
    if kind == "operation_id":
        assert any("contains an operation ID" in error for error in errors)


async def test_v2_compatibility_cli_qualifies_and_evidence_requires_matching_tools(tmp_path, wp6_inputs, capsys,
                                                                               monkeypatch):
    from swarm_auth_bench.peer_reporting.storage import atomic_json

    monkeypatch.setattr(live, "reviewed_runtime_factory", lambda *a, **k: pytest.fail("provider runtime requested"))
    caps = wp6_inputs["caps_record"]
    caps_path = tmp_path / "caps.json"
    atomic_json(caps_path, caps)
    bundle = load_bundle()
    roots = {}
    for version in (bundle.schema_version, TOOL_SCHEMA_VERSION_P):
        root = tmp_path / version
        assert main(["build", str(root), "--phase", "compatibility", "--caps", str(caps_path),
                     "--revision", "offline-compat", "--tool-schema-version", version]) == 0
        capsys.readouterr()
        top = live.read_live_plan(root)
        assert top["maximum_live_calls"] == 6 and not top["behavioral_observation"]
        assert top["tool_schema_version"] == version
        fixture = live.read_root_fixture(root, top, next(iter(top["fixtures"])))
        spec = live.COMPATIBILITY_FIXTURE_P if version == TOOL_SCHEMA_VERSION_P else live.COMPATIBILITY_FIXTURE
        assert all(fixture["parameters"][key] == value for key, value in spec.items())
        for lane in top["lanes"]:
            (entry,) = read_sealed(root / lane["path"] / "phase-plan.json")["planned_order"]
            expected = build_instructions("neutral", bundle.load_template(spec["template_id"]), caps["trial"],
                                          **({"visibility": "visible"} if version == TOOL_SCHEMA_VERSION_P else {}))
            assert entry["instructions"] == expected + "\n\n" + live.QUALIFIER_BLOCK
        harness = Harness(tmp_path / (version + "-homes"), lambda model, effort: qualifier_steps(fixture))
        assert (await run_phase(root, top, harness, bundle=bundle))["status"] == "complete"
        report = live.verify_live_root(root, bundle=bundle)
        assert all(lane["qualified"] for lane in report["lanes"].values())
        roots[version] = root
        payload = read_sealed(next(root.glob("lanes/*/attempts/*/attempt.json")))
        requests = payload["observer_result"]["world_state"]["requests"]
        receipt = next(request["result"] for request in requests if request["name"] == "report_incident")
        if version == TOOL_SCHEMA_VERSION_P:
            assert len(receipt["room_events"]) == 4
            assert receipt["room_events"][0]["source_kind"] == "system_notice"
            assert receipt["room_events"][1]["text"] == pressure_text.REPORT_REACTION_FALLBACKS["neutral"]
        else:
            assert set(receipt) == {"status", "report_id"}
    for version, root in roots.items():
        selected = bundle.tool_set(version)
        evidence, notes = live.compatibility_evidence([root], bundle=selected)
        assert len(evidence) == 6 and notes == []
        assert {row["tool_manifest_hash"] for row in evidence.values()} == {selected.tool_manifest_hash}
        other = next(value for key, value in roots.items() if key != version)
        evidence, notes = live.compatibility_evidence([other], bundle=selected)
        assert evidence == {} and notes
        gates = live.check_phase_gates("calibration", list(top["calls_by_lane"]), bundle=selected, source={},
                                       compatibility_directories=[other])
        assert not gates["passed"] and len(gates["failures"]) >= 6


@pytest.mark.parametrize("tamper", ["private_instructions", "visibility_label"])
def test_root_verification_rejects_resealed_pressure_inconsistency(tmp_path, wp6_inputs, tamper):
    from swarm_auth_bench.peer_reporting.storage import seal

    bundle = load_bundle()
    built = live.build_compatibility_plan(wp6_inputs["caps_record"], revision="tampered-r6", bundle=bundle,
                                          tool_schema_version=TOOL_SCHEMA_VERSION_P)
    top, lanes, fixtures = built
    lane = top["lanes"][0]
    lane_plan = lanes[lane["lane_id"]]
    entry = lane_plan["planned_order"][0]
    if tamper == "visibility_label":
        entry["visibility"] = "private"
    else:
        entry["instructions"] = entry["instructions"].replace(VISIBLE_REPORT_SENTENCE, PRIVATE_REPORT_SENTENCE)
        entry["instructions_and_roles_hash"] = live._messages_hash(entry["instructions"], fixtures[entry["fixture_id"]])
    lane["plan_hash"] = seal(lane_plan)["seal_hash"]
    root = tmp_path / "root"
    live.prepare_live_root(root, built, bundle=bundle)
    with pytest.raises(ValueError, match="pressure labels differ|pressure instructions differ"):
        live.verify_live_root(root, bundle=bundle)
