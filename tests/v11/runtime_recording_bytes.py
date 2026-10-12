"""Capture default recordings with the current or archived fake transport.

Regenerate the pre-WP1 hash from a separate checkout of 0630195:
    uv run --offline --extra dev python tests/v11/runtime_recording_bytes.py --checkout <path>
Run that command with each baseline interpreter. No provider or subprocess runtime is used.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


async def default_recording_bytes(tmp_path, monkeypatch):
    # Keep benchmark imports here so the generator can select the archived checkout first.
    from swarm_auth_bench import events, long_events
    from swarm_auth_bench import runtime as core
    from swarm_auth_bench.peer_reporting import world as p1_world
    from swarm_auth_bench.peer_reporting_v11 import live, world
    from swarm_auth_bench.peer_reporting_v11.bundle import load_bundle
    from swarm_auth_bench.peer_reporting_v11.lanes import lane_caps
    from swarm_auth_bench.peer_reporting_v11.live_runtime import run_live_observer

    from .live_fakes import FakeTransport, caps_record, fake_version
    from .pressure_fakes import pressure_fixture, social_fixture
    from .review_helpers import fixture as real_fixture

    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    # This byte guard controls transport timing. Separate tests exercise the
    # real thread-backed writes, races, drain deadlines and cleanup retries.
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    fixed_time = SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 1234.0)
    for module in (core, long_events, events, p1_world, world):
        monkeypatch.setattr(module, "time", fixed_time)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(long_events, "datetime", FixedDateTime)
    monkeypatch.setattr(events, "datetime", FixedDateTime)
    recordings = {}
    for level in ("L0", "L1", "L2", "L3", "L4", "S", "P"):
        sample = (pressure_fixture(pressure="neutral") if level == "P" else social_fixture() if level == "S"
                  else real_fixture(level))
        home = tmp_path / level / "home"
        home.mkdir(parents=True)
        bundle = load_bundle().tool_set("peer-reporting-v11-tools-v2") if level == "P" else load_bundle()
        script = [("tool", "read_channel", {"after_event_id": None, "limit": 128}),
                  ("usage", 100), ("message", "Task complete.")]
        transport = FakeTransport("gpt-6-sol", "low", script, home)
        caps = lane_caps(caps_record(), "smoke", 1)
        proof = await live.manifest_preflight(transport, transport.model, caps, reasoning_effort="low",
                                             bundle=bundle, version_reader=fake_version)
        result = await run_live_observer(sample, "Exact instructions.", directory=home.parent / "attempt",
                                         attempt_id="fixed-attempt", requested_model=transport.model,
                                         reasoning_effort="low", world_mode="normal", bundle=bundle, caps=caps,
                                         qualification=proof, runtime=transport, clock=lambda: 0.0)
        assert not result["infrastructure_failures"]
        recordings[level] = {"preflight": proof, "result": result,
                             "thread_request": transport._thread_params(bundle.wire_tool_specs(),
                                                                         Path("swarm-codex-controller-fixed"),
                                                                         "Exact instructions."),
                             "turn_request": transport._turn_params("fixed-thread", sample["packet"])}
    return (json.dumps(recordings, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main():
    global __package__

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path, help="separate pre-WP1 checkout of 0630195")
    args = parser.parse_args()
    checkout = args.checkout.resolve(strict=True)
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    if commit != "06301953a344bf0cf1c701519e66ef8e770a59d2":
        parser.error("--checkout must be at pre-WP1 commit 0630195")
    if not (checkout / "src").is_dir() or not (checkout / "tests").is_dir():
        parser.error("--checkout must contain the archived src/ and tests/ files")
    sys.path[:0] = [str(checkout / "src"), str(checkout / "tests")]
    # Direct script execution needs the archived package for relative fake imports.
    __package__ = "v11"

    import pytest

    with tempfile.TemporaryDirectory(prefix="pre-wp1-recordings-") as directory:
        with pytest.MonkeyPatch.context() as monkeypatch:
            encoded = asyncio.run(default_recording_bytes(Path(directory), monkeypatch))
    print(f"commit={commit} python={platform.python_version()} sha256={hashlib.sha256(encoded).hexdigest()}")


if __name__ == "__main__":
    main()
