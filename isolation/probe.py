"""Run the live gVisor qualification suite inside SwarmAuthBench."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm_auth_bench.isolation import GVisorIsolation  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-only", action="store_true")
    args = parser.parse_args()
    isolation = GVisorIsolation()
    result = await (isolation.probe_boundaries() if args.worker_only else isolation.metadata())
    result["qualified_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["isolation_source_sha256"] = hashlib.sha256(
        (Path(__file__).resolve().parents[1] / "src/swarm_auth_bench/isolation.py").read_bytes()
    ).hexdigest()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
