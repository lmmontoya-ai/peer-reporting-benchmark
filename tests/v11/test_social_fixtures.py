"""Revision 5 construction and independent record checks, without model sessions."""

from collections import Counter
from copy import deepcopy

import pytest

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.peer_reporting.fixtures import field_differences
from swarm_auth_bench.peer_reporting_v11 import FIXTURE_VERSION
from swarm_auth_bench.peer_reporting_v11.config import (
    SOCIAL_FIELDS,
    load_protocol,
    social_fixture_cells,
)
from swarm_auth_bench.peer_reporting_v11.incidents import find_cue_words, load_all_templates, load_template
from swarm_auth_bench.peer_reporting_v11.ladder import (
    GRANT_RULES,
    SOCIAL_ENDORSEMENT_FRAMES,
    SOCIAL_MENTION_FRAMES,
    _hidden_labels,
    _record_operations,
    build_fixture,
    is_authorized,
    verify_fixture,
)

CELLS = [cell for arm in ("social_pilot_xhigh", "social_grid_xhigh", "social_anchor_xhigh")
         for cell in social_fixture_cells(arm)]
TEMPLATES = ("firewall-change", "budget-transfer")


def social_fixture(template_id="firewall-change", **overrides):
    arguments = {"split": "calibration", "seed": 1102, "level": "S", "variant": "violation",
                 "difficulty": "hard", "block": 1, "prevalence_k": 8, "post_condition": "endorse_8"}
    arguments.update(overrides)
    return build_fixture(load_template(template_id), **arguments)


def registry(fixture):
    return next(record for record in fixture["public"]["records"].values()
                if record["record_type"] == "principal_registry_export")


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("cell", CELLS)
def test_every_enumerated_social_fixture(template_id, cell):
    template = load_template(template_id)
    fixture = social_fixture(template_id, **cell)
    assert verify_fixture(fixture, template) == []
    p, public, truth = fixture["parameters"], fixture["public"], fixture["truth"]
    namespace = content_hash([FIXTURE_VERSION, template_id, "calibration", 1102, "social", p["block"]])[:10]
    master = [f"peer-{content_hash([namespace, index])[:8]}" for index in range(16)]
    display = sorted(master, key=lambda actor: content_hash(["roster", 1102, template_id, "calibration", actor]))
    assert public["roster"] == [{"agent_id": actor} for actor in display]
    assert [post["sender_id"] for post in public["messages"]] == display * 2
    assert len(fixture["fixed_replies"]) == 16
    assert truth["social"] | {key: p[key] for key in SOCIAL_FIELDS} == truth["social"]
    assert p["near_miss_type"] is p["compound_type"] is None
    assert public["policy"]["grant_rules"] == GRANT_RULES
    assert fixture["fetchable_records"] == {}
    assert fixture["audit"]["master_roster"] == master
    assert fixture["audit"]["display_roster"] == display
    assert fixture["audit"]["posts_per_peer"] == {actor: 2 for actor in master}
    assert fixture["audit"]["pinned_slots"] == {
        str(slot): actor for slot, actor in zip(truth["social"]["poster_slots"], truth["social"]["poster_ids"], strict=True)}
    assert fixture["audit"]["byte_ranges"]["focal_announcement"] is None
    operations = _record_operations(public["records"])
    assert len(operations) == 32
    assert Counter(op["actor_id"] for op in operations) == {actor: 2 for actor in master}
    assert all(len({op["artifact"]["artifact_id"] for op in operations if op["actor_id"] == actor}) == 2
               for actor in master)
    assert [op["operation_time"] for op in operations] == sorted({op["operation_time"] for op in operations})
    for index, size in enumerate((11, 11, 10)):
        request = public["records"][f"r-{namespace}-request-{index + 1}"]
        request_ops = [op for op in operations if op["artifact"] == request["artifact"]]
        assert len(request_ops) == size
        assert request_ops[0]["operation"] == template["operations"]["initiate"]["name"]
        assert request["stored_request"]["operation_id"] == request_ops[0]["operation_id"]
    unauthorized = [op for op in operations if not is_authorized(op, public["records"], public["policy"])]
    assert len(unauthorized) == len({op["actor_id"] for op in unauthorized}) == p["prevalence_k"]
    assert all(op["operation"] == template["operations"]["endorse"]["name"] for op in unauthorized)
    assert {op["operation_id"] for op in unauthorized} == set(truth["social"]["violation_operation_ids"])
    assert not set(truth["social"]["poster_ids"]) & set(truth["social"]["violator_ids"])
    assert all(op["operation_id"] not in post["text"] for op in operations for post in public["messages"])
    grants = registry(fixture)["grants"]
    decoy_ids = {f"grant-{content_hash([namespace, f'social-decoy-{index}'])[:12]}" for index in range(8)}
    if p["difficulty"] == "hard":
        for artifact in {op["artifact"]["artifact_id"] for op in operations}:
            groups = [grant for grant in grants if grant["artifact"]["artifact_id"] == artifact
                      and len(grant["actor_ids"]) > 1]
            assert sorted(len(grant["actor_ids"]) for grant in groups) == [2, 3, 4]
            assert all(len(grant["actor_ids"]) == len(set(grant["actor_ids"])) for grant in groups)
        assert {grant["grant_id"] for grant in grants} & decoy_ids == decoy_ids
    else:
        assert all(len(grant["actor_ids"]) == 1 for grant in grants)
        assert not {grant["grant_id"] for grant in grants} & decoy_ids
    if p["variant"] == "violation":
        parent = social_fixture(template_id, **(cell | {"variant": "twin", "prevalence_k": 0}))
        allowed = {f"/records/r-{namespace}-registry/grants"}
        assert truth["focal_operation_id"] in truth["unauthorized_operation_ids"]
        assert fixture["audit"]["twin_to_violation_byte_difference"] == (
            parent["audit"]["packet_bytes"] - fixture["audit"]["packet_bytes"])
    else:
        assert fixture["audit"]["twin_to_violation_byte_difference"] is None
        if p["post_condition"] == "none":
            assert fixture["parent_fixture_id"] is None
            return
        parent = social_fixture(template_id, **(cell | {"post_condition": "none"}))
        allowed = {f"/messages/{slot - 1}/text" for slot in truth["social"]["poster_slots"]}
    differences = field_differences(parent["public"], public)
    assert differences and {difference["path"] for difference in differences} <= allowed
    assert fixture["parent_fixture_id"] == parent["fixture_id"]
    assert fixture["parent_public_differences"] == differences
    assert fixture["fetchable_records"] == parent["fetchable_records"]


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("block", [0, 1, 2])
def test_nesting_and_identical_operations_across_difficulty_posts_and_variant(template_id, block):
    reference = social_fixture(template_id, block=block, prevalence_k=12, post_condition="none")
    sequence = reference["truth"]["social"]["violation_operation_ids"]
    roster = reference["public"]["roster"]
    operations = _record_operations(reference["public"]["records"])
    for difficulty in ("hard", "plain"):
        for post in ("none", "endorse_2", "endorse_8", "mention_8"):
            for k in (0, 1, 4, 8, 12):
                if k == 12 and post != "none":
                    continue
                fixture = social_fixture(template_id, block=block, difficulty=difficulty, post_condition=post,
                                         prevalence_k=k, variant="twin" if k == 0 else "violation")
                assert fixture["truth"]["social"]["violation_operation_ids"] == sequence[:k]
                assert fixture["truth"]["focal_operation_id"] == sequence[0]
                assert fixture["public"]["roster"] == roster
                assert _record_operations(fixture["public"]["records"]) == operations


@pytest.mark.parametrize("template_id", list(load_all_templates()))
@pytest.mark.parametrize("frame", SOCIAL_ENDORSEMENT_FRAMES + SOCIAL_MENTION_FRAMES)
def test_verbatim_frames_pass_cue_and_hidden_label_rules_for_every_template(template_id, frame):
    text = frame.format(request_noun=load_template(template_id)["request_noun"])
    assert find_cue_words(text) == []
    assert _hidden_labels({"public": {"messages": [{"text": text}]}}) == []


@pytest.mark.parametrize("overrides", [
    {"prevalence_k": 0}, {"prevalence_k": 2}, {"prevalence_k": 12}, {"prevalence_k": True},
    {"variant": "twin"}, {"variant": "benign"}, {"difficulty": "easy"}, {"difficulty": None},
    {"block": 3}, {"block": True}, {"block": None}, {"post_condition": "endorse_4"},
    {"near_miss_type": "wrong_digest"}, {"compound_type": "revoked"}, {"specification_revision": 4},
])
def test_disallowed_social_parameters(overrides):
    with pytest.raises(ValueError):
        social_fixture(**overrides)


@pytest.mark.parametrize("field", SOCIAL_FIELDS)
def test_social_parameters_are_rejected_at_existing_levels(field):
    with pytest.raises(ValueError, match="only at level S"):
        build_fixture(load_template("firewall-change"), split="calibration", seed=1102,
                      level="L1", variant="violation", **{field: social_fixture()["parameters"][field]})


def test_frames_are_copied_verbatim_from_normative_spec():
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[2] / "docs/v12/spec.md").read_text(encoding="utf-8")
    for frame in SOCIAL_ENDORSEMENT_FRAMES + SOCIAL_MENTION_FRAMES:
        assert frame in spec


def reseal(fixture):
    fixture["packet"] = canonical_json(fixture["public"])
    fixture["public_hash"] = content_hash(fixture["public"])
    fixture["truth_hash"] = content_hash(fixture["truth"])
    fixture["complete_world_hash"] = content_hash({key: fixture[key] for key in
                                                  ("public", "fetchable_records", "fixed_replies")})
    fixture["fixture_id"] = content_hash({key: fixture[key] for key in
                                         ("version", "parameters", "complete_world_hash", "truth_hash")})


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("mutation,diagnostic", [
    ("drop_restore", "authorization mismatch"), ("poster_violator", "posters must be disjoint"),
    ("frame", "post frame or routine text mismatch"), ("operation_post", "post names an operation ID"),
    ("plain_group", "plain has grouped grants"), ("grant_without_truth", "authorization mismatch"),
])
def test_resealed_social_mutations_fail(template_id, mutation, diagnostic):
    fixture = social_fixture(template_id, variant="twin", prevalence_k=0,
                             difficulty="plain" if mutation == "plain_group" else "hard")
    grants = registry(fixture)["grants"]
    restore_id = f"grant-{content_hash([fixture['truth']['focal_operation_id'].split('-')[1], 'social-restore-0'])[:12]}"
    restore = next(grant for grant in grants if grant["grant_id"] == restore_id)
    if mutation == "drop_restore":
        grants.remove(restore)
    elif mutation == "poster_violator":
        poster = fixture["truth"]["social"]["poster_ids"][0]
        operation = next(op for op in fixture["truth"]["operations"] if op["actor_id"] == poster
                         and op["operation"] == load_template(template_id)["operations"]["endorse"]["name"])
        for grant in grants:
            if grant["artifact"] == operation["artifact"] and grant["allowed_operations"] == [operation["operation"]]:
                grant["actor_ids"] = [actor for actor in grant["actor_ids"] if actor != poster]
        operation["authorized"] = False
        fixture["truth"]["social"].update(violator_ids=[poster], violation_operation_ids=[operation["operation_id"]])
        fixture["truth"]["unauthorized_operation_ids"] = [operation["operation_id"]]
        fixture["parameters"].update(variant="violation", prevalence_k=1)
    elif mutation in ("frame", "operation_post"):
        slot = fixture["truth"]["social"]["poster_slots"][0]
        fixture["public"]["messages"][slot - 1]["text"] = (
            "I looked at the entries." if mutation == "frame" else fixture["truth"]["focal_operation_id"])
    elif mutation == "plain_group":
        grouped = deepcopy(restore)
        grouped["grant_id"] = "grant-added"
        grouped["actor_ids"].append(next(actor for actor in fixture["fixed_replies"] if actor not in grouped["actor_ids"]))
        grants.append(grouped)
    else:
        restore["validity_interval"]["end"] = load_template(template_id)["date"] + "T09:59:59Z"
    reseal(fixture)
    assert any(diagnostic in error for error in verify_fixture(fixture, load_template(template_id)))


def test_unused_generator_fields_keep_protocol_values():
    fixture, protocol = social_fixture(), load_protocol()
    assert fixture["parameters"]["near_miss_types"] == protocol["near_miss_types"]
    for field in ("distractor_grants", "companion_slot_offset", "pressure_slot_offsets"):
        assert fixture["parameters"][field] == protocol["generator_parameters"][field]
