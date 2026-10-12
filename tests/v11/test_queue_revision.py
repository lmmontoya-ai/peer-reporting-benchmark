"""Retain evidence built by revision 6 and verify it unchanged with revision 7 code."""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import SPECIFICATION_REVISION, collection, live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.receipts import receipt_bytes
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

BASELINE_BUILD = """
import json, sys
from pathlib import Path
from swarm_auth_bench.events import content_hash
from swarm_auth_bench.peer_reporting_v11 import SPECIFICATION_REVISION, collection, live
from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.incidents import load_all_templates
from swarm_auth_bench.peer_reporting_v11.receipts import receipt_bytes
from swarm_auth_bench.peer_reporting_v11.schemas import TOOL_SCHEMA_VERSION_P

assert SPECIFICATION_REVISION == 6
root = Path(sys.argv[1])
caps = json.loads((root / "caps.json").read_text(encoding="utf-8"))
inputs = dict(protocol=load_protocol(), templates=load_all_templates(), caps_record=caps)
assert collection.build_study(root / "study", **inputs)["fixtures"] == 405
assert collection.verify_study(root / "study", **inputs)["valid"]
for version, name in ((None, "v1"), (TOOL_SCHEMA_VERSION_P, "v2")):
    plan = live.build_compatibility_plan(caps, revision="revision-consistency", tool_schema_version=version)
    live.prepare_live_root(root / name, plan)
    (root / (name + "-receipt.json")).write_bytes(receipt_bytes(root / name))
bundles = [load_bundle(), load_bundle().tool_set(TOOL_SCHEMA_VERSION_P)]
(root / "tools.json").write_bytes(json.dumps([content_hash(b.tool_manifest()) for b in bundles]).encode("utf-8"))
"""


def test_revision6_sealed_study_and_compatibility_evidence_verify_unchanged(tmp_path, wp6_inputs):
    repository = Path(__file__).resolve().parents[2]
    source = tmp_path / "baseline"
    archive = subprocess.run(["git", "archive", "b0476d2", "src"], cwd=repository, check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as files:
        files.extractall(source, filter="data")
    (tmp_path / "caps.json").write_bytes(json.dumps(wp6_inputs["caps_record"]).encode("utf-8"))
    environment = {**os.environ, "PYTHONPATH": str(source / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    baseline = subprocess.run([sys.executable, "-c", BASELINE_BUILD, str(tmp_path)], cwd=tmp_path,
                              env=environment, capture_output=True, text=True, encoding="utf-8")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert SPECIFICATION_REVISION == 7
    study = tmp_path / "study"
    retained = {path: path.read_bytes() for path in study.rglob("*.json")}
    manifest = read_sealed(study / collection.STUDY_MANIFEST)
    assert manifest["protocol"]["specification_revision"] == 6
    assert len(manifest["fixtures"]) == 405
    assert {read_sealed(study / ref["path"])["provenance"]["specification_revision"]
            for ref in manifest["fixtures"].values()} == {6}
    report = collection.verify_study(study, **wp6_inputs)
    assert report["valid"], report["errors"]
    assert live.verify_sealed_study(study, wp6_inputs["caps_record"])["valid"]
    for name in ("v1", "v2"):
        assert receipt_bytes(tmp_path / name) == (tmp_path / (name + "-receipt.json")).read_bytes()
        assert live.verify_live_root(tmp_path / name)["live_model_call_starts"] == 0
    bundles = [load_bundle(), load_bundle().tool_set(TOOL_SCHEMA_VERSION_P)]
    assert json.loads((tmp_path / "tools.json").read_bytes()) == [content_hash(b.tool_manifest()) for b in bundles]
    assert all(path.read_bytes() == data for path, data in retained.items())
