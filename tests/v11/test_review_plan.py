"""The frozen review plan (spec 11; docs/v11/review-rubric.md section 4), built from the real study manifest."""

import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed, seal
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.review_plan import (
    REVIEW_SEED,
    SCOPES,
    _counts,
    _selection,
    build_review_plan,
    plan_rows,
    rank,
    verify_review_plan,
)


@pytest.fixture(scope="module")
def plan(wp6_study):
    _, manifest, _ = wp6_study
    return build_review_plan(manifest)


@pytest.fixture(scope="module")
def rows(plan):
    return plan["rows"]


def test_sizes_for_the_v11_study(plan):
    counts = plan["counts"]
    assert counts["first_review_trials"] == 1128
    assert counts["first_review_by_arm_and_scope"] == {
        "collection": {"full": 117, "reports_and_posts": 504, "posts": 315},
        "channel_failure": {"full": 72},
        "low_effort": {"full": 15, "reports_and_posts": 63, "posts": 42}}
    assert counts["first_review_by_scope"] == {"full": 204, "reports_and_posts": 567, "posts": 357}
    assert counts["second_review_trials"] == 336
    assert counts["second_review_by_scope"] == {"full": 72, "reports_and_posts": 162, "posts": 102}
    assert sorted(counts["full_scope_templates"].values()) == [5, 5, 5, 6, 6, 6, 6, 6]


def test_calibration_and_smoke_are_outside_the_plan(plan, rows, wp6_study):
    _, manifest, _ = wp6_study
    arms = {row["assignment_id"]: row["arm"] for row in manifest["assignments"]}
    assert {arms[row["assignment_id"]] for row in rows} == {"collection", "channel_failure", "low_effort"}
    assert set(plan["excluded_arms"]) == {"calibration", "smoke"}
    assert len(rows) == sum(arm in ("collection", "channel_failure", "low_effort") for arm in arms.values())


def test_every_trial_has_posts_and_every_control_has_reports(rows):
    assert all("observer_message" in SCOPES[row["scope"]] for row in rows)
    for row in rows:
        if row["variant"] in ("twin", "benign", "ambiguity", "accusation"):
            assert "private_report" in SCOPES[row["scope"]], row
        if row["arm"] == "channel_failure":
            assert row["scope"] == "full" and "final_response" in SCOPES[row["scope"]]


def test_one_violation_per_level_prompt_model_cell_with_its_twin(rows):
    full = [row for row in rows if row["arm"] == "collection" and row["scope"] == "full"]
    violations = Counter((row["level"], row["prompt_condition"], row["model"]) for row in full
                         if row["variant"] == "violation")
    assert len(violations) == 45 and set(violations.values()) == {1}
    picks = {(row["level"], row["prompt_condition"], row["model"]): row["template_id"] for row in full
             if row["variant"] == "violation"}
    twins = {(row["level"], row["prompt_condition"], row["model"]): row["template_id"] for row in full
             if row["variant"] == "twin"}
    assert twins == picks
    for level in ("L0", "L1", "L2", "L3", "L4"):
        assert len({template for (lv, *_), template in picks.items() if lv == level}) == 8
    controls = Counter((row["variant"], row["prompt_condition"], row["model"]) for row in full
                       if row["variant"] in ("benign", "ambiguity", "accusation"))
    assert len(controls) == 27 and set(controls.values()) == {1}
    pairs = defaultdict(set)
    for row in full:
        if row["variant"] in ("ambiguity", "accusation"):
            pairs[row["prompt_condition"], row["model"]].add(row["template_id"])
    assert all(len(templates) == 1 for templates in pairs.values())


def test_low_effort_sample_reuses_the_collection_fixture_for_each_level_and_model(rows):
    collection = {(row["level"], row["variant"], row["model"]): row["fixture_id"] for row in rows
                  if row["arm"] == "collection" and row["scope"] == "full" and row["prompt_condition"] == "guided"}
    low = [row for row in rows if row["arm"] == "low_effort" and row["scope"] == "full"]
    assert len(low) == 15
    assert all(row["fixture_id"] == collection[row["level"], row["variant"], row["model"]] for row in low)
    assert Counter(row["variant"] for row in low) == {"violation": 6, "twin": 6, "ambiguity": 3}


def test_second_review_takes_a_quarter_of_each_stratum_by_documented_rank(plan, rows):
    strata = defaultdict(list)
    for row in rows:
        strata[row["arm"], row["level"], row["variant"], row["scope"], row["model"]].append(row)
    for members in strata.values():
        ordered = sorted(members, key=lambda row: rank(plan["seed"], "second-review||", row["assignment_id"]))
        take = -(-len(members) // 4)
        assert [row["second_review"] for row in ordered] == [True] * take + [False] * (len(members) - take)
    row = rows[0]
    digest = hashlib.sha256(f"{REVIEW_SEED}||second-review||{row['assignment_id']}".encode()).hexdigest()
    assert rank(REVIEW_SEED, "second-review||", row["assignment_id"])[0] == digest


def test_a_violation_pick_follows_the_balance_rule_and_seeded_rank(plan, rows):
    first_cell = [row for row in rows if row["arm"] == "collection" and row["variant"] == "violation"
                  and (row["level"], row["prompt_condition"], row["model"]) == ("L0", "neutral", "gpt-6-luna")]
    chosen = min(first_cell, key=lambda row: rank(plan["seed"], "", row["assignment_id"]))
    assert chosen["scope"] == "full" and "violation_sample" in chosen["reasons"]
    assert sum(row["scope"] == "full" for row in first_cell) == 1


def test_the_plan_is_deterministic_sealed_and_verifiable(plan, wp6_study):
    _, manifest, _ = wp6_study
    assert build_review_plan(manifest) == plan
    assert plan["seal_hash"] and plan["study_manifest_hash"] == manifest["seal_hash"]
    assert verify_review_plan(plan, manifest) == []
    frozen = build_review_plan(manifest, frozen_at_utc="2026-10-05T00:00:00+00:00")
    assert verify_review_plan(frozen, manifest) == []
    tampered = deepcopy({key: value for key, value in plan.items() if key != "seal_hash"})
    tampered["rows"][0]["second_review"] = not tampered["rows"][0]["second_review"]
    assert "plan rows differs from the recomputed plan" in verify_review_plan(seal(tampered), manifest)
    assert any("seal" in error for error in verify_review_plan({**plan, "seed": 7}, manifest))
    assert set(plan_rows(plan)) == {row["assignment_id"] for row in plan["rows"]}


@pytest.mark.parametrize("seed", [1, 2, REVIEW_SEED + 1, True, "20261005"])
def test_build_refuses_any_seed_other_than_the_protocol_seed(wp6_study, seed):
    _, manifest, _ = wp6_study
    with pytest.raises(ValueError, match="protocol review_seed"):
        build_review_plan(manifest, seed=seed)


def test_two_fully_recomputed_plans_with_alternative_seeds_cannot_verify(plan, wp6_study):
    _, manifest, _ = wp6_study
    assert plan["seed"] == load_protocol()["review_seed"]
    alternate = []
    for seed in (1, 2):
        changed = deepcopy({key: value for key, value in plan.items() if key != "seal_hash"})
        changed["seed"] = seed
        changed["rows"] = sorted(_selection(manifest, seed).values(), key=lambda row: row["assignment_id"])
        changed["counts"] = _counts(changed["rows"])
        alternate.append(seal(changed))
    assert [row["scope"] for row in alternate[0]["rows"]] != [row["scope"] for row in alternate[1]["rows"]]
    for other in alternate:
        assert "plan seed differs from the recomputed plan" in verify_review_plan(other, manifest)
    assert verify_review_plan(plan, manifest) == []


def test_build_and_verify_read_the_current_protocol_seed(plan, wp6_study, monkeypatch):
    _, manifest, _ = wp6_study
    changed_protocol = {**load_protocol(), "review_seed": REVIEW_SEED + 1}
    monkeypatch.setattr("swarm_auth_bench.peer_reporting_v11.review_plan.load_protocol", lambda: changed_protocol)
    changed = build_review_plan(manifest)
    assert changed["seed"] == changed_protocol["review_seed"]
    assert verify_review_plan(changed, manifest) == []
    assert "plan seed differs from the recomputed plan" in verify_review_plan(plan, manifest)


def test_the_plan_reads_only_manifest_labels(plan, wp6_study):
    _, manifest, _ = wp6_study
    stripped = deepcopy({key: value for key, value in manifest.items() if key != "seal_hash"})
    for row in stripped["assignments"]:
        row["instructions"] = "replaced"
    rebuilt = build_review_plan(seal(stripped))
    assert [(row["assignment_id"], row["scope"], row["second_review"]) for row in rebuilt["rows"]] == [
        (row["assignment_id"], row["scope"], row["second_review"]) for row in plan["rows"]]
    with pytest.raises(ValueError, match="seal"):
        build_review_plan({**manifest, "seal_hash": "0" * 64})


def test_review_plan_command_writes_once(wp6_study, tmp_path, capsys):
    directory, manifest, _ = wp6_study
    output = tmp_path / "review-plan.json"
    assert main(["review-plan", str(directory), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    stored = read_sealed(output)
    assert result["seal_hash"] == stored["seal_hash"] and stored["frozen_at_utc"]
    assert verify_review_plan(stored, manifest) == []
    assert main(["review-plan", str(directory), "--output", str(output)]) == 2
    assert "never overwritten" in json.loads(capsys.readouterr().out)["error"]


def test_review_plan_command_has_no_seed_override(wp6_study, tmp_path, capsys):
    directory, _, _ = wp6_study
    output = tmp_path / "review-plan.json"
    with pytest.raises(SystemExit) as error:
        main(["review-plan", str(directory), "--output", str(output), "--seed", "1"])
    assert error.value.code == 2
    assert "unrecognized arguments: --seed 1" in capsys.readouterr().err
    assert not output.exists()
