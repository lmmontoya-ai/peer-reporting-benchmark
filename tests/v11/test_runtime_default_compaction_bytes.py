"""Default compaction stays byte-identical to the archived b0476d2 observer."""

import asyncio
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

from swarm_auth_bench import events, long_events
from swarm_auth_bench import runtime as core
from swarm_auth_bench.peer_reporting import live as p1_live
from swarm_auth_bench.peer_reporting import world as p1_world
from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11 import live, live_runtime, world
from swarm_auth_bench.peer_reporting_v11.live_review import export_live_review
from swarm_auth_bench.peer_reporting_v11.receipts import receipt_bytes

from . import live_fakes


def test_default_compaction_attempts_receipts_and_exports_match_b0476d2(tmp_path, monkeypatch):
    repository = Path(__file__).resolve().parents[2]
    source = subprocess.run(["git", "-C", str(repository), "show",
                             "b0476d2:src/swarm_auth_bench/peer_reporting_v11/live_runtime.py"],
                            capture_output=True, check=True).stdout.decode("utf-8")
    baseline = ModuleType(live_runtime.__package__ + "._b0476d2_observer")
    monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
    exec(compile(source, "b0476d2/live_runtime.py", "exec"), baseline.__dict__)
    # The current fake transport checks this opt-in setting on its controller.
    # The archived controller predates the setting; None is its default.
    baseline._Controller.reasoning_summary = None

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    wall = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    fixed_time = SimpleNamespace(monotonic=lambda: 0.0, time=lambda: wall)
    for module in (core, events, long_events, p1_world, world, p1_live, live, baseline):
        monkeypatch.setattr(module, "time", fixed_time)
    for module in (events, long_events, p1_live, live, live_fakes):
        monkeypatch.setattr(module, "datetime", FixedDateTime)

    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(live.secrets, "token_hex", lambda size: "0" * (2 * size))
    original_harness = live_fakes.Harness

    class CompactionHarness(original_harness):
        def __init__(self, root, script_for, **kwargs):
            def scripts(model, effort):
                script = script_for(model, effort)
                if callable(script):
                    return lambda packet: [("compaction",), *script(packet)]
                return [("compaction",), *script]

            super().__init__(root, scripts, **kwargs)

        def kwargs(self, **changes):
            return super().kwargs(clock=lambda: 0.0, ledger_clock=lambda: wall, wall_clock=lambda: wall, **changes)

    monkeypatch.setattr(live_fakes, "Harness", CompactionHarness)
    active = tmp_path / "active"
    current_observer = live_runtime.run_live_observer

    def capture(observer):
        monkeypatch.setattr(live_runtime, "run_live_observer", observer)
        compatibility, _, status, _ = live_fakes.qualified_root(active)
        assert status["status"] == "complete"
        smoked = live_fakes.smoke_root(active, compatibility)
        assert smoked["status"]["status"] == "complete"
        output = active / "export"
        exported = export_live_review(smoked["root"], output, study_directory=smoked["study"],
                                      bundle=live_fakes.fake_bundle())
        assert exported["attempts"] == 12
        archives = list(active.glob("**/attempts/*/attempt.json"))
        assert len(archives) == 18
        for path in archives:
            archive = read_sealed(path)
            assert "runtime_recording" not in archive and "runtime_recording" not in archive["observer_result"]
            assert any(event.get("raw", {}).get("params", {}).get("item", {}).get("type") == "contextCompaction"
                       for event in archive["observer_result"]["raw_events"])
        retained = {str(path.relative_to(active)): path.read_bytes()
                    for path in active.rglob("*") if path.is_file() and path.suffix != ".lock"}
        retained["compatibility-receipt"] = receipt_bytes(compatibility)
        retained["smoke-receipt"] = receipt_bytes(smoked["root"], study_directory=smoked["study"])
        return retained

    original = capture(baseline.run_live_observer)
    # Both replays use the same prepared-root paths and controlled clocks.
    # Only the observer implementation changes; implementation attestations
    # still name the current checkout in both runs.
    assert active.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(active)
    current = capture(current_observer)
    assert current.keys() == original.keys(), (sorted(current.keys() - original.keys()),
                                               sorted(original.keys() - current.keys()))
    differences = [name for name in current if current[name] != original[name]]
    assert differences == []
