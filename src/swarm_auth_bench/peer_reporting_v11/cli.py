"""v1.1 offline tools and explicitly authorized, capped live phases.

Offline commands (validate, build, verify, replay, export-review) never start a
model session. Live commands (compatibility, calibration, smoke, collection)
refuse to run without frozen caps equal to the sealed plan's caps and a sealed
user execution authorization that names the exact plan.

A behavioral root built from a study that already has roots of the same phase
names each of them with ``--prior-root``. ``build`` excludes their consumed
assignments; ``verify``, ``export-review``, and the live commands recheck them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ..peer_reporting.config import read_json
from . import PROTOCOL_ID, TOOL_SCHEMA_VERSION
from .lanes import PHASES

LIVE_COMMANDS = ("compatibility", "calibration", "smoke", "collection")


def _scorer(enabled: bool):
    if not enabled:
        return None, None
    from .score import score_trial, summarize

    return score_trial, summarize


def _prior_roots(command: argparse.ArgumentParser) -> None:
    command.add_argument("--prior-root", dest="prior_roots", type=Path, action="append", default=[],
                         help="every earlier root of the same study and phase (repeat for each)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m swarm_auth_bench.peer_reporting_v11", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check the tool binding and optional caps or authorization files")
    validate.add_argument("--caps", type=Path)
    validate.add_argument("--authorization", type=Path)
    validate.add_argument("--root", type=Path, help="sealed live root that the authorization must name")
    build = commands.add_parser("build", help="seal a live phase plan offline; no model call")
    build.add_argument("root", type=Path, help="fresh output directory")
    build.add_argument("--phase", choices=PHASES, required=True)
    build.add_argument("--caps", type=Path, required=True)
    build.add_argument("--revision", required=True)
    build.add_argument("--study", type=Path, help="sealed v1.1 study directory (calibration, smoke, collection)")
    build.add_argument("--compatibility", type=Path, action="append", default=[])
    build.add_argument("--smoke", type=Path)
    _prior_roots(build)
    verify = commands.add_parser("verify", help="verify a sealed live root and its retained lane evidence")
    verify.add_argument("root", type=Path)
    _prior_roots(verify)
    for name in ("build-study", "verify-study"):
        study = commands.add_parser(name, help="build or verify the sealed study offline; no model call")
        study.add_argument("directory", type=Path)
        study.add_argument("--caps", type=Path, required=True, help="frozen v1.1 caps record")
    replay = commands.add_parser("replay", help="authored offline replay through the v1.1 world; no model call")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, help="replay sealed live-plan entries")
    source.add_argument("--matrix", action="store_true", help="replay every level, variant, F mode, and effort")
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--assignment", action="append")
    replay.add_argument("--template", default="release-request")
    replay.add_argument("--split", default="collection")
    replay.add_argument("--seed", type=int, default=1101)
    replay.add_argument("--no-score", action="store_true")
    export = commands.add_parser("export-review", help="export verified attempts in the live review shape")
    export.add_argument("root", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--no-score", action="store_true")
    _prior_roots(export)
    for name in LIVE_COMMANDS:
        command = commands.add_parser(name, help=f"run the sealed {name} phase after explicit authorization")
        command.add_argument("root", type=Path, help="sealed live root built for this phase")
        command.add_argument("--caps", type=Path, required=True, help="frozen caps record; must equal the sealed caps")
        command.add_argument("--authorization", type=Path, required=True,
                             help="sealed user execution authorization naming this plan")
        command.add_argument("--compatibility", type=Path, action="append", default=[])
        command.add_argument("--smoke", type=Path)
        command.add_argument("--stop-file", type=Path, help="an extra stop file; root/STOP is always watched")
        _prior_roots(command)
    return parser


def _run(args: argparse.Namespace) -> dict:
    from .bundle import load_bundle

    if args.command in ("build-study", "verify-study"):
        from .collection import build_study, verify_study
        from .config import load_protocol
        from .incidents import load_all_templates

        action = build_study if args.command == "build-study" else verify_study
        return action(args.directory, protocol=load_protocol(), templates=load_all_templates(),
                      caps_record=read_json(args.caps))
    if args.command == "validate":
        from .lanes import validate_authorization, validate_caps_record
        from .live import read_live_plan

        bundle = load_bundle()
        result = {"valid": True, "protocol_id": PROTOCOL_ID, "tool_schema_version": TOOL_SCHEMA_VERSION,
                  "tool_manifest_hash": bundle.tool_manifest_hash, "live_model_calls": 0}
        if args.caps:
            record = validate_caps_record(read_json(args.caps), require_frozen=False)
            result.update(caps_status=record["caps_status"], caps_revision=record["revision"])
        if args.authorization:
            if not args.root:
                raise ValueError("--authorization requires --root")
            approval = validate_authorization(read_json(args.authorization), read_live_plan(args.root))
            result.update(authorization_phase=approval["phase"], authorization_hash=approval["seal_hash"])
        return result
    if args.command == "build":
        from .live import build_phase_plan, prepare_live_root

        bundle = load_bundle()
        plan = build_phase_plan(args.phase, read_json(args.caps), revision=args.revision, study_directory=args.study,
                                compatibility_directories=args.compatibility, smoke_directory=args.smoke,
                                prior_roots=args.prior_roots, bundle=bundle)
        study = args.study if args.phase != "compatibility" else None
        return prepare_live_root(args.root, plan, study_directory=study, prior_roots=args.prior_roots, bundle=bundle)
    if args.command == "verify":
        from .live import verify_live_root

        report = verify_live_root(args.root, bundle=load_bundle(), prior_roots=args.prior_roots)
        return {key: value for key, value in report.items() if key != "lanes"} | {
            "lanes": {lane: {key: value for key, value in item.items() if key != "entries"}
                      for lane, item in report["lanes"].items()}}
    if args.command == "replay":
        from .runner import replay_live_root, replay_matrix

        scorer, _ = _scorer(not args.no_score)
        if args.matrix:
            return replay_matrix(args.output, template_id=args.template, split=args.split, seed=args.seed,
                                 bundle=load_bundle(), scorer=scorer)
        return replay_live_root(args.root, args.output, assignment_ids=args.assignment, bundle=load_bundle(),
                                scorer=scorer)
    if args.command == "export-review":
        from .live_review import export_live_review

        scorer, summarize = _scorer(not args.no_score)
        return export_live_review(args.root, args.output, prior_roots=args.prior_roots, bundle=load_bundle(),
                                  scorer=scorer, summarize=summarize)
    from .live import read_live_plan, reviewed_runtime_factory, run_live_phase

    plan = read_live_plan(args.root)
    if plan["phase"] != args.command:
        raise ValueError(f"{args.root} is a sealed {plan['phase']} root, not {args.command}")
    return asyncio.run(run_live_phase(
        args.root, caps_record=read_json(args.caps), authorization=read_json(args.authorization),
        runtime_factory=reviewed_runtime_factory, compatibility_directories=args.compatibility,
        smoke_directory=args.smoke, stop_file=args.stop_file, prior_roots=args.prior_roots, bundle=load_bundle()))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as error:
        # An error after a lane started cannot be asserted to have spent zero calls.
        print(json.dumps({"error": f"{type(error).__name__}: {error}",
                          "live_model_calls": None if args.command in LIVE_COMMANDS else 0}))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str))
    if args.command in LIVE_COMMANDS:
        return 0 if result.get("status") == "complete" and result.get("status_counts", {}).get(
            "archived") == result.get("maximum_live_calls") else 2
    if args.command == "verify-study":
        return 0 if result["valid"] else 2
    return 0
