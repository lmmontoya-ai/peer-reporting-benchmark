"""Controller cleanup outside the sealed attempt trace."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

CLEANUP_BUDGET_SECONDS = 30
OWNER_MARKER = ".runtime-owner.json"
HOME_PREFIXES = ("swarm-codex-controller-", "swarm-codex-manifest-", "work-")
LOG = logging.getLogger(__name__)


def process_start(pid: int) -> str | None:
    """Return a process birth identity; unknown identities are never swept."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 87 else "unknown"
        try:
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, created, exited, system, user):
                return "unknown"
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # The command name in /proc/stat can contain spaces and parentheses.
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        return "unknown"


def mark_home(home: Path, *, process_pid: int | None = None) -> None:
    marker = home / OWNER_MARKER
    record = (json.loads(marker.read_bytes()) if marker.exists() else
              {"runner_pid": os.getpid(), "runner_start": process_start(os.getpid())})
    if process_pid is not None:
        record.update(process_pid=process_pid, process_start=process_start(process_pid))
    marker.write_bytes((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def _kill_group(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        if result.returncode and process_start(pid) is not None:
            raise OSError("app-server process tree could not be removed")
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def sweep_orphan_homes(directory: Path | None = None) -> list[dict[str, Any]]:
    """Remove only marked homes with proven dead runners and matching child identities.

    Legacy unmarked homes have no reliable owner evidence and are left alone.
    A reused PID, an unreadable marker, or an unknown process identity is also
    insufficient evidence to remove a home.
    """
    root = (directory or Path(tempfile.gettempdir())).resolve()
    removed = []
    try:
        homes = sorted(root.iterdir())
    except OSError as error:
        LOG.warning("Controller orphan sweep could not inspect %s: %s", root, error)
        return removed
    for home in homes:
        if not home.name.startswith(HOME_PREFIXES) or home.is_symlink() or not home.is_dir():
            continue
        if not (home / OWNER_MARKER).is_file():
            continue
        try:
            record = json.loads((home / OWNER_MARKER).read_bytes())
            pid, birth = record["runner_pid"], record["runner_start"]
            if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or birth == "unknown":
                continue
            actual = process_start(pid)
            if actual == "unknown" or actual == birth:
                continue
            child = record.get("process_pid")
            if child is not None:
                child_birth = record.get("process_start")
                if type(child) is not int or child <= 0 or not isinstance(child_birth, str):
                    continue
                current = process_start(child)
                if current == "unknown" or child_birth == "unknown":
                    continue
                if current == child_birth:
                    _kill_group(child)
                elif current is not None:
                    continue
            target = home.resolve()
            if target.parent != root:
                continue
            shutil.rmtree(target)
            entry = {"home": str(target), "process_pid": child}
            removed.append(entry)
            LOG.warning("Removed orphan controller home %s (app-server PID %s)", target, child)
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOG.warning("Controller orphan sweep retained %s: %s", home, error)
    return removed


async def close_runtime(runtime: Any) -> None:
    """Reserve half of a fresh fixed budget for a resumable force-close pass."""
    deadline = asyncio.get_running_loop().time() + CLEANUP_BUDGET_SECONDS
    try:
        await asyncio.wait_for(runtime.close(), CLEANUP_BUDGET_SECONDS / 2)
    except (Exception, asyncio.CancelledError):
        remaining = max(0.001, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(runtime.close(), remaining)
