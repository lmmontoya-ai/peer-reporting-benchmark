"""Prior-start histories share primary audits only within one operation."""

import shutil
from collections import Counter

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, receipts
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_Q

from .receipt_helpers import commit_receipt
from .test_queue_fix1 import build, selection
from .test_queue_live import study as study


def test_six_root_chain_audits_each_ancestor_once_and_rechecks_later_changes(tmp_path, study, monkeypatch):
    directory = tmp_path / "study"
    shutil.copytree(study, directory)
    bundle = load_bundle().tool_set(TOOL_SCHEMA_VERSION_Q)
    receipt_directory = tmp_path / "receipts"
    roots = []
    source = selection(directory, "smoke")[2]
    for number in range(6):
        ledger = live.prior_root_ledger(roots, phase="smoke", source=source, study_directory=directory,
                                      bundle=bundle, receipt_directory=receipt_directory) if roots else None
        built = build(directory, phase="smoke", ledger=ledger, receipts=receipt_directory)
        root = directory / "roots" / ("ancestor-" + str(number))
        live.prepare_live_root(root, built, study_directory=directory, prior_roots=roots,
                               bundle=bundle, receipt_directory=receipt_directory)
        commit_receipt(root, receipt_directory, study_directory=directory)
        roots.append(root)
    audits = Counter()
    original = receipts.check_receipt
    def checked(root, *args, **kwargs):
        audits[root.resolve()] += 1
        return original(root, *args, **kwargs)
    monkeypatch.setattr(receipts, "check_receipt", checked)
    plan = live.read_live_plan(roots[-1])
    live.check_queue_prior_starts(plan, directory, bundle=bundle, receipt_directory=receipt_directory)
    assert audits == Counter({root.resolve(): 1 for root in roots[:-1]})
    # The cache cannot hide a changed ancestor in a subsequent operation.
    first = live.read_live_plan(roots[0])
    evidence = roots[0] / first["lanes"][0]["path"] / "phase-plan.json"
    original_bytes = evidence.read_bytes()
    evidence.write_bytes(original_bytes + b" ")
    try:
        with pytest.raises(ValueError):
            live.check_queue_prior_starts(plan, directory, bundle=bundle, receipt_directory=receipt_directory)
    finally:
        evidence.write_bytes(original_bytes)
    assert read_sealed(evidence)["seal_hash"] == first["lanes"][0]["plan_hash"]
