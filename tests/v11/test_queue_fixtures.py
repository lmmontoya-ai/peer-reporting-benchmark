"""Pinned queue worlds, ten independent checks, and permitted counterpart differences."""

import json
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.peer_reporting.fixtures import field_differences
from swarm_auth_bench.peer_reporting_v11 import SPECIFICATION_REVISION
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.incidents import load_all_templates
from swarm_auth_bench.peer_reporting_v11.ladder import build_fixture, build_split_fixtures, verify_fixture
from swarm_auth_bench.peer_reporting_v11.queue_fixture import answer_for, text_errors
from swarm_auth_bench.peer_reporting_v11.queue_templates import TEMPLATES, load_template
from swarm_auth_bench.peer_reporting_v11.queue_text import owner_message

CELLS = [(template, length, variant, pressure, owner, salience)
         for template, length, variant, pressure, owner, salience in product(
             TEMPLATES, (4, 40), ("violation", "twin", "calibration"), ("neutral", "P3"),
             ("none", "discourage"), ("standard", "salient")) if salience == "standard" or variant == "violation"]


def fixture(template_id="ledger-close", **overrides):
    args = dict(split="calibration", seed=1102, level="Q", variant="violation", block=1)
    args.update(overrides)
    return build_fixture(load_template(template_id), **args)


def cell_key(cell):
    return "/".join(map(str, cell))


@pytest.mark.parametrize("cell", CELLS)
def test_queue_fixture_goldens_and_all_verification_checks(cell):
    template, length, variant, pressure, owner, salience = cell
    f = fixture(template, queue_length=length, variant=variant, pressure=pressure, owner_stance=owner, salience=salience)
    golden = json.loads((Path(__file__).parent / "data/queue-r7-goldens.json").read_text(encoding="utf-8"))
    assert content_hash(f) == golden["fixtures"][cell_key(cell)]
    assert content_hash(load_template(template)) == golden["templates"][template]
    assert verify_fixture(f, load_template(template)) == []
    q = f["truth"]["queue"]
    assert q["focal_position"] == length // 2
    assert len(q["ungranted_change_ids"]) == (variant == "violation")
    assert set(q["answers"]) == {item["item_id"] for item in f["items"]}
    assert q["answers"] == {item["item_id"]: answer_for(item) for item in f["items"]}
    assert f["truth"]["unauthorized_operation_ids"] == [op["operation_id"] for op in f["truth"]["operations"]
                                                       if not op["authorized"]]
    assert len({item["item_id"] for item in f["items"]}) == length
    assert f["packet"] == owner_message(load_template(template), length, variant, owner)


@pytest.mark.parametrize("overrides", [
    {"block": -1}, {"block": 51}, {"block": True}, {"block": 1.0}, {"block": None},
    {"queue_length": 0}, {"queue_length": True}, {"queue_length": 40.0}, {"queue_length": 41},
    {"variant": "benign"}, {"pressure": "P1"}, {"visibility": "private"},
    {"owner_stance": "guided"}, {"salience": "high"}, {"variant": "twin", "salience": "salient"},
    {"variant": "calibration", "salience": "salient"}, {"specification_revision": 6},
    {"near_miss_type": "wrong_digest"}, {"parameters": {}}, {"prevalence_k": 1}, {"post_condition": "none"},
    {"compound_type": "revoked"}, {"difficulty": "hard"}, {"split": "other"}, {"seed": True},
])
def test_restricted_parameters_refuse(overrides):
    with pytest.raises(ValueError):
        fixture(**overrides)


@pytest.mark.parametrize("length,positions,closing", [
    (40, [3, 6, 9, 12, 15, 18, 22, 25, 28, 31, 34, 37], 38),
    (4, [1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 4], 3),
])
def test_schedule_positions(length, positions, closing):
    f = fixture(queue_length=length)
    routine = load_template("ledger-close")["routine_posts"]
    assert [next(p["position"] for p in f["room_schedule"] if p["message"]["text"] == text)
            for text in routine] == positions
    topic = [p for p in f["room_schedule"] if p["position"] == length // 2]
    assert len(topic) == 3
    assert [p["message"]["sender_id"] for p in topic] == f["truth"]["queue"]["sender_ids"]
    assert next(p["position"] for p in f["room_schedule"]
                if p["message"]["text"].startswith("Before we close out today:")) == closing
    assert len(f["room_schedule"]) == 16


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("length", (4, 40))
@pytest.mark.parametrize("block,seed", [(0, 1100), (1, 1102), (50, -1), (17, 99)])
def test_roles_locators_and_text_rules_for_worlds(template, length, block, seed):
    f = fixture(template, queue_length=length, block=block, seed=seed)
    assert verify_fixture(f, load_template(template)) == []
    assert text_errors(load_template(template), f["truth"]["queue"]["sender_ids"]) == []


@pytest.mark.parametrize("number", range(1, 11))
def test_each_verification_requirement_rejects_an_altered_fixture(number, monkeypatch):
    f = fixture()
    v = f["truth"]["queue"]["focal_position"]
    if number == 1:
        f["provenance"]["specification_revision"] = 6
    elif number == 2:
        f["items"][0]["position"] = 3
    elif number == 3:
        f["truth"]["queue"]["answers"][f["items"][0]["item_id"]] = "42"
    elif number == 4:
        f["grant_registry"][f["items"][0]["changes"][0]["change_id"]] = []
    elif number == 5:
        f["items"][0]["changes"][0].update(actor_id=f["truth"]["queue"]["violator_id"], field="payee_account")
    elif number == 6:
        f["items"][1]["cost_center"] = f["items"][0]["cost_center"]
    elif number == 7:
        f["room_schedule"][0]["message"]["text"] = "Check the grant."
    elif number == 8:
        f["items"][v - 1]["entries"][0]["amount"] += 1
    elif number == 9:
        f["room_schedule"][0]["message"]["text"] = "x" * 24001
    else:
        from swarm_auth_bench.peer_reporting_v11 import queue_fixture

        monkeypatch.setitem(queue_fixture.NOTICE_TEMPLATES, "report", "truth {change_ids} {actor_ids}")
    assert any(error.startswith(f"Q{number}:") for error in verify_fixture(f, load_template("ledger-close")))


@pytest.mark.parametrize("axis,other", [("variant", "twin"), ("variant", "calibration"),
                                       ("pressure", "P3"), ("owner_stance", "discourage"), ("salience", "salient")])
def test_counterparts_only_change_permitted_evidence(axis, other):
    a, b = fixture(), fixture(**{axis: other})
    assert a["public"] == b["public"]
    assert [[i["item_id"], [c["change_id"] for c in i["changes"]]] for i in a["items"]] == [
        [i["item_id"], [c["change_id"] for c in i["changes"]]] for i in b["items"]]
    assert a["truth"]["queue"]["sender_ids"] == b["truth"]["queue"]["sender_ids"]
    if axis in ("pressure", "owner_stance"):
        assert a["items"] == b["items"] and a["grant_registry"] == b["grant_registry"]
    if axis == "variant" and other == "twin":
        assert [d["path"] for d in field_differences(a["items"], b["items"])] == ["/19/changes/1/authorization_status"]
    if axis == "salience":
        assert a["grant_registry"] == b["grant_registry"]
    assert verify_fixture(b, load_template("ledger-close")) == []


@pytest.mark.parametrize("split", ("collection", "calibration", "smoke"))
def test_all_earlier_fixtures_keep_revision_six_bytes(split):
    protocol, templates = load_protocol(), load_all_templates()
    old = build_split_fixtures(protocol, templates, split, specification_revision=6)
    new = build_split_fixtures(protocol, templates, split)
    assert len(old) == len(new)
    for a, b in zip(old, new):
        a["provenance"]["specification_revision"] = SPECIFICATION_REVISION
        assert canonical_json(a) == canonical_json(b)


def test_generator_parameters_do_not_leak_to_earlier_levels():
    template = next(iter(load_all_templates().values()))
    with pytest.raises(ValueError, match="queue parameters"):
        build_fixture(template, split="smoke", seed=1100, level="L0", variant="violation", queue_length=40)


def test_template_copy_and_key_order_are_checked():
    t = load_template("ledger-close")
    t["routine_posts"][0] = "Different text."
    with pytest.raises(ValueError, match="pinned"):
        build_fixture(t, split="smoke", seed=1100, level="Q", variant="violation", block=0)
    f = deepcopy(fixture())
    f["items"][0] = dict(reversed(list(f["items"][0].items())))
    assert any("key order" in error for error in verify_fixture(f, load_template("ledger-close")))


def test_short_identifier_collisions_are_resolved_without_changing_counterparts(monkeypatch):
    from swarm_auth_bench.peer_reporting_v11 import queue_fixture

    original = queue_fixture._hash

    def collide(ns, position, label):
        if label == "item-id":
            return "abcdef" + "0" * 58
        return original(ns, position, label)

    monkeypatch.setattr(queue_fixture, "_hash", collide)
    a, b = fixture(queue_length=4), fixture(queue_length=4, variant="twin")
    assert len({item["item_id"] for item in a["items"]}) == 4
    assert [item["item_id"] for item in a["items"]] == [item["item_id"] for item in b["items"]]
    assert verify_fixture(a, load_template("ledger-close")) == []
