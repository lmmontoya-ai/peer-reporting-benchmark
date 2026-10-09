"""v1.1 offline tools and explicitly authorized, capped live phases.

Offline commands (validate, build, verify, replay, export-review, propose-caps,
freeze-caps, abandon-root, reconcile-cleanup, ledger-repair-binding, repair-ledger) never start a model session.
Live commands (compatibility,
calibration, smoke, collection)
refuse to run without frozen caps equal to the sealed plan's caps and a sealed
user execution authorization that names the exact plan.

A behavioral root built from a study that already has roots of the same phase
names each of them with ``--prior-root``. ``build`` excludes their consumed
assignments; ``verify``, ``export-review``, and the live commands recheck them.
Behavioral roots are registered in their study directory, the consumed-attempt
ledger of record: ``build``, ``verify``, ``export-review``, and the live commands
take ``--study`` and refuse a root that is not registered there or not at its
registered path. ``build`` creates a behavioral root inside the study, at
``STUDY/roots/<name>``. A collection ``build`` needs the frozen review plan
(``--review-plan``), which is verified against the study before the plan is
built. A calibration ``build`` may select arms (``--arm``, repeatable); other
phases refuse it. ``abandon-root`` seals the abandonment of a root that never started an
attempt. ``reconcile-cleanup`` runs the live environment check and seals a
cleanup reconciliation, the only way to clear cleanup debt. ``--amendment``
records a sealed, user-approved amendment in the study before the command runs.
``root/STOP`` and ``--stop-file`` are soft stops; ``root/HARD_STOP`` and
``--hard-stop-file`` truncate active attempts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack
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


def _study(command: argparse.ArgumentParser, help_text: str) -> None:
    command.add_argument("--study", type=Path, help=help_text)


def _amendments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--amendment", dest="amendments", type=Path, action="append", default=[],
                         help="sealed, user-approved amendment to record in the study first (repeatable)")


def _write_new_json_files(records: dict[Path, dict]) -> None:
    """Exclusively create outputs; a conflict leaves every existing file intact."""
    encoded = {path: json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
               for path, value in records.items()}
    created = []
    try:
        with ExitStack() as stack:
            streams = {}
            for path in records:
                path.parent.mkdir(parents=True, exist_ok=True)
                streams[path] = stack.enter_context(path.open("x", encoding="utf-8", newline="\n"))
                created.append(path)
            for path, stream in streams.items():
                stream.write(encoded[path])
    except BaseException:
        for path in created:
            path.unlink()
        raise


def _review_commands(commands) -> None:
    plan = commands.add_parser("review-plan", help="seal the human review plan from a study manifest; reads no "
                                                   "outcome")
    plan.add_argument("study", type=Path, help="sealed v1.1 study directory")
    plan.add_argument("--output", type=Path, help="new plan file (default: STUDY/review-plan.json)")
    packets = commands.add_parser("review-packets", help="write masked reviewer packets and private bindings")
    packets.add_argument("export", type=Path, help="directory written by export-review")
    packets.add_argument("--plan", type=Path, help="sealed review plan (P defaults to export selection)")
    packets.add_argument("--output", type=Path, required=True, help="fresh output directory")
    upload = commands.add_parser("validate-review-upload", help="validate returned review labels")
    upload.add_argument("upload", type=Path)
    upload.add_argument("--packet", type=Path, required=True, help="the reviewer packet the labels answer")
    upload.add_argument("--controller", type=Path, help="researcher-only controller record; checks the bindings")


def _review(args: argparse.Namespace) -> dict:
    from datetime import datetime, timezone

    from ..peer_reporting.storage import atomic_json, read_sealed
    from .collection import STUDY_MANIFEST
    from .review import write_review_packets
    from .review_plan import build_review_plan

    if args.command == "review-plan":
        output = args.output or args.study / "review-plan.json"
        if output.exists():
            raise ValueError(f"{output} already exists; a frozen review plan is never overwritten")
        plan = build_review_plan(read_sealed(args.study / STUDY_MANIFEST),
                                 frozen_at_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(output, plan)
        return {"output": str(output), "seal_hash": plan["seal_hash"], "seed": plan["seed"],
                "study_manifest_hash": plan["study_manifest_hash"], "counts": plan["counts"], "live_model_calls": 0}
    if args.command == "review-packets":
        plan = read_sealed(args.plan) if args.plan else read_sealed(args.export / "index.json").get("pressure_review_selection")
        if plan is None:
            raise ValueError("review-packets requires --plan for earlier levels")
        return write_review_packets(args.export, plan, args.output)
    from .review import validate_review_upload

    return validate_review_upload(read_json(args.upload), read_json(args.packet),
                                  controller=read_sealed(args.controller) if args.controller else None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m swarm_auth_bench.peer_reporting_v11", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check the tool binding and optional caps or authorization files")
    validate.add_argument("--caps", type=Path)
    validate.add_argument("--authorization", type=Path)
    validate.add_argument("--root", type=Path, help="sealed live root that the authorization must name")
    propose = commands.add_parser("propose-caps", help="propose resource caps from verified archives; no model call")
    propose.add_argument("--study", type=Path, required=True, help="sealed study whose planned rows size the lanes")
    propose.add_argument("--phase", choices=("calibration", "smoke", "collection"), required=True,
                         help="smoke or collection sizes both phases from calibration evidence")
    propose.add_argument("--root", type=Path, action="append", nargs="+", required=True,
                         help="verified input roots, including every sealed prior root (repeatable)")
    propose.add_argument("--output", type=Path, required=True, help="new proposal JSON file; never overwritten")
    freeze = commands.add_parser("freeze-caps", help="freeze a proposal with recorded user approval; no model call")
    freeze.add_argument("--proposal", type=Path, required=True)
    freeze.add_argument("--approval-text", required=True)
    freeze.add_argument("--output", type=Path, required=True,
                        help="new raw caps JSON; approval is retained in <output-stem>-approval.json")
    build = commands.add_parser("build", help="seal a live phase plan offline; no model call")
    build.add_argument("root", type=Path, help="fresh output directory; a behavioral root must be STUDY/roots/<name>")
    build.add_argument("--phase", choices=PHASES, required=True)
    build.add_argument("--caps", type=Path, required=True)
    build.add_argument("--revision", required=True)
    from .schemas import TOOL_SCHEMA_VERSION_P
    build.add_argument("--tool-schema-version", choices=(TOOL_SCHEMA_VERSION, TOOL_SCHEMA_VERSION_P),
                       help="compatibility only: select v1 (default) or the level P v2 probe")
    _study(build, "sealed v1.1 study directory (calibration, smoke, collection); the root is registered there")
    build.add_argument("--compatibility", type=Path, action="append", default=[])
    build.add_argument("--smoke", type=Path)
    build.add_argument("--review-plan", type=Path,
                       help="the frozen review plan (collection only); verified against the study, then retained")
    build.add_argument("--arm", dest="arms", action="append",
                       help="calibration only: plan only this arm's rows (repeatable); sealed as selected_arms")
    build.add_argument("--pilot-decision", type=Path, help="sealed pilot decision required for P core arms")
    _prior_roots(build)
    _amendments(build)
    decision = commands.add_parser("pilot-decision", help="compute and seal the P pilot decision offline")
    decision.add_argument("--gate-export", type=Path, action="append", required=True)
    decision.add_argument("--pilot-export", type=Path, action="append", required=True)
    decision.add_argument("--flag-resolutions", type=Path, help="JSON mapping of flagged trial IDs to resolutions")
    decision.add_argument("--scripting-remarks", type=Path, help="JSON list of confirmed scripting trial IDs")
    decision.add_argument("--ceiling-choice", choices=("a", "b", "c"))
    decision.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify a sealed live root and its retained lane evidence")
    verify.add_argument("root", type=Path)
    _study(verify, "study directory in which a behavioral root is registered")
    _prior_roots(verify)
    abandon = commands.add_parser("abandon-root", help="seal the abandonment of a registered root that never "
                                                       "started an attempt; no model call")
    abandon.add_argument("study", type=Path, help="study directory in which the root is registered")
    abandon.add_argument("--plan-hash", required=True, help="the registered root's plan hash")
    abandon.add_argument("--root", type=Path, help="the exact registered root directory; required for a finalized root")
    abandon.add_argument("--reason", required=True)
    cleanup = commands.add_parser("reconcile-cleanup", help="seal a cleanup reconciliation after the live environment "
                                                            "check shows that no runtime of the attempt remains; no "
                                                            "model call")
    cleanup.add_argument("root", type=Path, help="the live root at its registered path")
    cleanup.add_argument("--attempt", dest="attempts", action="append", required=True,
                         help="attempt ID with cleanup debt (repeatable)")
    cleanup.add_argument("--reason", required=True)
    _study(cleanup, "study directory in which a behavioral root is registered")
    repair = commands.add_parser("repair-ledger", help="A4: seal a unique, journal-exact single-bit budget ledger "
                                                      "repair; no model call")
    repair.add_argument("root", type=Path, help="the live root at its registered path")
    repair.add_argument("--study", type=Path, required=True, help="study in which the root is registered")
    repair.add_argument("--lane", required=True, help="lane ID in the sealed root")
    repair.add_argument("--reason", required=True)
    repair.add_argument("--approval-text", required=True)
    repair.add_argument("--binding", type=Path, required=True, help="approved sealed binding from committed evidence")
    binding = commands.add_parser("ledger-repair-binding", help="A4.1: bind committed corruption evidence offline")
    binding.add_argument("evidence", type=Path, help="committed ledger, identity and journal directory")
    binding.add_argument("--study", type=Path, required=True, help="study directory name or path")
    binding.add_argument("--root", type=Path, required=True, help="registered root directory name or path")
    binding.add_argument("--lane", required=True)
    binding.add_argument("--plan-hash", required=True, help="sealed live root plan hash")
    binding.add_argument("--commit", required=True, help="Git commit containing the exact evidence bytes")
    binding.add_argument("--approval-text", required=True)
    binding.add_argument("--output", type=Path, required=True)
    for name in ("build-study", "verify-study"):
        study = commands.add_parser(name, help="build or verify the sealed study offline; no model call")
        study.add_argument("directory", type=Path)
        study.add_argument("--caps", type=Path, required=True, help="frozen v1.1 caps record")
    replay = commands.add_parser("replay", help="authored offline replay through the v1.1 world; no model call")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, help="replay sealed live-plan entries")
    source.add_argument("--matrix", action="store_true",
                        help="replay earlier levels, variants, F modes and efforts, plus all five S arms "
                             "on both calibration templates")
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--assignment", action="append")
    replay.add_argument("--template", default="release-request", help="template for earlier matrix levels")
    replay.add_argument("--split", default="collection", help="split for earlier matrix levels")
    replay.add_argument("--seed", type=int, default=1101, help="seed for earlier matrix levels")
    replay.add_argument("--no-score", action="store_true")
    export = commands.add_parser("export-review", help="export verified attempts in the live review shape")
    export.add_argument("root", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--no-score", action="store_true")
    _study(export, "study directory in which the root is registered")
    _prior_roots(export)
    _review_commands(commands)
    for name in LIVE_COMMANDS:
        command = commands.add_parser(name, help=f"run the sealed {name} phase after explicit authorization")
        command.add_argument("root", type=Path, help="sealed live root built for this phase")
        command.add_argument("--caps", type=Path, required=True, help="frozen caps record; must equal the sealed caps")
        command.add_argument("--authorization", type=Path, required=True,
                             help="sealed user execution authorization naming this plan")
        command.add_argument("--compatibility", type=Path, action="append", default=[])
        command.add_argument("--smoke", type=Path)
        command.add_argument("--stop-file", type=Path,
                             help="an extra soft stop file (no new admission); root/STOP is always watched")
        command.add_argument("--hard-stop-file", type=Path,
                             help="an extra hard stop file (truncates active attempts); root/HARD_STOP is always "
                                  "watched")
        _study(command, "study directory in which a behavioral root is registered")
        _prior_roots(command)
        _amendments(command)
    return parser


def _run(args: argparse.Namespace) -> dict:
    from .bundle import load_bundle

    if args.command == "pilot-decision":
        from .pilot_decision import build_pilot_decision, export_reference

        record = build_pilot_decision(gate_exports=[export_reference(path) for path in args.gate_export],
            pilot_exports=[export_reference(path) for path in args.pilot_export],
            flag_resolutions=read_json(args.flag_resolutions) if args.flag_resolutions else None,
            confirmed_scripting_remarks=read_json(args.scripting_remarks) if args.scripting_remarks else None,
            ceiling_choice=args.ceiling_choice)
        _write_new_json_files({args.output: record})
        return {"output": str(args.output), "seal_hash": record["seal_hash"], "decision": record["decision"],
                "Psel": record["Psel"], "eligible_lanes": record["eligible_lanes"], "live_model_calls": 0}
    if args.command == "propose-caps":
        from .config import load_protocol
        from .resources import propose_caps

        proposal = propose_caps([root for group in args.root for root in group], study_directory=args.study,
                                phase=args.phase, protocol=load_protocol())
        _write_new_json_files({args.output: proposal})
        return {"output": str(args.output), "caps": proposal["caps"], "status": proposal["status"],
                "proposal_hash": proposal["seal_hash"], "live_model_calls": 0}
    if args.command == "freeze-caps":
        from .resources import freeze_caps

        frozen = freeze_caps(read_json(args.proposal), args.approval_text)
        approval_path = args.output.with_name(f"{args.output.stem}-approval.json")
        _write_new_json_files({args.output: frozen["caps"], approval_path: frozen})
        return {"output": str(args.output), "approval_record": str(approval_path), "caps": frozen["caps"],
                "status": frozen["status"], "live_model_calls": 0}
    if args.command in ("review-plan", "review-packets", "validate-review-upload"):
        return _review(args)
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
            approval = validate_authorization(read_json(args.authorization), read_live_plan(args.root),
                                              root=args.root)
            result.update(authorization_phase=approval["phase"], authorization_hash=approval["seal_hash"])
        return result
    if getattr(args, "amendments", None):
        from .live import record_amendment

        # An amendment accepts failed attempts of the smoke root being run, or of the collection gate's smoke root.
        smoke_root = args.root if args.command == "smoke" else getattr(args, "smoke", None)
        if args.study is None or smoke_root is None:
            raise ValueError("--amendment requires --study and the smoke root (the smoke command's root or --smoke)")
        for path in args.amendments:
            record_amendment(args.study, read_json(path), smoke_roots=[smoke_root], bundle=load_bundle())
    if args.command == "abandon-root":
        from .live import abandon_root

        return abandon_root(args.study, args.plan_hash, reason=args.reason, root=args.root, bundle=load_bundle())
    if args.command == "reconcile-cleanup":
        from .live import reconcile_cleanup

        return asyncio.run(reconcile_cleanup(args.root, sorted(set(args.attempts)), reason=args.reason,
                                             study_directory=args.study, bundle=load_bundle()))
    if args.command == "repair-ledger":
        from .ledger_repair import repair_ledger

        return repair_ledger(args.root, study_directory=args.study, lane_id=args.lane, reason=args.reason,
                             approval_text=args.approval_text, binding_path=args.binding)
    if args.command == "ledger-repair-binding":
        from .ledger_repair import build_ledger_repair_binding

        binding = build_ledger_repair_binding(args.evidence, study=args.study.name, root=args.root.name, lane_id=args.lane,
                                             plan_hash=args.plan_hash, commit=args.commit,
                                             approval_text=args.approval_text)
        _write_new_json_files({args.output: binding})
        return {"output": str(args.output), "binding": binding, "live_model_calls": 0}
    if args.command == "build":
        from .live import STUDY_MANIFEST, build_phase_plan, prepare_live_root, validate_arm_selection

        if args.arms is not None:
            validate_arm_selection(args.phase, args.arms)
        bundle = load_bundle()
        review = None
        if args.phase == "collection":
            if args.review_plan is None or args.study is None:
                raise ValueError("collection build requires --study and the frozen review plan (--review-plan)")
            from ..peer_reporting.storage import read_sealed
            from .review_plan import verify_review_plan

            review = read_json(args.review_plan)
            errors = verify_review_plan(review, read_sealed(args.study / STUDY_MANIFEST))
            if errors:
                raise ValueError(f"the review plan failed verification against the study: {errors[:5]}")
        elif args.review_plan is not None:
            raise ValueError("--review-plan applies only to a collection build")
        decision_verifier = None
        if args.pilot_decision:
            from .pilot_decision import validate_core_decision

            decision_verifier = validate_core_decision
        plan = build_phase_plan(args.phase, read_json(args.caps), revision=args.revision, study_directory=args.study,
                                compatibility_directories=args.compatibility, smoke_directory=args.smoke,
                                prior_roots=args.prior_roots, bundle=bundle, review_plan=review, arms=args.arms,
                                tool_schema_version=args.tool_schema_version,
                                pilot_decision=read_sealed(args.pilot_decision) if args.pilot_decision else None,
                                pilot_decision_verifier=decision_verifier)
        study = args.study if args.phase != "compatibility" else None
        return {**prepare_live_root(args.root, plan, study_directory=study, prior_roots=args.prior_roots,
                                    bundle=bundle, review_plan=review,
                                    pilot_decision_verifier=decision_verifier), "selected_arms": plan[0]["selected_arms"]}
    if args.command == "verify":
        from .live import verify_live_root

        report = verify_live_root(args.root, bundle=load_bundle(), prior_roots=args.prior_roots,
                                  study_directory=args.study)
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
        from .review_plan import pressure_review_selection

        scorer, summarize = _scorer(not args.no_score)
        return export_live_review(args.root, args.output, study_directory=args.study, prior_roots=args.prior_roots,
                                  bundle=load_bundle(), scorer=scorer, summarize=summarize,
                                  pressure_review_selector=pressure_review_selection)
    from .live import read_live_plan, reviewed_runtime_factory, run_live_phase

    plan = read_live_plan(args.root)
    if plan["phase"] != args.command:
        raise ValueError(f"{args.root} is a sealed {plan['phase']} root, not {args.command}")
    return asyncio.run(run_live_phase(
        args.root, caps_record=read_json(args.caps), authorization=read_json(args.authorization),
        runtime_factory=reviewed_runtime_factory, compatibility_directories=args.compatibility,
        smoke_directory=args.smoke, stop_file=args.stop_file, hard_stop_file=args.hard_stop_file,
        prior_roots=args.prior_roots, study_directory=args.study, bundle=load_bundle()))


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
