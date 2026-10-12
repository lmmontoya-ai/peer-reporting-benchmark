"""Controller cleanup outside the sealed attempt trace."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    """Report marked homes with proven dead runners for manual cleanup.

    Legacy unmarked homes have no reliable owner evidence and are left alone.
    A child is reported as live only when its birth identity still matches.
    This scan never removes a directory or signals a process, even if a path
    is replaced after its marker has been read.
    """
    root = (directory or Path(tempfile.gettempdir())).resolve()
    orphans = []
    try:
        homes = sorted(root.iterdir())
    except OSError as error:
        LOG.warning("Controller orphan sweep could not inspect %s: %s", root, error)
        return orphans
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
            live_child = None
            if child is not None:
                child_birth = record.get("process_start")
                if (type(child) is int and child > 0 and isinstance(child_birth, str)
                        and child_birth != "unknown" and process_start(child) == child_birth):
                    live_child = child
            entry = {"home": str(home), "process_pid": live_child}
            orphans.append(entry)
            LOG.warning("Orphan controller home %s (live app-server PID %s); retained for manual cleanup",
                        home, live_child)
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOG.warning("Controller orphan sweep retained %s: %s", home, error)
    return orphans


async def close_runtime(runtime: Any) -> None:
    """Finish bounded cleanup before propagating outer-task cancellation.

    The propagated cancellation carries cleanup_completed so the caller can
    record cancellation while retaining the resource-cleanup result.
    """
    deadline = asyncio.get_running_loop().time() + CLEANUP_BUDGET_SECONDS
    first_pass = asyncio.create_task(runtime.close())

    async def finish() -> None:
        try:
            await asyncio.wait_for(first_pass, CLEANUP_BUDGET_SECONDS / 2)
        except (Exception, asyncio.CancelledError):
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(runtime.close(), remaining)

    cleanup = asyncio.create_task(finish())
    cancellation = None
    try:
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as error:
                if cleanup.cancelled():
                    if cancellation is not None:
                        cancellation.cleanup_completed = False
                        raise cancellation from error
                    raise
                cancellation = cancellation or error
                # Cancelling the cooperative pass starts the resumable retry.
                # Outer cancellation never cancels the force-close pass.
                if not first_pass.done():
                    first_pass.cancel()
    except Exception as error:
        if cancellation is not None:
            cancellation.cleanup_completed = False
            raise cancellation from error
        raise
    if cancellation is not None:
        cancellation.cleanup_completed = True
        raise cancellation
