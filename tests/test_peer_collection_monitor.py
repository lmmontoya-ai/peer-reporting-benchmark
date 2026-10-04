"""Provisional monitor checks use only synthetic local files, never a guest."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def test_unknown_usage_remains_visible_without_a_provider_call(tmp_path, monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("peer_monitor", scripts / "monitor_peer_collection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path / "live-collection"
    directory.mkdir()
    (directory / "phase-index.json").write_text(json.dumps({
        "entries": {"synthetic": {"status": "archived", "model": "synthetic-model",
                                  "attempt_id": "synthetic-attempt", "attempt": {
                                      "check_passed": True, "elapsed_seconds": 1}}},
        "last_run": {"halted": {"reason": "unknown_usage_hold"}}}), encoding="utf-8")
    (directory / "budget-ledger.json").write_text(json.dumps({"attempts": {
        "synthetic": {"status": "unresolved", "observed": 0, "reservation": 100}},
        "stop_reason": None}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", "-c", module.REMOTE_READ, str(tmp_path)],
                            capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    row = data["phases"]["collection"]
    assert data["verified_final_evidence"] is False
    assert row["archived"] == 1
    assert row["unknown_usage_attempts"] == 1
    assert row["halt_reason"] == "unknown_usage_hold"
    assert row["status"] == "halted"
    assert row["reserved_tokens"] == 100
    assert data["phases"]["smoke"]["status"] == "unstarted"
