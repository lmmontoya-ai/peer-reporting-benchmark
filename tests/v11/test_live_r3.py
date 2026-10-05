"""Round-3 execution rules over sealed roots and the in-memory transport. No model calls."""

import hashlib
import json
import shutil
import time
from copy import deepcopy

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json, read_sealed, seal
from swarm_auth_bench.peer_reporting_v11 import bundle as bundle_module
from swarm_auth_bench.peer_reporting_v11 import live as v11_live
from swarm_auth_bench.peer_reporting_v11.cli import main
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review

from .live_fakes import (
    FakeTransport,
    Harness,
    authorization,
    compat_fixture,
    fake_bundle,
    qualified_root,
    qualifier_steps,
    report_steps,
    study_rows,
    write_study,
)
from .live_fakes import run_phase as run
from .test_live_r2 import (
    LUNA_L1,
    NATIVE,
    READ,
    SERIAL,
    amendment,
    attempt_of,
    build_plan,
    payloads,
    records,
    scripted,
    smoke_root,
    smoke_study,
)


@pytest.fixture(scope="module")
def compat(tmp_path_factory):
    return qualified_root(tmp_path_factory.mktemp("r3-compat"))[0]


@pytest.mark.parametrize("wrong_path", ["typo", "empty", "other_root", "copy"])
async def test_wrong_root_cannot_abandon_started_smoke_or_release_consumed_assignments(
        compat, tmp_path, monkeypatch, capsys, wrong_path):
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    failing = scripted(fixtures, {("gpt-6-astra", "xhigh", "L1", "violation"): lambda f: [NATIVE]})
    status = await run(root, plan, Harness(tmp_path / "h1", failing),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "held" and status["live_model_call_starts"] == 3
    consumed = {record["data"]["attempt_id"] for record in records(root, "attempt_started")}
    supplied = tmp_path / "smoke-v1-typo"
    if wrong_path == "empty":
        supplied.mkdir()
    elif wrong_path == "other_root":
        supplied = compat
    elif wrong_path == "copy":
        shutil.copytree(root, supplied)  # Even the same sealed plan at another path is refused.
    with pytest.raises(ValueError, match="exact registered root path"):
        v11_live.abandon_root(study, plan["seal_hash"], root=supplied, reason="Wrong directory.", bundle=fake_bundle())
    assert main(["abandon-root", str(study), "--plan-hash", plan["seal_hash"], "--root", str(supplied),
                 "--reason", "Wrong directory."]) == 2
    assert "exact registered root path" in json.loads(capsys.readouterr().out)["error"]
    assert [entry["state"] for entry in v11_live.registered_roots(study)] == ["finalized"]
    with pytest.raises(v11_live.LivePhaseError, match="consumed attempts are never abandoned"):
        v11_live.abandon_root(study, plan["seal_hash"], root=root, reason="Already started.", bundle=fake_bundle())
    with pytest.raises(ValueError, match="name every smoke root"):
        build_plan("smoke", study, caps=SERIAL, revision="smoke-v2", compatibility_directories=[compat])
    later, later_plan = smoke_root(tmp_path, compat, study, revision="smoke-v2", prior_roots=[root])
    assert set(later_plan["consumed_attempts"]["consumed_attempt_ids"]) == consumed
    assert later_plan["maximum_live_calls"] == 9
    later_status = await run(later, later_plan, Harness(tmp_path / "h2", scripted(fixtures)),
                             compatibility_directories=[compat], study_directory=study, prior_roots=[root])
    assert later_status["status"] == "complete" and later_status["live_model_call_starts"] == 9
    assert consumed.isdisjoint(record["data"]["attempt_id"] for record in records(later, "attempt_started"))
    with pytest.raises(v11_live.GateError, match="entries differ from the study's smoke rows"):
        build_plan("collection", study, caps=SERIAL, compatibility_directories=[compat], smoke_directory=later)


@pytest.mark.parametrize("plan_state", ["missing", "corrupt", "another_plan"])
def test_finalized_abandonment_requires_matching_sealed_plan_at_registered_path(
        compat, tmp_path, monkeypatch, capsys, plan_state):
    monkeypatch.setattr(bundle_module, "load_bundle", fake_bundle)
    study, _, _ = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    path = root / v11_live.LIVE_PLAN_FILE
    if plan_state == "missing":
        path.unlink()
        message = "no matching sealed live plan"
    elif plan_state == "corrupt":
        atomic_json(path, {**plan, "revision": "edited"})
        message = "missing or corrupt"
    else:
        atomic_json(path, v11_live.read_live_plan(compat))
        message = "another plan hash"
    with pytest.raises(ValueError, match=message):
        v11_live.abandon_root(study, plan["seal_hash"], root=root, reason="Cannot prove no starts.", bundle=fake_bundle())
    assert main(["abandon-root", str(study), "--plan-hash", plan["seal_hash"], "--root", str(root),
                 "--reason", "Cannot prove no starts."]) == 2
    assert message in json.loads(capsys.readouterr().out)["error"]
    assert [entry["state"] for entry in v11_live.registered_roots(study)] == ["finalized"]


def test_abandonment_records_registered_path_and_checked_journal_hashes(compat, tmp_path):
    study, _, _ = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    (registered,) = v11_live.registered_roots(study)
    assert registered["root_path"] == "roots/smoke-v1"
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.glob("lanes/*/journal.jsonl")}
    assert len(hashes) == 6
    result = v11_live.abandon_root(study, plan["seal_hash"], root=root / ".", reason="Never started.", bundle=fake_bundle())
    record = read_sealed(study / v11_live.STUDY_REGISTRY / v11_live.ABANDONED_DIRECTORY / f"{plan['seal_hash']}.json")
    assert result["root_journals_checked"] is True
    assert record["root_path"] == result["root_path"] == registered["root_path"]
    assert record["lane_journal_hashes"] == result["lane_journal_hashes"] == hashes
    assert v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)["study_registration"][
        "state"] == "abandoned"


@pytest.mark.parametrize("plan_state", ["missing", "corrupt", "another_plan"])
def test_pending_root_can_be_abandoned_without_a_matching_plan(compat, tmp_path, monkeypatch, plan_state):
    study, _, _ = smoke_study(tmp_path / "study")
    built = build_plan("smoke", study, caps=SERIAL, compatibility_directories=[compat])
    root = study / "roots" / "pending"

    def crash(*args, **kwargs):
        raise OSError("simulated crash before the plan write")

    with monkeypatch.context() as patched:
        patched.setattr(v11_live, "create_lane_phase", crash)
        with pytest.raises(OSError, match="simulated crash"):
            v11_live.prepare_live_root(root, built, study_directory=study)
    (pending,) = v11_live.registered_roots(study)
    path = root / v11_live.LIVE_PLAN_FILE
    if plan_state == "corrupt":
        atomic_json(path, {"kind": v11_live.TOP_PLAN_KIND})
    elif plan_state == "another_plan":
        atomic_json(path, v11_live.read_live_plan(compat))
    result = v11_live.abandon_root(study, pending["plan_hash"], reason="Incomplete registration.", bundle=fake_bundle())
    assert result["prior_state"] == "pending" and result["root_path"] == "roots/pending"
    assert result["root_journals_checked"] is True and result["lane_journal_hashes"] == {}


async def test_verify_and_export_refuse_an_abandoned_root_with_journaled_starts(compat, tmp_path):
    study, _, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    stopped = scripted(fixtures, {LUNA_L1: lambda f: [READ, ("call", lambda: (root / "STOP").touch()), *report_steps(f)]})
    status = await run(root, plan, Harness(tmp_path / "homes", stopped),
                       compatibility_directories=[compat], study_directory=study)
    assert status["live_model_call_starts"] == 1
    # A retained record from the vulnerable implementation must not hide consumed evidence.
    record = seal({"kind": v11_live.ABANDONED_KIND, "plan_hash": plan["seal_hash"], "root_journals_checked": True})
    marker = study / v11_live.STUDY_REGISTRY / v11_live.ABANDONED_DIRECTORY / f"{plan['seal_hash']}.json"
    marker.parent.mkdir()
    atomic_json(marker, record)
    with pytest.raises(v11_live.EvidenceError, match="abandoned root has journaled attempt starts"):
        v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    with pytest.raises(v11_live.EvidenceError, match="abandoned root has journaled attempt starts"):
        export_live_review(root, tmp_path / "export", bundle=fake_bundle(), study_directory=study)
    assert not (tmp_path / "export").exists()


async def test_identical_compatibility_roots_need_distinct_authorizations(tmp_path):
    built = v11_live.build_compatibility_plan(SERIAL, revision="compat-v1", bundle=fake_bundle())
    a, b = tmp_path / "compat-a", tmp_path / "compat-b"
    for root in (a, b):
        v11_live.prepare_live_root(root, built)
    plan_a, plan_b = v11_live.read_live_plan(a), v11_live.read_live_plan(b)
    assert plan_a["seal_hash"] != plan_b["seal_hash"]
    assert plan_a["root_instance_nonce"] != plan_b["root_instance_nonce"]
    assert {key: value for key, value in plan_a.items() if key not in {"seal_hash", "root_instance_nonce"}} == {
        key: value for key, value in plan_b.items() if key not in {"seal_hash", "root_instance_nonce"}}
    approval = authorization(plan_a, root=a)
    sample = compat_fixture(a, plan_a)
    first = Harness(tmp_path / "ha", lambda model, effort: qualifier_steps(sample))
    assert (await run(a, plan_a, first, auth=approval))["status"] == "complete"
    second = Harness(tmp_path / "hb", lambda model, effort: qualifier_steps(sample))
    with pytest.raises(ValueError, match="authorization names another phase, plan"):
        await run(b, plan_b, second, auth=approval)
    assert len(first.created) == 6 and second.created == []
    assert records(b, "attempt_started") == [] and not (b / "authorizations").exists()


@pytest.mark.parametrize("stop", ["hard_stop", "deadline"])
@pytest.mark.parametrize("fault", ["packet_mismatch", "protocol", "transport", "storage"])
async def test_a_stop_never_masks_a_delivery_protocol_transport_or_storage_failure(
        compat, tmp_path, monkeypatch, stop, fault):
    study, rows, fixtures = smoke_study(tmp_path / "study")
    root, plan = smoke_root(tmp_path, compat, study)
    now = [time.time()]

    def truncate():
        if stop == "hard_stop":
            (root / "HARD_STOP").touch()
        else:
            now[0] += 10 ** 6

    push = FakeTransport._push
    delayed = []

    async def fault_after_stop(self, method, params):
        item = params.get("item", {})
        if fault == "packet_mismatch" and item.get("type") == "userMessage":
            changed = deepcopy(params)
            changed["item"]["content"][0]["text"] += " "
            delayed.append((method, changed))
            return
        if method == "turn/completed":
            if fault == "packet_mismatch":
                for delayed_method, changed in delayed:
                    await push(self, delayed_method, changed)
            elif fault in {"protocol", "transport"}:
                scope = {"threadId": self.thread_id, "turnId": self.turn_id}
                raw_method, raw_params = (NATIVE[1], {**scope, **NATIVE[2]}) if fault == "protocol" else (
                    "runtime/disconnected", scope)
                await push(self, raw_method, raw_params)
        await push(self, method, params)

    monkeypatch.setattr(FakeTransport, "_push", fault_after_stop)
    special = {LUNA_L1: lambda f: [("usage", 1500), ("stall", truncate)]}
    bundle = fake_bundle()
    if fault == "storage":
        audit = bundle.audit_state

        def broken_audit(directory, checkpoint=None):
            if directory.resolve().is_relative_to(root.resolve()):
                raise OSError("simulated durable storage failure")
            return audit(directory, checkpoint)
        bundle = fake_bundle(audit_state=broken_audit)
    harness = Harness(tmp_path / "h1", scripted(fixtures, special))
    status = await run(root, plan, harness, compatibility_directories=[compat], study_directory=study,
                       wall_clock=lambda: now[0], bundle=bundle)
    attempt = attempt_of(rows, "gpt-6-luna", "L1", "violation")
    payload = payloads(root)[attempt]
    assert status["status"] == "held" and status["live_model_call_starts"] == 1
    assert ("hard_stop" if stop == "hard_stop" else "forced_stop_deadline") in status["holds"]
    assert payload["orchestrator"]["collection_stop_reasons"] == ["hard_stop_or_forced_deadline"]
    assert f"execution_check_failure:{attempt}" in status["holds"]
    assert payload["check"]["classification"] != "stop_truncation" and payload["check"]["passed"] is False
    assert payload["observer_result"]["infrastructure_failures"]
    if fault == "packet_mismatch":
        assert "initial packet delivery mismatch" in payload["observer_result"]["infrastructure_failures"]
        assert payload["check"]["checks"]["initial_receipt_exact"] is False
    if stop == "hard_stop":
        (root / "HARD_STOP").unlink()
    resumed = Harness(tmp_path / "h2", scripted(fixtures))
    later = await run(root, plan, resumed, compatibility_directories=[compat], study_directory=study)
    assert later["status"] == "held" and f"retained_failed_or_unknown_attempt:{attempt}" in later["holds"]
    assert resumed.created == []


@pytest.mark.parametrize("lane_used", [True, False])
async def test_amending_every_failure_does_not_prove_a_collection_lane(compat, tmp_path, lane_used):
    rows, fixtures = study_rows("smoke")
    collection_rows, collection_fixtures = study_rows("collection", template_id="release-request", seed=1101)
    if not lane_used:
        collection_rows = [row for row in collection_rows if (row["model"], row["effort"]) != ("gpt-6-astra", "xhigh")]
    study = write_study(tmp_path / "study", rows + collection_rows, {**fixtures, **collection_fixtures}, caps=SERIAL)
    root, plan = smoke_root(tmp_path, compat, study)
    special = {("gpt-6-astra", "xhigh", level, variant): lambda f: [NATIVE]
               for level, variant in (("L1", "violation"), ("L3", "violation"), ("L4", "twin"))}
    for index, (level, variant) in enumerate((("L1", "violation"), ("L3", "violation"), ("L4", "twin"))):
        status = await run(root, plan, Harness(tmp_path / f"h{index}", scripted(fixtures, special)),
                           compatibility_directories=[compat], study_directory=study)
        assert status["status"] == "held"
        failed = attempt_of(rows, "gpt-6-astra", level, variant)
        v11_live.record_amendment(study, amendment(study, [failed]), smoke_roots=[root], bundle=fake_bundle())
    status = await run(root, plan, Harness(tmp_path / "final", scripted(fixtures)),
                       compatibility_directories=[compat], study_directory=study)
    assert status["status"] == "complete" and status["live_model_call_starts"] == 12
    report = v11_live.verify_live_root(root, bundle=fake_bundle(), study_directory=study)
    assert all(row["check_passed"] is False for row in report["lanes"]["gpt-6-astra-xhigh"]["entries"])
    kwargs = {"caps": SERIAL, "compatibility_directories": [compat], "smoke_directory": root}
    if lane_used:
        with pytest.raises(v11_live.GateError, match="gpt-6-astra-xhigh needs at least one passed, valid smoke attempt"):
            build_plan("collection", study, **kwargs)
    else:
        built = build_plan("collection", study, **kwargs)
        assert len(built[0]["gate_evidence"]["smoke"]["accepted_failed_attempts"]) == 3
