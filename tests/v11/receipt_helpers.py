"""Commit receipts in real, isolated temporary Git worktrees."""

import subprocess
from pathlib import Path

from swarm_auth_bench.peer_reporting_v11.receipts import write_receipt


def commit_receipt(root, receipt_directory, *, study_directory=None):
    directory = Path(receipt_directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").exists():
        git(directory, "init", "-q")
    path = write_receipt(root, directory, study_directory=study_directory)
    git(directory, "add", "--", path.name)
    git(directory, "commit", "--allow-empty", "-qm", "Retain offline primary evidence receipt.")
    return directory


def git(directory, *args):
    return subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "core.autocrlf=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Offline test",
            "-c",
            "user.email=offline@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
    ).stdout
