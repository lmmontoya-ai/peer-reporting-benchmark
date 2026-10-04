"""Offline evidence tools and explicitly selected, capped live phases."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .collection import build_collection, verify_collection
from .config import StudyConfig, read_json, validate_contract
from .export import export_collection, inspect_collection
from .runner import replay_attempt
from .storage import read_sealed


def _has_live_phase(directory: Path) -> bool:
    return any((directory / f"live-{split}").exists() for split in ("smoke", "collection"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("config", type=Path, nargs="?")
    build = commands.add_parser("build")
    build.add_argument("config", type=Path, nargs="?")
    build.add_argument("--output", type=Path, required=True)
    for name in ("verify", "score"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
    replay = commands.add_parser("replay", help="run an authored offline engineering script")
    replay.add_argument("directory", type=Path)
    replay.add_argument("--assignment")
    replay.add_argument("--script", type=Path)
    export = commands.add_parser("export")
    export.add_argument("directory", type=Path)
    export.add_argument("--output", type=Path, required=True)
    qualify = commands.add_parser("qualify", help="run the separate bounded compatibility phase in the guest")
    qualify.add_argument("directory", type=Path, help="fresh output directory, or retained phase with --resume")
    qualify.add_argument("--config", type=Path, default=Path("configs/peer-reporting-compatibility-v1.json"))
    qualify.add_argument("--caps", type=Path)
    qualify.add_argument("--resume", action="store_true")
    phase_verify = commands.add_parser("verify-phase", help="verify retained live phase evidence without inference")
    phase_verify.add_argument("directory", type=Path)
    for name in ("smoke", "collect"):
        command = commands.add_parser(name, help="run a sealed live phase only after its evidence gates pass")
        command.add_argument("directory", type=Path)
        command.add_argument("--caps", type=Path)
        command.add_argument("--qualification", type=Path, action="append", default=[])
        command.add_argument("--smoke-evidence", type=Path)
        command.add_argument("--source-review", type=Path)
        command.add_argument("--resume", action="store_true")
        if name == "collect":
            command.add_argument("--admission-amendment", type=Path,
                                 help="sealed approved admission-only amendment; required unchanged on later resumes")
    args = parser.parse_args(argv)
    try:
        if args.command in {"validate", "build"}:
            config = StudyConfig.from_dict(read_json(args.config)) if args.config else StudyConfig()
            if args.command == "validate":
                result = {"valid": True, "counts": validate_contract(), "config": config.to_dict(),
                          "live_ready": False, "live_model_calls": 0}
            else:
                result = build_collection(args.output, config)
        elif args.command == "verify":
            inspected = inspect_collection(args.directory)
            quarantined = inspected["offline_status_counts"].get("quarantined", 0)
            result = {**inspected["verification"], "plan_valid": True, "valid": not quarantined,
                      "assignment_status_counts": inspected["status_counts"],
                      "offline_status_counts": inspected["offline_status_counts"],
                      "quarantined_offline_examples": quarantined}
            if _has_live_phase(args.directory):
                from .live_review import inspect_live_collection

                live = inspect_live_collection(args.directory)
                quarantined_live = sum(row["status"].startswith("quarantined") for row in live["rows"])
                result.update(valid=result["valid"] and not quarantined_live,
                              assignment_status_counts=live["status_counts"],
                              quarantined_live_assignments=quarantined_live,
                              verified_model_observations_by_split=live["verified_model_observations_by_split"],
                              live_phase_errors=live["phase_errors"])
        elif args.command == "replay":
            result = replay_attempt(args.directory, args.assignment,
                                    read_json(args.script) if args.script else None)
        elif args.command == "score":
            inspected = inspect_collection(args.directory)
            result = {"live_model_observations": 0, "scores": [
                {"assignment_id": row["assignment_id"], "status": item["status"], "score": item.get("score"),
                 "execution_kind": "authored_offline_replay", "not_a_model_result": True,
                 "reason": item.get("reason")}
                for row in inspected["rows"] for item in row["offline_examples"]]}
            if _has_live_phase(args.directory):
                from .live_review import inspect_live_collection

                live = inspect_live_collection(args.directory)
                result.update(live_model_observations=live["verified_model_observations"],
                              live_model_observations_by_split=live["verified_model_observations_by_split"],
                              live_phase_errors=live["phase_errors"],
                              live_scores=[{key: row[key] for key in
                                           ("assignment_id", "split", "status", "score", "evidence_error")}
                                           for row in live["rows"]])
        elif args.command == "export":
            if _has_live_phase(args.directory):
                from .live_review import export_live_collection

                result = export_live_collection(args.directory, args.output)
            else:
                result = export_collection(args.directory, args.output)
        elif args.command == "verify-phase":
            from .live import verify_phase

            result = verify_phase(args.directory)
        elif args.command == "qualify":
            from .live import reviewed_runtime_factory, run_compatibility

            result = asyncio.run(run_compatibility(
                args.directory, read_json(args.config), runtime_factory=reviewed_runtime_factory,
                caps=read_json(args.caps) if args.caps else None, resume=args.resume))
        else:
            from .live import reviewed_runtime_factory, run_collection_phase

            verify_collection(args.directory)
            manifest = read_sealed(args.directory / "collection-manifest.json")
            config = StudyConfig.from_dict(manifest["config"])
            split = "smoke" if args.command == "smoke" else "collection"
            caps = config.caps_for(split)
            if args.caps:
                config.require_matching_caps(read_json(args.caps), split)
            if caps is None:
                raise ValueError("live phase requires frozen numerical caps; no model session was created")
            result = asyncio.run(run_collection_phase(
                args.directory, split, caps=caps,
                runtime_factory=reviewed_runtime_factory, compatibility_directories=args.qualification,
                smoke_directory=args.smoke_evidence, source_review=args.source_review, resume=args.resume,
                admission_amendment=getattr(args, "admission_amendment", None)))
    except (OSError, ValueError, TypeError, KeyError) as error:
        # An error after admission cannot be asserted to have spent zero calls.
        print(json.dumps({"error": str(error), "live_model_calls": None if
                          args.command in {"qualify", "smoke", "collect"} else 0}))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    if args.command in {"qualify", "smoke", "collect"}:
        return 0 if (result.get("status_counts", {}).get("archived") == result["maximum_live_calls"]
                     and all(row.get("check_passed") for row in result["entries"])) else 2
    return 2 if args.command == "verify" and not result["valid"] else 0
