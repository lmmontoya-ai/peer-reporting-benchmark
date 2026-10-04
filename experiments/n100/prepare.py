"""Prepare a fresh N100 tree from local pinned Git objects; offline only."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

BASE = "5df9b95d5212f551a1e56c182d02bc3374ed0f10"

ALLOWLIST = frozenset(('src/swarm_auth_bench/peer_reporting/__init__.py', 'src/swarm_auth_bench/peer_reporting/collection.py', 'src/swarm_auth_bench/peer_reporting/config.py', 'src/swarm_auth_bench/peer_reporting/export.py', 'src/swarm_auth_bench/peer_reporting/fixtures.py', 'src/swarm_auth_bench/peer_reporting/prompts.py', 'src/swarm_auth_bench/peer_reporting/protocol.json', 'configs/peer-reporting-n100-v1-candidate.json', 'docs/peer-reporting-n100-spec.md', 'docs/peer-reporting-n100-human-review-plan-v1.json', 'scripts/prepare_n100_review_plan.py', 'scripts/summarize_peer_collection.py', 'tests/test_peer_reporting_n100_fixtures.py', 'tests/test_peer_reporting_n100_plan.py', 'tests/test_peer_reporting_n100_runtime.py'))

def sha(data):
    return hashlib.sha256(data).hexdigest()

def prepare(repository, destination):
    repository = Path(repository).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("destination must be new")
    if repository == destination or repository in destination.parents:
        raise ValueError("destination must be outside the source repository")
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / "overlay-manifest.json").read_text(encoding="utf-8"))
    if manifest["base_commit"] != BASE:
        raise ValueError("unexpected base commit")
    resolved = subprocess.check_output(["git", "-C", str(repository), "rev-parse", BASE], text=True).strip()
    if resolved != BASE:
        raise ValueError("base commit mismatch")
    if {row["path"] for row in manifest["files"]} != ALLOWLIST:
        raise ValueError("overlay allowlist mismatch")
    paths = []
    for row in manifest["files"]:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in paths:
            raise ValueError("unsafe or duplicate overlay path")
        paths.append(relative.as_posix())
        data = (here / "overlay" / relative).read_bytes()
        if sha(data) != row["sha256"]:
            raise ValueError("overlay hash mismatch: " + row["path"])
        try:
            original = subprocess.check_output(["git", "-C", str(repository), "show", BASE + ":" + row["path"]], stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            original = None
        if (sha(original) if original is not None else None) != row["base_sha256"]:
            raise ValueError("base source hash mismatch: " + row["path"])
    actual = sorted(p.relative_to(here / "overlay").as_posix() for p in (here / "overlay").rglob("*") if p.is_file())
    if actual != sorted(paths):
        raise ValueError("overlay file set mismatch")
    archive = subprocess.check_output(["git", "-C", str(repository), "archive", BASE])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        members = source.getmembers()
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or not (member.isfile() or member.isdir()):
                raise ValueError("unsafe Git archive member")
        destination.mkdir(parents=True)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
    for row in manifest["files"]:
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((here / "overlay" / row["path"]).read_bytes())
    env = dict(os.environ, PYTHONPATH=str(destination / "src"), PYTHONDONTWRITEBYTECODE="1")
    check = """import json
from pathlib import Path
from swarm_auth_bench.peer_reporting.collection import build_collection, verify_collection
from swarm_auth_bench.peer_reporting.config import StudyConfig
from swarm_auth_bench.peer_reporting.storage import read_sealed
config=StudyConfig.from_dict(json.loads(Path('configs/peer-reporting-n100-v1-candidate.json').read_text(encoding='utf-8')))
result=build_collection(Path('offline-check/study'),config)
audit=verify_collection(Path('offline-check/study'))
assert audit['valid'] and audit['counts']=={'collection':54,'smoke':9}
manifest=read_sealed(Path('offline-check/study/collection-manifest.json'))
assert manifest['seal_hash']=='0887b38e5a6a53ebaac071ca93e38c606c0e6d4151abd9d37d70f1dba83dab02'
Path('offline-check/verification.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
print(json.dumps({'plan_hash':manifest['seal_hash'],'counts':audit['counts'],'offline_only':True}))
"""
    subprocess.run([sys.executable, "-c", check], cwd=destination, env=env, check=True)
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repository, args.destination)
