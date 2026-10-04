"""Sync source, execute a command, or fetch run artifacts from the isolated guest."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from sync_to_wsl import INCLUDE, PROJECT, allowed

REMOTE_PROJECT = "/opt/swarm-auth-bench"
SSH = ["wsl.exe", "-d", "SwarmAuthBench", "--user", "bench", "--cd", "/", "--exec",
       "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", "/home/bench/.ssh/swarm-vm",
       "-p", "2222", "bench@127.0.0.1"]


def project_path(value: str) -> str:
    path = PurePosixPath(value)
    base = PurePosixPath(REMOTE_PROJECT)
    if not path.is_absolute() or ".." in path.parts or (path != base and base not in path.parents):
        raise ValueError("guest project must be inside /opt/swarm-auth-bench")
    return path.as_posix()


def command(arguments: list[str], project: str = REMOTE_PROJECT) -> list[str]:
    if not arguments:
        raise ValueError("a guest command is required")
    project = project_path(project)
    environment = ["env", "HTTPS_PROXY=http://10.0.2.100:3128", "HTTP_PROXY=http://10.0.2.100:3128",
                   "NO_PROXY=localhost,127.0.0.1", "PYTHONPATH=" + project + "/src"]
    return SSH + ["cd " + shlex.quote(project) + " && exec " + shlex.join(environment + arguments)]


def sync(project: str = REMOTE_PROJECT) -> None:
    project = project_path(project)
    if project != REMOTE_PROJECT:
        # A phase snapshot is created once. Refuse to overwrite its source
        # while another collection might still be using it.
        subprocess.run(command(["mkdir", "-p", str(PurePosixPath(project).parent)]), check=True)
        subprocess.run(command(["mkdir", "-m", "700", project]), check=True)
    files = []
    for name in INCLUDE:
        path = PROJECT / name
        if path.is_dir():
            files.extend(item for item in path.rglob("*") if allowed(item))
        elif path.exists() and allowed(path):
            files.append(path)
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path in sorted(set(files)):
                tar.add(path, arcname=path.relative_to(PROJECT).as_posix(), recursive=False)
        archive.seek(0)
        subprocess.run(command(["tar", "-xf", "-", "-C", project], project), stdin=archive, check=True)
    if project != REMOTE_PROJECT:
        subprocess.run(command(["ln", "-s", REMOTE_PROJECT + "/.venv", project + "/.venv"], project), check=True)
    print(f"Copied {len(set(files))} source files to the independent Linux guest at {project}.")


def fetch(remote: str, destination: Path, project: str = REMOTE_PROJECT) -> None:
    relative = PurePosixPath(remote)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "runs":
        raise ValueError("only relative paths beneath guest runs/ may be fetched")
    target = destination.resolve()
    if not target.is_relative_to(PROJECT.resolve()):
        raise ValueError("local artifact destination must stay inside this project")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(command(["tar", "-cf", "-", "--", relative.as_posix()], project), stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError("artifact archive contains a link or special file")
                resolved = (target / member.name).resolve()
                if not resolved.is_relative_to(target):
                    raise ValueError("artifact archive escapes destination")
            tar.extractall(target, filter="data")
    print(f"Fetched {relative} into {target}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=REMOTE_PROJECT,
                        help="Fresh phase snapshot beneath the guest project; never overwritten by sync")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("sync")
    execute = sub.add_parser("exec")
    execute.add_argument("arguments", nargs=argparse.REMAINDER)
    retrieve = sub.add_parser("fetch")
    retrieve.add_argument("remote")
    retrieve.add_argument("--destination", type=Path, default=PROJECT / ".local/guest-artifacts")
    args = parser.parse_args()
    if args.action == "sync":
        sync(args.project)
    elif args.action == "fetch":
        fetch(args.remote, args.destination, args.project)
    else:
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        raise SystemExit(subprocess.run(command(arguments, args.project)).returncode)


if __name__ == "__main__":
    main()
