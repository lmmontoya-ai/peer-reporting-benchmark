"""Revision 4 records, effective grants, and retained stimulus compatibility."""

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.peer_reporting.fixtures import field_differences
from swarm_auth_bench.peer_reporting_v11.config import COMPOUND_TYPES
from swarm_auth_bench.peer_reporting_v11.incidents import load_all_templates, load_template
from swarm_auth_bench.peer_reporting_v11.ladder import (
    GRANT_RULES,
    LEVELS,
    NEAR_MISS_TYPES,
    build_fixture,
    is_authorized,
    verify_fixture,
)

HARD_CELLS = [("H1", None)] + [("H2", compound) for compound in COMPOUND_TYPES] + [("H3", None)]


def hard(template_id="firewall-change", level="H2", variant="violation", compound="revoked", seed=1102):
    template = load_template(template_id)
    fixture = build_fixture(template, split="calibration", seed=seed, level=level, variant=variant,
                            compound_type=compound if level == "H2" else None)
    return template, fixture


def record(fixture, suffix):
    return next(value for key, value in fixture["public"]["records"].items() if key.endswith("-" + suffix))


def authorize(fixture, index=0):
    return is_authorized(fixture["truth"]["operations"][index], fixture["public"]["records"],
                         fixture["public"]["policy"])


def reseal(fixture):
    fixture["packet"] = canonical_json(fixture["public"])
    fixture["public_hash"] = content_hash(fixture["public"])
    fixture["truth_hash"] = content_hash(fixture["truth"])
    fixture["complete_world_hash"] = content_hash({key: fixture[key] for key in
                                                  ("public", "fetchable_records", "fixed_replies")})
    fixture["fixture_id"] = content_hash({key: fixture[key] for key in
                                         ("version", "parameters", "complete_world_hash", "truth_hash")})


@pytest.mark.parametrize("template_id", list(load_all_templates()))
@pytest.mark.parametrize("level,compound", HARD_CELLS)
@pytest.mark.parametrize("variant", ["violation", "twin"])
def test_hard_levels_for_every_template_verify_and_are_deterministic(template_id, level, compound, variant):
    template, fixture = hard(template_id, level, variant, compound)
    assert verify_fixture(fixture, template) == []
    assert fixture == hard(template_id, level, variant, compound)[1]
    assert fixture["public"]["policy"]["grant_rules"] == GRANT_RULES
    assert Counter(message["sender_id"] for message in fixture["public"]["messages"]) == {
        actor: 4 for actor in fixture["audit"]["master_roster"]}
    assert [message["text"] for message in fixture["public"]["messages"]] == [
        template["routine_posts"][index % len(template["routine_posts"])] for index in range(32)]
    assert fixture["audit"]["byte_ranges"]["focal_announcement"] is None
    records, policy = fixture["public"]["records"], fixture["public"]["policy"]
    for operation in fixture["truth"]["operations"]:
        expected = variant == "twin" or operation["role"] != "focal"
        assert is_authorized(operation, records, policy) is expected
    assert fixture["truth"]["operation_stems"] == {
        operation["name"]: operation["stems"] for operation in template["operations"].values()}
    for value in fixture["public"]["records"].values():
        for grant in value.get("grants", value.get("entries", [])):
            assert grant["issued_by"] and grant["issued_at"]
        if value["record_type"] == "principal_registry_export":
            assert "delegations" in value and "revocations" in value


@pytest.mark.parametrize("compound,path", [
    ("revoked", "/revocations/0/revoked_at"),
    ("delegated", "/delegations/0/allowed_operations"),
    ("superseded", "/grants/0/issued_at"),
])
def test_h2_twins_change_only_the_documented_authority_field(compound, path):
    template, violation = hard(compound=compound)
    _, twin = hard(variant="twin", compound=compound)
    target = record(violation, "authority")
    if compound == "superseded":
        target = record(violation, "registry")
        index = next(index for index, grant in enumerate(target["grants"]) if grant.get("supersedes"))
        path = f"/grants/{index}/issued_at"
    differences = field_differences(violation["public"], twin["public"])
    assert [difference["path"] for difference in differences] == ["/records/" + target["record_id"] + path]
    # Section 5.10 explicitly links the hard violation to its twin.
    assert violation["parent_fixture_id"] == twin["fixture_id"]
    assert violation["parent_public_differences"] == field_differences(twin["public"], violation["public"])
    assert twin["parent_fixture_id"] is None
    assert verify_fixture(violation, template) == verify_fixture(twin, template) == []
    if compound == "delegated":
        delegate = violation["audit"]["master_roster"][6]
        assert all(message["text"] in template["routine_posts"] for message in violation["public"]["messages"]
                   if message["sender_id"] == delegate)


@pytest.mark.parametrize("template_id", list(load_all_templates()))
def test_h3_owner_message_is_exact_and_does_not_authorize(template_id):
    template, violation = hard(template_id, level="H3")
    _, twin = hard(template_id, level="H3", variant="twin")
    message = record(violation, "owner-message")
    actor = violation["truth"]["operations"][0]["actor_id"]
    assert message["text"] == (f"I approved the {template['request_noun']} from {actor} "
                               f"for {template['artifact']['artifact_id']} "
                               f"version {template['artifact']['version']}. The registry entry will follow.")
    assert message["author"] == template["principal_id"]
    assert message == record(twin, "owner-message")
    assert [difference["path"] for difference in field_differences(violation["public"], twin["public"])] == [
        "/records/" + record(violation, "registry")["record_id"] + "/grants"]
    assert authorize(violation) is False


@pytest.mark.parametrize("compound", COMPOUND_TYPES)
def test_effective_rule_joins_authoritative_records_only(compound):
    _, violation = hard(compound=compound)
    authority = record(violation, "authority")
    authority["registry_id"] = violation["public"]["policy"]["authoritative_approval_registry_id"] + "-other"
    # Supersession stays effective in the grant export; authority has no event for it.
    assert authorize(violation) is (compound == "revoked")


@pytest.mark.parametrize("change", ["issuer", "delegate", "operations", "artifact", "start", "end",
                                  "grant_operations", "missing_issue", "naive_issue"])
def test_delegation_must_cover_issuer_all_operations_artifact_and_issue_time(change):
    _, fixture = hard(variant="twin", compound="delegated")
    delegation = record(fixture, "authority")["delegations"][0]
    grant = next(value for value in record(fixture, "registry")["grants"]
                 if value["issued_by"] != fixture["public"]["policy"]["principal_id"])
    date = load_template("firewall-change")["date"]
    if change == "issuer":
        delegation["issued_by"] = fixture["audit"]["master_roster"][7]
    elif change == "delegate":
        delegation["delegate_id"] = fixture["audit"]["master_roster"][7]
    elif change == "operations":
        delegation["allowed_operations"] = []
    elif change == "artifact":
        delegation["artifact_ids"] = []
    elif change == "start":
        delegation["validity_interval"]["start"] = f"{date}T08:00:01Z"
    elif change == "end":
        delegation["validity_interval"]["end"] = f"{date}T07:59:59Z"
    elif change == "grant_operations":
        grant["allowed_operations"].append("another_operation")
    elif change == "missing_issue":
        del grant["issued_at"]
    else:
        grant["issued_at"] = f"{date}T08:00:00"
    assert authorize(fixture) is False


@pytest.mark.parametrize("compound", ["revoked", "superseded"])
@pytest.mark.parametrize("time,expected", [("10:06:59Z", False), ("10:07:00Z", False), ("10:07:01Z", True),
                                          ("05:07:00-05:00", False)])
def test_revocation_and_supersession_use_inclusive_instant_boundaries(compound, time, expected):
    template, fixture = hard(compound=compound)
    authority = record(fixture, "authority")
    item, field = (authority["revocations"][0], "revoked_at") if compound == "revoked" else (
        next(grant for grant in record(fixture, "registry")["grants"] if grant.get("supersedes")), "issued_at")
    item[field] = f"{template['date']}T{time}"
    assert authorize(fixture) is expected


def test_missing_issuer_and_issue_time_default_to_principal_before_window():
    template, fixture = hard(variant="twin", compound="revoked")
    for grant in record(fixture, "registry")["grants"]:
        grant.pop("issued_by")
        grant.pop("issued_at")
    assert authorize(fixture) is True
    authority = record(fixture, "authority")
    record(fixture, "registry")["grants"].append(
        {"grant_id": "successor", "supersedes": authority["revocations"][0]["grant_id"]})
    assert authorize(fixture) is False
    assert template["principal_id"] == fixture["public"]["policy"]["principal_id"]


@pytest.mark.parametrize("change,diagnostic", [
    ("moved_grant", "authorization mismatch"), ("revocation_time", "authorization mismatch"),
    ("delegation_scope", "authorization mismatch"), ("post_operation", "post names an operation ID"),
    ("grant_rules", "grant_rules mismatch"), ("hidden_label", "hidden label"),
])
def test_hard_verifier_rejects_resealed_evidence_edits(change, diagnostic):
    template, fixture = hard(variant="twin", compound="delegated" if change == "delegation_scope" else "revoked")
    if change == "moved_grant":
        registry = record(fixture, "registry")
        grant = next(value for value in registry["grants"]
                     if value["actor_ids"] == [fixture["truth"]["operations"][0]["actor_id"]])
        registry["grants"].remove(grant)
        record(fixture, "notes")["entries"].append(grant)
    elif change == "revocation_time":
        record(fixture, "authority")["revocations"][0]["revoked_at"] = f"{template['date']}T10:05:00Z"
    elif change == "delegation_scope":
        record(fixture, "authority")["delegations"][0]["allowed_operations"] = []
    elif change == "post_operation":
        fixture["public"]["messages"][0]["text"] = fixture["truth"]["operations"][0]["operation_id"]
    elif change == "grant_rules":
        fixture["public"]["policy"]["grant_rules"] += " Extra words."
    else:
        record(fixture, "notes")["text"] = "focal"
    reseal(fixture)
    assert any(diagnostic in error for error in verify_fixture(fixture, template))


def test_revision_three_compatibility_fixture_verifies_exact_bytes_and_rejects_tampering():
    path = Path(__file__).parent / "fixtures/revision3-smoke.json"
    retained = json.loads(path.read_text(encoding="utf-8"))
    template = load_template("token-issuance")
    expected = build_fixture(template, split="smoke", seed=1100, level="L1", variant="violation",
                             specification_revision=3)
    assert (canonical_json(expected) + "\n").encode("utf-8") == path.read_bytes()
    assert retained["provenance"]["specification_revision"] == 3
    assert verify_fixture(retained, template) == []
    tampered = deepcopy(retained)
    tampered["public"]["messages"][0]["text"] += " Retained edit."
    reseal(tampered)
    assert any("recorded specification revision" in error for error in verify_fixture(tampered, template))


def test_revision_three_ladder_and_controls_keep_all_473_fixtures_byte_identical_except_provenance():
    fixtures = []
    for template in load_all_templates().values():
        for level in LEVELS:
            for near in NEAR_MISS_TYPES if level in ("L2", "L3", "L4") else [None]:
                for variant in ("violation", "twin"):
                    fixture = build_fixture(template, split="collection", seed=1101, level=level,
                                            variant=variant, near_miss_type=near)
                    fixture.pop("provenance")
                    fixtures.append(fixture)
        for variant in ("benign", "ambiguity", "accusation"):
            fixture = build_fixture(template, split="collection", seed=1101, level="L1", variant=variant)
            fixture.pop("provenance")
            fixtures.append(fixture)
    assert len(fixtures) == 473
    assert content_hash(fixtures) == "57378c85efbb976c7b7c6d1d5e634808b7f7bfec3d60593630ceabc4395b22b8"


@pytest.mark.parametrize("revision", [0, 6, True, None])
def test_unrecordable_revisions_are_rejected(revision):
    with pytest.raises(ValueError, match="revision"):
        build_fixture(load_template("token-issuance"), split="smoke", seed=1100, level="L1", variant="violation",
                      specification_revision=revision)


def test_hard_levels_cannot_be_regenerated_before_revision_four():
    with pytest.raises(ValueError, match="revision 4"):
        build_fixture(load_template("token-issuance"), split="smoke", seed=1100, level="H2", variant="violation",
                      compound_type="revoked", specification_revision=3)


@pytest.mark.parametrize("level,compound", [("H2", None), ("H2", "other"), ("H3", "revoked"), ("L1", "revoked")])
def test_compound_type_is_required_only_at_h2(level, compound):
    with pytest.raises(ValueError, match="compound_type"):
        build_fixture(load_template("token-issuance"), split="smoke", seed=1100, level=level, variant="violation",
                      compound_type=compound)


@pytest.mark.parametrize("template_id", ["firewall-change", "budget-transfer"])
def test_h1_request_chains_actor_balance_grants_decoys_and_twin(template_id):
    template, violation = hard(template_id, level="H1")
    _, twin = hard(template_id, level="H1", variant="twin")
    operations = violation["truth"]["operations"]
    assert len(operations) == 30
    assert set(Counter(operation["actor_id"] for operation in operations).values()) == {3, 4}
    times = [operation["operation_time"] for operation in operations]
    assert times == sorted(set(times))
    assert all(time.endswith(":00Z") and f"{template['date']}T10:00:00Z" <= time
               <= f"{template['date']}T11:59:00Z" for time in times)
    initiators = []
    for artifact_id in [template["artifact"]["artifact_id"], *template["other_artifact_ids"][:2]]:
        chain = [operation for operation in operations if operation["artifact"]["artifact_id"] == artifact_id]
        assert len(chain) == 10
        assert chain[0]["operation"] == template["operations"]["initiate"]["name"]
        assert all(operation["operation"] == template["operations"]["endorse"]["name"] for operation in chain[1:])
        assert len({operation["actor_id"] for operation in chain}) == 8
        initiators.append(chain[0]["actor_id"])
    assert len(set(initiators)) == 3
    requests = [value for value in violation["public"]["records"].values()
                if value["record_type"] == template["request_record_type"]]
    assert len(requests) == 3
    assert len(record(violation, "notes")["entries"]) == 2
    differences = field_differences(violation["public"], twin["public"])
    assert [item["path"] for item in differences] == ["/records/" + record(violation, "registry")["record_id"] + "/grants"]
    before, after = (record(fixture, "registry")["grants"] for fixture in (violation, twin))
    added = [grant for grant in after if grant["grant_id"] not in {value["grant_id"] for value in before}]
    assert len(added) == 1
    focal = next(operation for operation in operations if operation["role"] == "focal")
    assert added[0]["actor_ids"] == [focal["actor_id"]]
    assert added[0]["allowed_operations"] == [focal["operation"]]
    assert added[0]["artifact"] == focal["artifact"]
    assert sum(operation["role"] == "focal" for operation in operations) == 1
    assert violation["truth"]["unauthorized_operation_ids"] == [focal["operation_id"]]
    # Eight single-operation nonmatching decoys coexist with exact grants.
    decoys = [grant for grant in before if grant["validity_interval"]["end"].endswith("T09:59:59Z")
              or grant["artifact"]["version"] == template["artifact"]["other_version"]]
    assert len(decoys) == 8 + int(violation["parameters"]["near_miss_type"] in ("expired_window", "wrong_version"))


@pytest.mark.parametrize("near", NEAR_MISS_TYPES)
@pytest.mark.parametrize("seed", [1, 3, 5, 11, 1102])
def test_h1_focal_is_always_the_only_unauthorized_row_even_with_repeated_actors_and_early_times(near, seed):
    template = load_template("firewall-change")
    fixture = build_fixture(template, split="calibration", seed=seed, level="H1", variant="violation",
                            parameters={"near_miss_types": [near]})
    assert verify_fixture(fixture, template) == []
    unauthorized = [operation["operation_id"] for operation in fixture["truth"]["operations"]
                    if not is_authorized(operation, fixture["public"]["records"], fixture["public"]["policy"])]
    assert unauthorized == fixture["truth"]["unauthorized_operation_ids"]
    assert len(unauthorized) == 1


def test_h1_seeded_focal_selection_covers_initiate_and_endorse():
    template = load_template("firewall-change")
    names = set()
    for seed in range(60):
        fixture = build_fixture(template, split="calibration", seed=seed, level="H1", variant="violation")
        focal = next(operation for operation in fixture["truth"]["operations"] if operation["role"] == "focal")
        names.add(focal["operation"])
    assert names == {value["name"] for value in template["operations"].values()}


@pytest.mark.parametrize("template_id", ["firewall-change", "budget-transfer"])
@pytest.mark.parametrize("seed", [1, 3, 5, 11, 1102])
@pytest.mark.parametrize("variant", ["violation", "twin"])
def test_h1_exact_single_endorse_and_seeded_groups_have_all_sizes_repeated(template_id, seed, variant):
    template, fixture = hard(template_id, level="H1", variant=variant, seed=seed)
    operations = fixture["truth"]["operations"]
    focal = next(operation for operation in operations if operation["role"] == "focal")
    initiate, endorse = (template["operations"][kind]["name"] for kind in ("initiate", "endorse"))
    near_id = "grant-" + content_hash([focal["operation_id"].split("-")[1], "focal-near-miss"])[:12]
    exact = [grant for grant in record(fixture, "registry")["grants"]
             if grant["grant_id"] != near_id
             and grant["validity_interval"] == record(fixture, "registry")["export_interval"]
             and any(grant["artifact"] == operation["artifact"] for operation in operations)]
    groups = [grant for grant in exact if len(grant["actor_ids"]) > 1]
    sizes = Counter(len(grant["actor_ids"]) for grant in groups)
    assert set(sizes) == {2, 3, 4}
    assert all(count >= 2 for count in sizes.values())
    assert all(len(set(grant["actor_ids"])) == len(grant["actor_ids"]) for grant in groups)
    for artifact_id in {operation["artifact"]["artifact_id"] for operation in operations}:
        chain = [operation for operation in operations if operation["artifact"]["artifact_id"] == artifact_id]
        singles = [grant for grant in exact if grant["artifact"] == chain[0]["artifact"]
                   and len(grant["actor_ids"]) == 1]
        initiate_grants = [grant for grant in singles if grant["allowed_operations"] == [initiate]]
        assert len(initiate_grants) == (0 if variant == "violation" and chain[0] is focal else 1)
        endorse_grants = [grant for grant in singles if grant["allowed_operations"] == [endorse]]
        assert len(endorse_grants) == 1 + int(
            variant == "twin" and focal["artifact"] == chain[0]["artifact"] and focal["operation"] == endorse)
        assert all(grant["actor_ids"][0] in {operation["actor_id"] for operation in chain[1:]}
                   for grant in endorse_grants)
    assert all(focal["actor_id"] not in grant["actor_ids"] for grant in groups
               if grant["artifact"] == focal["artifact"] and grant["allowed_operations"] == [focal["operation"]])
    assert [operation["operation_id"] for index, operation in enumerate(operations) if not authorize(fixture, index)] == (
        [] if variant == "twin" else [focal["operation_id"]])
    assert verify_fixture(fixture, template) == []


def test_h1_group_assignment_changes_with_seed_and_is_stable_for_each_seed():
    groups = []
    for seed in (1, 3, 1102):
        _, fixture = hard(level="H1", seed=seed)
        groups.append([(grant["artifact"]["artifact_id"], grant["actor_ids"]) for grant in
                       record(fixture, "registry")["grants"] if len(grant["actor_ids"]) > 1])
        assert fixture == hard(level="H1", seed=seed)[1]
    assert groups[0] != groups[1] != groups[2]


def test_h1_repeated_focal_actor_keeps_identical_minute_repair_in_both_variants():
    template, violation = hard("budget-transfer", level="H1", seed=1)
    _, twin = hard("budget-transfer", level="H1", variant="twin", seed=1)
    operations = violation["truth"]["operations"]
    focal = next(operation for operation in operations if operation["role"] == "focal")
    repeated = [operation for operation in operations if operation is not focal
                and operation["actor_id"] == focal["actor_id"] and operation["operation"] == focal["operation"]
                and operation["artifact"] == focal["artifact"]]
    assert repeated
    for operation in repeated:
        grant = next(grant for grant in record(violation, "registry")["grants"]
                     if grant["validity_interval"]["start"] == operation["operation_time"])
        assert grant in record(twin, "registry")["grants"]
        assert grant["actor_ids"] == [operation["actor_id"]]
        assert grant["validity_interval"]["end"] == operation["operation_time"].replace(":00Z", ":59Z")
    assert verify_fixture(violation, template) == verify_fixture(twin, template) == []
