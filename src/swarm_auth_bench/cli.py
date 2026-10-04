"""Command line entry points for configuration, isolation, runs, and reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from dataclasses import replace
from pathlib import Path

from .config import EpisodeConfig
from .report import build_report, write_report


async def _run(config: EpisodeConfig, output: Path) -> Path:
    from .harness import EpisodeRunner
    from .isolation import GVisorIsolation
    from .runtime import CodexRuntime

    runtime = CodexRuntime(model=config.model, reasoning_effort=config.reasoning_effort)
    isolation = GVisorIsolation()
    runner = EpisodeRunner(config, isolation, runtime, output)
    result = await runner.run()
    print(json.dumps({"run_directory": str(result)}, sort_keys=True), flush=True)
    return result


async def _doctor(probe: bool) -> None:
    import shutil

    result = {"system": platform.platform(), "python": platform.python_version(),
              "docker": shutil.which("docker"), "codex": shutil.which("codex"),
              "auth_file_present": (Path.home() / ".codex" / "auth.json").is_file()}
    if probe:
        from .isolation import GVisorIsolation

        result["isolation"] = await GVisorIsolation().metadata()
    print(json.dumps(result, indent=2))


async def _pilot(matrix_path: Path, output: Path, parallel: int, limit: int | None) -> None:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if matrix.get("study_type") != "exploratory_feasibility_pilot":
        raise ValueError("pilot matrix must declare its exploratory status")
    base = EpisodeConfig.from_dict(matrix["base"])
    jobs = []
    repetitions = matrix.get("repetitions", 1)
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    for condition in matrix["conditions"]:
        combined = base.to_dict()
        combined.update(condition)
        for repetition in range(repetitions):
            config = EpisodeConfig.from_dict(combined)
            jobs.append(replace(config, seed=base.seed + repetition))
    if limit:
        jobs = jobs[:limit]
    semaphore = asyncio.Semaphore(parallel)
    failures = []

    async def execute(config):
        async with semaphore:
            try:
                print(json.dumps({"starting": config.name, "seed": config.seed}), flush=True)
                directory = await _run(config, output)
                summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
                if not summary["valid"]:
                    failures.append({"condition": config.name, "seed": config.seed,
                                     "config_hash": config.digest, "error": "invalid_episode",
                                     "run_directory": str(directory)})
                return directory
            except Exception as error:
                failures.append({"condition": config.name, "seed": config.seed,
                                 "config_hash": config.digest,
                                 "error": type(error).__name__, "message": str(error)})
                print(json.dumps({"failed": config.name, "error": str(error)}), flush=True)
                return None

    paths = [p for p in await asyncio.gather(*(execute(config) for config in jobs)) if p]
    output.mkdir(parents=True, exist_ok=True)
    (output / "pilot_failures.json").write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
    if paths:
        write_report(build_report(paths), output / "report")
    if failures:
        raise RuntimeError(f"{len(failures)} pilot episodes failed; inspect {output / 'pilot_failures.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled swarm authorization experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect the environment; optionally run real isolation probes")
    doctor.add_argument("--probe", action="store_true")
    validate = commands.add_parser("validate-config", help="Validate one episode configuration")
    validate.add_argument("config", type=Path)
    run = commands.add_parser("run", help="Run real agents inside verified gVisor workers")
    run.add_argument("config", type=Path)
    run.add_argument("--output", type=Path, default=Path("runs"))
    pilot = commands.add_parser("pilot", help="Run the declared exploratory matrix")
    pilot.add_argument("matrix", type=Path)
    pilot.add_argument("--output", type=Path, default=Path("runs/pilot"))
    pilot.add_argument("--parallel-episodes", type=int, default=1)
    pilot.add_argument("--limit", type=int)
    report = commands.add_parser("report", help="Summarize completed episodes without inventing semantic labels")
    report.add_argument("directories", nargs="+", type=Path)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--semantic-review", type=Path, help="Attach independently recorded provisional model labels")
    args = parser.parse_args()
    if args.command == "doctor":
        asyncio.run(_doctor(args.probe))
    elif args.command == "validate-config":
        config = EpisodeConfig.load(args.config)
        print(json.dumps({"valid": True, "config_hash": config.digest, "config": config.to_dict()}, indent=2))
    elif args.command == "run":
        directory = asyncio.run(_run(EpisodeConfig.load(args.config), args.output))
        if not json.loads((directory / "summary.json").read_text(encoding="utf-8"))["valid"]:
            raise RuntimeError(f"episode invalidated by infrastructure failure; inspect {directory}")
    elif args.command == "pilot":
        if args.parallel_episodes < 1 or (args.limit is not None and args.limit < 1):
            parser.error("parallel episodes and limit must be positive")
        asyncio.run(_pilot(args.matrix, args.output, args.parallel_episodes, args.limit))
    elif args.command == "report":
        write_report(build_report(args.directories, semantic_review_artifact=args.semantic_review), args.output)


if __name__ == "__main__":
    main()
