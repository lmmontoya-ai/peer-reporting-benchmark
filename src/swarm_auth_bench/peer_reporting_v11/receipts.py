"""P-A4 hashes of primary root evidence, anchored in local Git HEAD."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..events import canonical_json
from ..peer_reporting.live import EvidenceError
from ..peer_reporting.storage import read_sealed, safe_child, seal
from .git_evidence import committed_files
from .schemas import TOOL_SCHEMA_VERSION_P

ROOT_PLAN = "live-plan.json"
STUDY_MANIFEST = "collection-manifest.json"


def requires_receipt(root: Path, plan: dict) -> bool:
    if plan["phase"] == "compatibility":
        return plan["tool_schema_version"] == TOOL_SCHEMA_VERSION_P
    return any(
        entry["level"] == "P"
        for lane in plan["lanes"]
        for entry in read_sealed(safe_child(root, lane["path"]) / "phase-plan.json")["planned_order"]
    )


def receipt_bytes(root: Path, *, study_directory: Path | None = None) -> bytes:
    """Hash live files, including added lane evidence and every file in an attempt archive.

    Compatibility runs precede the study and have no study manifest. Behavioral
    roots require the explicit owning study; its path is relative to the root too.
    """
    root = Path(root).resolve()
    plan = read_sealed(root / ROOT_PLAN)
    files = {root / ROOT_PLAN}
    if plan["phase"] != "compatibility":
        if study_directory is None:
            raise EvidenceError("a behavioral receipt requires its study directory")
        manifest_path = Path(study_directory).resolve() / STUDY_MANIFEST
        if read_sealed(manifest_path)["seal_hash"] != plan["source"]["study_manifest_hash"]:
            raise EvidenceError("receipt study manifest differs from the root plan")
        files.add(manifest_path)
    for lane in plan["lanes"]:
        directory = safe_child(root, lane["path"])
        files.update((directory / "phase-plan.json", directory / "journal.jsonl"))
    lanes = root / "lanes"
    files.update(lanes.rglob("phase-plan.json"))
    files.update(lanes.rglob("journal.jsonl"))
    for directory in lanes.rglob("attempts"):
        files.update(path for path in directory.rglob("*") if path.is_file())
    hashes = {
        Path(os.path.relpath(path, root)).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }
    return (
        canonical_json(seal({"kind": "peer_reporting_v11_primary_receipt", "files": hashes})) + "\n"
    ).encode("utf-8")


def write_receipt(root: Path, receipt_directory: Path, *, study_directory: Path | None = None) -> Path:
    plan = read_sealed(Path(root) / ROOT_PLAN)
    if not requires_receipt(Path(root), plan):
        raise EvidenceError("this earlier-level root requires no receipt")
    raw = receipt_bytes(root, study_directory=study_directory)
    receipt_directory = Path(receipt_directory)
    receipt_directory.mkdir(parents=True, exist_ok=True)
    path = safe_child(receipt_directory, plan["seal_hash"] + ".json")
    if path.exists() and path.read_bytes() != raw:
        raise EvidenceError("existing receipt differs from live primary evidence")
    path.write_bytes(raw)
    return path


def check_receipt(root: Path, receipt_directory: Path | None, *, study_directory: Path | None = None) -> None:
    root = Path(root)
    plan = read_sealed(root / ROOT_PLAN)
    if not requires_receipt(root, plan):
        return
    if receipt_directory is None:
        raise EvidenceError("this root requires an explicit committed receipt directory")
    try:
        raw = receipt_bytes(root, study_directory=study_directory)
        name = plan["seal_hash"] + ".json"
        _, committed = committed_files(Path(receipt_directory), "HEAD", (name,))
        if committed[name] != raw:
            raise EvidenceError("receipt differs from live primary evidence")
    except (OSError, ValueError) as error:
        raise EvidenceError(f"primary evidence receipt mismatch: {error}") from error
