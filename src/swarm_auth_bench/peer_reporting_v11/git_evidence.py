"""Exact committed bytes for offline evidence checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..peer_reporting.live import EvidenceError


def committed_files(directory: Path, commit: str, names: tuple[str, ...]) -> tuple[str, dict[str, bytes]]:
    """Require live files to equal the named local Git snapshot."""
    directory = Path(directory).resolve()

    def git(*args: str) -> bytes:
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, check=False)
        if result.returncode:
            raise EvidenceError("evidence requires exact committed content in the named Git commit")
        return result.stdout

    repository = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip())).resolve()
    relative = directory.relative_to(repository).as_posix()
    resolved = git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}").decode().strip()
    evidence = {}
    for name in names:
        path = directory / name
        raw = path.read_bytes()
        git_path = name if relative == "." else f"{relative}/{name}"
        if raw != git("show", f"{resolved}:{git_path}"):
            raise EvidenceError(f"binding evidence {name} differs from the named Git commit")
        evidence[name] = raw
    return resolved, evidence
