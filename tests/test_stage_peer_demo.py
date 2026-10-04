"""The local demo starts without results and never stages private records."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "stage_peer_demo", Path(__file__).parents[1] / "scripts" / "stage_peer_demo.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_stages_only_authored_ui_and_documentation(tmp_path):
    output = module.stage(tmp_path / "demo")
    names = {path.name for path in output.iterdir()}
    assert names == set(module.ASSETS) | {
        "peer-reporting-spec.md", "peer-reporting-source-review.md", "peer-reporting-resource-proposal.json"}
    assert not (output / "results-v1").exists()
    assert not (output / "collection-v1").exists()
    assert not (output / "run-status.json").exists()
    text = (output / "demo.html").read_text(encoding="utf-8")
    assert 'href="peer-reporting-spec.md"' in text
    assert 'href="../peer-reporting-spec.md"' not in text
    assert "Collection is pending" in text
    with pytest.raises(ValueError, match="new directory"):
        module.stage(output)
