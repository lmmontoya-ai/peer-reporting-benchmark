"""Copy the project into the dedicated WSL distro without mounting host drives."""

from __future__ import annotations

import argparse
import subprocess
import tarfile
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
INCLUDE = (
    "src", "scripts", "configs", "world", "tasks", "study", "tests", "docs", "isolation",
    "pyproject.toml", "uv.lock", "README.md", "research_project.md",
)
EXCLUDE_PARTS = {".git", ".local", ".venv", "__pycache__", "runs", ".pytest_cache"}
EXCLUDE_NAMES = {".env", "credentials.json", "auth.json"}


def allowed(path: Path) -> bool:
    relative = path.relative_to(PROJECT)
    return (
        not any(part in EXCLUDE_PARTS for part in relative.parts)
        and path.name not in EXCLUDE_NAMES
        and not path.name.startswith(".env.")
        and not path.is_symlink()
        and path.is_file()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="SwarmAuthBench")
    parser.add_argument("--destination", default="/opt/swarm-auth-bench")
    args = parser.parse_args()
    if not args.destination.startswith("/opt/") or ".." in Path(args.destination).parts:
        parser.error("destination must be beneath /opt")

    files: list[Path] = []
    for name in INCLUDE:
        candidate = PROJECT / name
        if candidate.is_dir():
            files.extend(path for path in candidate.rglob("*") if allowed(path))
        elif candidate.exists() and allowed(candidate):
            files.append(candidate)

    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path in sorted(set(files)):
                tar.add(path, arcname=path.relative_to(PROJECT).as_posix(), recursive=False)
        archive.seek(0)
        prefix = ["wsl.exe", "-d", args.distro, "--user", "root", "--cd", "/", "--exec"]
        subprocess.run(prefix + ["mkdir", "-p", args.destination], check=True)
        subprocess.run(prefix + ["tar", "-xf", "-", "-C", args.destination], stdin=archive, check=True)
        subprocess.run(prefix + ["chown", "-R", "bench:bench", args.destination], check=True)
    print(f"Copied {len(files)} project files to {args.distro}:{args.destination}")


if __name__ == "__main__":
    main()
