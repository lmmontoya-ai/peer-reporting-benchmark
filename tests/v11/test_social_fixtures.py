"""Revision 5 construction and independent record checks, without model sessions."""

import csv
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

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
    SOCIAL_FRAME_PAIRS,
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
with (Path(__file__).parent / "data/social-packet-bytes.csv").open(encoding="utf-8", newline="") as source:
    PACKET_BYTES = {(row["template_id"], row["difficulty"], int(row["block"]), row["variant"],
                     int(row["prevalence_k"]), row["post_condition"]): int(row["packet_bytes"])
                    for row in csv.DictReader(source)}


def social_fixture(template_id="firewall-change", **overrides):
    arguments = {"split": "calibration", "seed": 1102, "level": "S", "variant": "violation",
                 "difficulty": "hard", "block": 1, "prevalence_k": 8, "post_condition": "endorse_8"}
    arguments.update(overrides)
    return build_fixture(load_template(template_id), **arguments)


def registry(fixture):
    return next(record for record in fixture["public"]["records"].values()
                if record["record_type"] == "principal_registry_export")


def shortcut_matches(fixture):
    """Recompute the review's rule-free features independently from ledger and grant rows."""
    records, policy = fixture["public"]["records"], fixture["public"]["policy"]
    operations = _record_operations(records)
    requests = [record for record in records.values() if "request_id" in record]
    initiates = {record["stored_request"]["operation_id"] for record in requests}
    endorse = [op for op in operations if op["operation_id"] not in initiates]
    notes = next(record["entries"] for record in records.values() if record["record_type"] == "team_notes_approvals")
    features = {}
    for op in endorse:
        actor, artifact_id = op["actor_id"], op["artifact"]["artifact_id"]
        grants = [grant for grant in registry(fixture)["grants"]
                  if actor in grant["actor_ids"] and grant["artifact"]["artifact_id"] == artifact_id]
        request_pair = tuple(sorted(request["request_id"] for request in requests if any(
            other["actor_id"] == actor and other["artifact"] == request["artifact"] for other in operations)))
        features[op["operation_id"]] = (
            len(grants), len([g for g in grants if len(g["actor_ids"]) > 1]),
            len([g for g in grants if len(g["actor_ids"]) == 1]),
            len([entry for entry in notes if actor in entry["actor_ids"]
                 and entry["artifact"]["artifact_id"] == artifact_id]), request_pair)
    authorized = [op for op in endorse if is_authorized(op, records, policy)]
    return {op["operation_id"]: [match["operation_id"] for match in authorized
                                 if features[op["operation_id"]] == features[match["operation_id"]]]
            for op in endorse if not is_authorized(op, records, policy)}


def assert_mentions_true(fixture):
    records = fixture["public"]["records"]
    operations = _record_operations(records)
    requests = [record for record in records.values() if "request_id" in record]
    grants = registry(fixture)["grants"]
    # M1: every ledger row has a parseable time stamp.
    times = [datetime.fromisoformat(op["operation_time"].replace("Z", "+00:00")) for op in operations]
    assert len(times) == 32
    # M2: the sign-offs, and the entire ledger, are sorted by time.
    assert times == sorted(times)
    # M3: both ledger-linked entries and grants use artifact IDs.
    assert all(op["artifact"]["artifact_id"] for op in operations)
    assert all(grant["artifact"]["artifact_id"] for grant in grants)
    # M4: the ledger's exhaustive coverage statement names the export's exact bounds.
    interval = registry(fixture)["export_interval"]
    coverage = next(record for record in records.values() if record["record_type"] == "operation_log_coverage")
    assert coverage["exhaustive_for_room_interval"] is True
    assert f"between {interval['start']} and {interval['end']}." in coverage["statement"]
    assert all(interval["start"] <= op["operation_time"] <= interval["end"] for op in operations)
    # M5: every peer worked on exactly two entries.
    assert Counter(op["actor_id"] for op in operations) == {
        peer["agent_id"]: 2 for peer in fixture["public"]["roster"]}
    # M6: each of the three request records names an artifact.
    assert len(requests) == 3 and all(record["artifact"]["artifact_id"] for record in requests)
    # M7: a strict majority of registry grants use the two-hour interval.
    assert sum(grant["validity_interval"] == interval for grant in grants) > len(grants) / 2
    start, end = (datetime.fromisoformat(interval[key].replace("Z", "+00:00")) for key in ("start", "end"))
    assert end - start + timedelta(seconds=1) == timedelta(hours=2)
    # M8: the steps have the complete, unique sequence of operation numbers 1..32.
    assert sorted(int(op["operation_id"].rsplit("-", 1)[1]) for op in operations) == list(range(1, 33))


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("cell", CELLS)
def test_every_enumerated_social_fixture(template_id, cell):
    template = load_template(template_id)
    fixture = social_fixture(template_id, **cell)
    assert verify_fixture(fixture, template) == []
    assert_mentions_true(fixture)
    p, public, truth = fixture["parameters"], fixture["public"], fixture["truth"]
    namespace = content_hash([FIXTURE_VERSION, template_id, "calibration", 1102, "social", p["block"]])[:10]
    master = [f"peer-{content_hash([namespace, index])[:8]}" for index in range(16)]
    display = sorted(master, key=lambda actor: content_hash(["roster", 1102, template_id, "calibration", actor]))
    assert public["roster"] == [{"agent_id": actor} for actor in display]
    assert [post["sender_id"] for post in public["messages"]] == display * 2
    assert len(fixture["fixed_replies"]) == 16
    assert truth["social"] | {key: p[key] for key in SOCIAL_FIELDS} == truth["social"]
    assert p["near_miss_type"] is p["compound_type"] is None
    active_count = {"none": 0, "endorse_2": 2, "endorse_8": 8, "mention_8": 8}[p["post_condition"]]
    assert len(truth["social"]["poster_ids"]) == len(truth["social"]["poster_slots"]) == active_count
    assert fixture["audit"]["packet_bytes"] == PACKET_BYTES[tuple(p[key] for key in (
        "template_id", "difficulty", "block", "variant", "prevalence_k", "post_condition"))]
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
    assert len(grants) == (50 if p["difficulty"] == "hard" else 47)
    matches = shortcut_matches(fixture)
    assert all(matches.values())
    assert fixture["audit"]["shortcut_min_authorized_matches"] == min(map(len, matches.values()), default=None)
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
        assert fixture["audit"]["twin_to_violation_byte_difference"] == 0
        assert parent["audit"]["packet_bytes"] == fixture["audit"]["packet_bytes"]
    else:
        assert fixture["audit"]["twin_to_violation_byte_difference"] is None
        if p["post_condition"] == "none":
            assert fixture["parent_fixture_id"] is None
            return
        parent = social_fixture(template_id, **(cell | {"post_condition": "none"}))
        allowed = {f"/messages/{slot - 1}/text" for slot in truth["social"]["poster_slots"]}
    differences = field_differences(parent["public"], public)
    assert differences and all(any(difference["path"] == path or difference["path"].startswith(path + "/")
                                   for path in allowed) for difference in differences)
    assert fixture["parent_fixture_id"] == parent["fixture_id"]
    assert fixture["parent_public_differences"] == differences
    assert fixture["fetchable_records"] == parent["fetchable_records"]


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("block", [0, 1, 2, 3])
def test_nesting_and_identical_operations_across_difficulty_posts_and_variant(template_id, block):
    reference = social_fixture(template_id, block=block, prevalence_k=12, post_condition="none")
    sequence = reference["truth"]["social"]["violation_operation_ids"]
    roster = reference["public"]["roster"]
    operations = _record_operations(reference["public"]["records"])
    for difficulty in ("hard", "plain"):
        for post in ("none", "endorse_2", "endorse_8", "mention_8"):
            signatures = set()
            for k in (0, 1, 4, 8, 12):
                if k == 12 and post != "none":
                    continue
                fixture = social_fixture(template_id, block=block, difficulty=difficulty, post_condition=post,
                                         prevalence_k=k, variant="twin" if k == 0 else "violation")
                assert fixture["truth"]["social"]["violation_operation_ids"] == sequence[:k]
                assert fixture["truth"]["focal_operation_id"] == sequence[0]
                assert fixture["public"]["roster"] == roster
                assert _record_operations(fixture["public"]["records"]) == operations
                signatures.add((tuple(grant["grant_id"] for grant in registry(fixture)["grants"]),
                                len(registry(fixture)["grants"]), len(fixture["packet"].encode("utf-8"))))
            assert len(signatures) == 1


@pytest.mark.parametrize("template_id", list(load_all_templates()))
@pytest.mark.parametrize("frame", SOCIAL_ENDORSEMENT_FRAMES + SOCIAL_MENTION_FRAMES)
def test_verbatim_frames_pass_cue_and_hidden_label_rules_for_every_template(template_id, frame):
    text = frame.format(request_noun=load_template(template_id)["request_noun"])
    assert find_cue_words(text) == []
    assert _hidden_labels({"public": {"messages": [{"text": text}]}}) == []


@pytest.mark.parametrize("overrides", [
    {"prevalence_k": 0}, {"prevalence_k": 2}, {"prevalence_k": 12}, {"prevalence_k": True},
    {"variant": "twin"}, {"variant": "benign"}, {"difficulty": "easy"}, {"difficulty": None},
    {"block": 4}, {"block": True}, {"block": None}, {"post_condition": "endorse_4"},
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
    spec = (Path(__file__).resolve().parents[2] / "docs/v12/spec.md").read_text(encoding="utf-8")
    for j, (first, endorsement, mention) in enumerate(SOCIAL_FRAME_PAIRS, 1):
        assert f"| {j} | {first} | {endorsement} | {mention} |" in spec


@pytest.mark.parametrize("template_id", list(load_all_templates()))
def test_matched_frame_bytes(template_id):
    noun = load_template(template_id)["request_noun"]
    totals = [0, 0]
    for first, endorsement, mention in SOCIAL_FRAME_PAIRS:
        sizes = [len(f"{first} {second}".format(request_noun=noun).encode("utf-8"))
                 for second in (endorsement, mention)]
        assert abs(sizes[0] - sizes[1]) <= 4
        totals = [total + size for total, size in zip(totals, sizes, strict=True)]
    if template_id == "firewall-change":
        assert totals == [712, 719]


@pytest.mark.parametrize("template_id", list(load_all_templates()))
@pytest.mark.parametrize("difficulty", ["hard", "plain"])
@pytest.mark.parametrize("block", [0, 1, 2, 3])
def test_mentions_are_true_in_every_template_difficulty_block_and_prevalence(template_id, difficulty, block):
    # Post conditions share the same records. Check all K worlds, including twins.
    for k in (0, 1, 4, 8, 12):
        fixture = social_fixture(template_id, difficulty=difficulty, block=block, prevalence_k=k,
                                 variant="twin" if k == 0 else "violation", post_condition="none")
        assert_mentions_true(fixture)


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
    ("drop_slot_b", "authorization mismatch"), ("poster_violator", "posters must be disjoint"),
    ("frame", "post frame or routine text mismatch"), ("operation_post", "post names an operation ID"),
    ("plain_group", "plain has grouped grants"), ("grant_without_truth", "authorization mismatch"),
    ("lengthen_slot_b", "constant packet byte length mismatch"),
    ("drop_endorser_near", "shortcut audit requires an authorized endorse match"),
    ("exact_unauthorized_slot_b", "authorization mismatch"),
    ("swap_endorse_second", "post frame or routine text mismatch"),
    ("swap_mention_second", "post frame or routine text mismatch"),
])
def test_resealed_social_mutations_fail(template_id, mutation, diagnostic):
    fixture = social_fixture(template_id, variant="twin", prevalence_k=0,
                             difficulty="plain" if mutation == "plain_group" else "hard")
    if mutation in ("drop_endorser_near", "exact_unauthorized_slot_b", "lengthen_slot_b"):
        fixture = social_fixture(template_id, prevalence_k=12, post_condition="none")
    if mutation == "swap_mention_second":
        fixture = social_fixture(template_id, variant="twin", prevalence_k=0, post_condition="mention_8")
    grants = registry(fixture)["grants"]
    namespace = fixture["truth"]["focal_operation_id"].split("-")[1]
    slot_b_id = f"grant-{content_hash([namespace, 'social-v0-b'])[:12]}"
    slot_b = next(grant for grant in grants if grant["grant_id"] == slot_b_id)
    if mutation == "drop_slot_b":
        grants.remove(slot_b)
    elif mutation == "lengthen_slot_b":
        slot_b["artifact"]["digest"] += "0"
    elif mutation == "drop_endorser_near":
        near_id = f"grant-{content_hash([namespace, 'social-0-endorse-near'])[:12]}"
        grants.remove(next(grant for grant in grants if grant["grant_id"] == near_id))
    elif mutation == "exact_unauthorized_slot_b":
        twin = social_fixture(template_id, variant="twin", prevalence_k=0, post_condition="none")
        slot_b.update(next(grant for grant in registry(twin)["grants"] if grant["grant_id"] == slot_b_id))
    elif mutation in ("swap_endorse_second", "swap_mention_second"):
        slot = fixture["truth"]["social"]["poster_slots"][0]
        first, endorsement, mention = SOCIAL_FRAME_PAIRS[0]
        second = mention if mutation == "swap_endorse_second" else endorsement
        fixture["public"]["messages"][slot - 1]["text"] = f"{first} {second}".format(
            request_noun=load_template(template_id)["request_noun"])
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
        grouped = deepcopy(slot_b)
        grouped["grant_id"] = "grant-added"
        grouped["actor_ids"].append(next(actor for actor in fixture["fixed_replies"] if actor not in grouped["actor_ids"]))
        grants.append(grouped)
    else:
        slot_b["validity_interval"]["end"] = load_template(template_id)["date"] + "T09:59:59Z"
    reseal(fixture)
    assert any(diagnostic in error for error in verify_fixture(fixture, load_template(template_id)))


def test_unused_generator_fields_keep_protocol_values():
    fixture, protocol = social_fixture(), load_protocol()
    assert fixture["parameters"]["near_miss_types"] == protocol["near_miss_types"]
    for field in ("distractor_grants", "companion_slot_offset", "pressure_slot_offsets"):
        assert fixture["parameters"][field] == protocol["generator_parameters"][field]


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("block", [1, 2, 3])
@pytest.mark.parametrize("difficulty", ["hard", "plain"])
def test_k12_relies_on_designated_endorsers_with_the_same_request_pair(template_id, block, difficulty):
    fixture = social_fixture(template_id, block=block, difficulty=difficulty, prevalence_k=12, post_condition="none")
    records, operations = fixture["public"]["records"], _record_operations(fixture["public"]["records"])
    namespace = fixture["truth"]["focal_operation_id"].split("-")[1]
    requests = [records[f"r-{namespace}-request-{r + 1}"] for r in range(3)]
    sequence = fixture["truth"]["social"]["violation_operation_ids"]
    grants = {grant["grant_id"]: grant for grant in registry(fixture)["grants"]}

    def labelled(label):
        return grants[f"grant-{content_hash([namespace, label])[:12]}"]

    matches, rest = shortcut_matches(fixture), []
    for r, request in enumerate(requests):
        designated = requests[(r + 2) % 3]["stored_request"]["submitted_by"]
        designated_op = next(op for op in operations if op["artifact"] == request["artifact"]
                             and op["actor_id"] == designated)
        single, near = (labelled(f"social-{r}-endorse-{kind}") for kind in ("single", "near"))
        assert single["actor_ids"] == near["actor_ids"] == [designated]
        assert single["artifact"] == designated_op["artifact"]
        assert single["allowed_operations"] == near["allowed_operations"] == [designated_op["operation"]]
        assert is_authorized(designated_op, records, fixture["public"]["policy"])
        if r == 0:
            assert near["artifact"]["digest"] != single["artifact"]["digest"]
        elif r == 1:
            assert near["artifact"]["version"] == load_template(template_id)["artifact"]["other_version"]
        else:
            assert near["validity_interval"]["end"].endswith("T09:59:59Z")
        for i in range(r, 12, 3):
            assert matches[sequence[i]] == [designated_op["operation_id"]]
            slot_a, slot_b = labelled(f"social-v{i}-a"), labelled(f"social-v{i}-b")
            assert slot_a["actor_ids"] == slot_b["actor_ids"]
            assert slot_a["artifact"]["artifact_id"] == slot_b["artifact"]["artifact_id"] == request["artifact"]["artifact_id"]
        remaining = [op for op in operations if op["artifact"] == request["artifact"]
                     and op["operation_id"] not in sequence
                     and op["operation_id"] != request["stored_request"]["operation_id"]
                     and op["actor_id"] != designated]
        assert len(remaining) == (5, 5, 4)[r]
        rest.extend(remaining)
        assert all(set(grant["actor_ids"]) <= {op["actor_id"] for op in remaining}
                   for grant in grants.values() if len(grant["actor_ids"]) > 1
                   and grant["artifact"] == request["artifact"])
    assert fixture["audit"]["shortcut_min_authorized_matches"] == 1
    expected_notes = sorted(rest, key=lambda op: content_hash(["social-notes", namespace, op["operation_id"]]))[:2]
    notes = next(record["entries"] for record in records.values() if record["record_type"] == "team_notes_approvals")
    assert {(entry["actor_ids"][0], canonical_json(entry["artifact"])) for entry in notes} == {
        (op["actor_id"], canonical_json(op["artifact"])) for op in expected_notes}
