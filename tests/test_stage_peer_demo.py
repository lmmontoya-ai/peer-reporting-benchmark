"""The local helper stages the audited public projection or fails before output."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("stage_peer_demo", Path(__file__).parents[1] / "scripts/stage_peer_demo.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_stages_bundled_public_demo_and_excludes_private_surfaces(tmp_path):
    output = module.stage(tmp_path / "demo")
    summary = json.loads((output / "results-v1/summary.json").read_text(encoding="utf-8"))
    assert len(summary["rows"]) == 225
    assert summary["execution_audit"]["final_human_output_count"] == 0
    assert summary["execution_audit"]["unresolved_usage_assignments"] == 1
    assert len(module.projection_files(output)) > 450
    assert not (output / "run.html").exists()
    assert not (output / "run-status.json").exists()
    assert not (output / "collection-v1/reviewer").exists()
    assert not (output / "collection-v1/controller").exists()
    text = (output / "demo.html").read_text(encoding="utf-8")
    assert 'href="peer-reporting-spec.md"' in text
    assert 'href="../peer-reporting-spec.md"' not in text
    assert "Human accuracy review remains pending" in text
    with pytest.raises(ValueError, match="new directory"):
        module.stage(output)


def test_missing_public_projection_fails_before_creating_output(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(module, "SOURCE_DIR", source)
    output = tmp_path / "demo"
    with pytest.raises(ValueError, match="absent"):
        module.stage(output)
    assert not output.exists()


def test_projection_manifest_cannot_copy_private_paths(tmp_path):
    manifest = {"kind": "public_peer_results_projection_manifest_v1", "reviewer_packets_included": False,
                "controller_bindings_included": False, "file_count": 1,
                "files": [{"path": "../controller/secret.json", "sha256": "a" * 64}]}
    (tmp_path / "projection-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        module.projection_files(tmp_path)


def test_projection_file_modification_is_rejected(tmp_path):
    (tmp_path / "results-v1").mkdir()
    (tmp_path / "results-v1/summary.json").write_text("changed", encoding="utf-8")
    manifest = {"kind": "public_peer_results_projection_manifest_v1", "reviewer_packets_included": False,
                "controller_bindings_included": False, "file_count": 1,
                "files": [{"path": "results-v1/summary.json", "sha256": "a" * 64}]}
    (tmp_path / "projection-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        module.projection_files(tmp_path)
