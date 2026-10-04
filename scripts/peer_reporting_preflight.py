"""Unauthenticated tool-manifest checks in the qualified Linux guest.

The fake provider runs on loopback and never performs model inference.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from swarm_auth_bench.events import content_hash
from swarm_auth_bench.isolation import GVisorIsolation
from swarm_auth_bench.peer_reporting.catalog import ReviewedPeerRuntime, reviewed_catalog
from swarm_auth_bench.peer_reporting.config import MODELS
from swarm_auth_bench.peer_reporting.schemas import TOOL_DESCRIPTORS
from swarm_auth_bench.peer_reporting.storage import atomic_json, seal
from swarm_auth_bench.runtime import SUPPORTED_CODEX_VERSION


async def run(output: Path) -> dict:
    if sys.platform != "linux":
        raise RuntimeError("preflight requires the dedicated Linux guest")
    output.mkdir(parents=True, exist_ok=False)
    boundary = await asyncio.wait_for(GVisorIsolation().metadata(), 180)
    atomic_json(output / "environment.json", seal(boundary))
    if not boundary.get("verified"):
        raise RuntimeError("guest environment did not pass qualification")
    specs = [{"type": "function", **{key: value[key] for key in
              ("name", "description", "inputSchema")}} for value in TOOL_DESCRIPTORS]
    rows = []
    for model in MODELS:
        runtime = ReviewedPeerRuntime(model=model)
        proc = await asyncio.create_subprocess_exec(runtime.codex_executable, "--version",
                                                    stdout=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), 15)
        version = stdout.decode().strip().removeprefix("codex-cli ")
        if proc.returncode or version != SUPPORTED_CODEX_VERSION:
            raise RuntimeError("Codex binary version differs from the reviewed runtime")
        row = {"kind": "peer_runtime_preflight", "requested_model": model,
               "reasoning_effort": "xhigh", "tool_manifest_hash": content_hash(TOOL_DESCRIPTORS),
               "wire_tool_specs_hash": content_hash(specs),
               "catalog_sha256": reviewed_catalog(model)[1]["catalog_sha256"],
               "codex_version": version, "authenticated_model_calls": 0}
        try:
            row["manifest_attestation"] = await asyncio.wait_for(runtime._probe_manifest(specs), 60)
            row["verified"] = True
        except Exception as error:
            row.update(verified=False, error_type=type(error).__name__, error=str(error))
        finally:
            await runtime.close()
        atomic_json(output / f"{model}.json", seal(row))
        rows.append(row)
        print(json.dumps({"model": model, "verified": row["verified"], "model_calls": 0}), flush=True)
    result = {"kind": "peer_preflight_summary", "verified": all(row["verified"] for row in rows),
              "models": rows, "environment_hash": content_hash(boundary), "authenticated_model_calls": 0}
    atomic_json(output / "summary.json", seal(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    result = asyncio.run(run(parser.parse_args().output))
    raise SystemExit(0 if result["verified"] else 2)


if __name__ == "__main__":
    main()
