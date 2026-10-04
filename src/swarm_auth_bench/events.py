"""Authoritative append-only episode records, owned by the trusted controller."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class EventLog:
    def __init__(self, directory: Path, run_id: str) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self._stream = (directory / "events.jsonl").open("x", encoding="utf-8", newline="\n")

    def emit(
        self, kind: str, agent_id: str | None = None, *, logical_time: float | None = None, **data: Any,
    ) -> dict[str, Any]:
        record = {
            "schema_version": 1, "run_id": self.run_id, "sequence": len(self.events),
            "kind": kind, "agent_id": agent_id,
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - self.started,
            "logical_time": logical_time, "data": data,
        }
        encoded = canonical_json(record)
        self._stream.write(encoded + "\n")
        self._stream.flush()
        self.events.append(record)
        return record

    def artifact(self, name: str, value: Any) -> None:
        if Path(name).name != name or name in {".", ".."} or not name.endswith(".json"):
            raise ValueError("artifact name must be a simple JSON filename")
        path = self.directory / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()


def read_events(path: Path) -> list[dict[str, Any]]:
    result = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            raise ValueError(f"empty event at line {index + 1}")
        event = json.loads(line)
        if event.get("sequence") != index:
            raise ValueError(f"nonconsecutive event sequence at line {index + 1}")
        if result and event.get("run_id") != result[0]["run_id"]:
            raise ValueError("mixed run identifiers")
        result.append(event)
    return result

