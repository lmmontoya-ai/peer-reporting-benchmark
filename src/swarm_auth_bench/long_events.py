"""Streaming, hash-chained controller events for runs that outlive RAM retention."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import canonical_json, content_hash

GENESIS_HASH = "0" * 64


class StreamingEventLog:
    """EventLog's write interface, with constant event-history memory.

    Every event is flushed before return; fsync also runs every ``fsync_every``
    events and at close. Any write, flush, or fsync failure poisons this instance
    and propagates to the caller. There is deliberately no ``events`` list.
    """

    def __init__(self, directory: Path, run_id: str, *, fsync_every: int = 64) -> None:
        if type(fsync_every) is not int or fsync_every < 1:
            raise ValueError("fsync_every must be a positive integer")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.run_id = run_id
        self.started = time.monotonic()
        self.count = 0
        self.last_hash = GENESIS_HASH
        self.fsync_every = fsync_every
        self._failure: BaseException | None = None
        self._lock = threading.RLock()
        self._stream = (directory / "events.jsonl").open("x", encoding="utf-8", newline="\n")

    def _check(self) -> None:
        if self._failure is not None:
            raise RuntimeError("event log failed; run must stop") from self._failure
        if self._stream.closed:
            raise ValueError("event log is closed")

    def emit(
        self, kind: str, agent_id: str | None = None, *, logical_time: float | None = None, **data: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._check()
            record = {
                "schema_version": 2, "run_id": self.run_id, "sequence": self.count,
                "kind": kind, "agent_id": agent_id,
                "wall_time": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": time.monotonic() - self.started,
                "logical_time": logical_time, "data": data, "previous_hash": self.last_hash,
            }
            record["hash"] = content_hash(record)
            encoded = canonical_json(record) + "\n"
            try:
                if self._stream.write(encoded) != len(encoded):
                    raise OSError("short event write")
                self._stream.flush()
                if (self.count + 1) % self.fsync_every == 0:
                    os.fsync(self._stream.fileno())
            except BaseException as error:
                self._failure = error
                raise
            self.count += 1
            self.last_hash = record["hash"]
            return record

    def artifact(self, name: str, value: Any) -> None:
        if (Path(name).name != name or "/" in name or "\\" in name
                or name in {".", "..", "event-chain.json"} or not name.endswith(".json")):
            raise ValueError("artifact name must be a simple, nonreserved JSON filename")
        with self._lock:
            self._check()
            self._artifact(name, value)

    def _artifact(self, name: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        target = self.directory / name
        temporary = self.directory / (name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                if stream.write(encoded) != len(encoded):
                    raise OSError("short artifact write")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException as error:
            self._failure = error
            raise

    def close(self) -> None:
        with self._lock:
            if self._stream.closed:
                if self._failure is not None:
                    raise RuntimeError("event log failed; run must stop") from self._failure
                return
            try:
                self._check()
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._stream.close()
                self._artifact("event-chain.json", {
                    "schema_version": 1, "run_id": self.run_id, "event_count": self.count,
                    "final_hash": self.last_hash, "algorithm": "sha256-canonical-json",
                    "closed_cleanly": True, "fsync_every": self.fsync_every,
                })
            except BaseException as error:
                self._failure = error
                raise
            finally:
                if not self._stream.closed:
                    self._stream.close()


# Short alias for callers that use the original EventLog name locally.
EventLog = StreamingEventLog


def iter_events(path: Path, *, expected_count: int | None = None,
                expected_hash: str | None = None) -> Iterator[dict[str, Any]]:
    """Validate one record at a time. Exhaust the iterator to verify the tail.

    Supply the separately retained close checkpoint to detect whole-line tail
    deletion. A hash chain alone cannot detect removal of its own final records.
    """
    previous = GENESIS_HASH
    run_id = None
    count = 0
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        for index, line in enumerate(stream):
            if not line.endswith("\n"):
                raise ValueError(f"unterminated event at line {index + 1}")
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("sequence") != index:
                raise ValueError(f"nonconsecutive event sequence at line {index + 1}")
            if event.get("schema_version") != 2 or event.get("previous_hash") != previous:
                raise ValueError(f"invalid event chain at line {index + 1}")
            if index == 0:
                run_id = event.get("run_id")
            if event.get("run_id") != run_id:
                raise ValueError("mixed run identifiers")
            digest = event.get("hash")
            body = {key: value for key, value in event.items() if key != "hash"}
            if not isinstance(digest, str) or digest != hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest():
                raise ValueError(f"event hash mismatch at line {index + 1}")
            previous = digest
            count += 1
            yield event
    if expected_count is not None and count != expected_count:
        raise ValueError("event count differs from checkpoint")
    if expected_hash is not None and previous != expected_hash:
        raise ValueError("final event hash differs from checkpoint")
