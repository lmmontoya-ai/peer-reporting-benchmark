"""Small reproducibility and resource records from the trusted controller."""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path
from typing import Any


def implementation_hashes() -> dict[str, str]:
    package = Path(__file__).parent
    return {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(package.rglob("*.py"))}


def resource_snapshot() -> dict[str, Any]:
    record: dict[str, Any] = {"controller_pid": os.getpid(), "logical_cpus": os.cpu_count(),
                              "architecture": platform.machine()}
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        record["guest_memory"] = {name: value.strip() for name, value in (
            line.split(":", 1) for line in meminfo.read_text().splitlines()
        ) if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}}
    status = Path("/proc/self/status")
    if status.is_file():
        fields = {}
        for line in status.read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                if key in {"VmRSS", "VmHWM", "Threads"}:
                    fields[key] = value.strip()
        record["linux_process_status"] = fields
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        record.update({"user_cpu_seconds": usage.ru_utime, "system_cpu_seconds": usage.ru_stime,
                       "maximum_resident_set_raw": usage.ru_maxrss,
                       "maximum_resident_set_unit": "KiB on Linux; bytes on macOS"})
    except ImportError:
        record["resource_usage_available"] = False
    return record
