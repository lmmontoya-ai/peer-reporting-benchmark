"""Read-only, source-bound findings for separately declared prospective collectors."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .config import EpisodeConfig
from .cooperation import analyze_episode
from .events import content_hash
from .predict import BASELINES

REPOSITORY = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "related-prospective-findings-v1"


@dataclass(frozen=True)
class Collection:
    plan_directory: Path
    score_directory: Path
    archive_index: Path
    additional_archive_indices: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "additional_archive_indices", tuple(self.additional_archive_indices))

    @property
    def archive_indices(self) -> tuple[Path, ...]:
        return (self.archive_index, *self.additional_archive_indices)


class _Sources:
    def __init__(self) -> None:
        self.hashes: dict[str, str] = {}

    def bytes(self, path: Path) -> bytes:
        path = path.resolve()
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        key = str(path)
        if key in self.hashes and self.hashes[key] != checksum:
            raise ValueError(f"source changed during analysis: {path}")
        self.hashes[key] = checksum
        return data

    def json(self, path: Path) -> Any:
        return json.loads(self.bytes(path))

    def verify_unchanged(self) -> None:
        for path, expected in self.hashes.items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError(f"source changed during analysis: {path}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sealed(value: Any, kind: str) -> None:
    _require(isinstance(value, dict) and value.get("kind") == kind, f"expected {kind}")
    _require(value.get("artifact_sha256") == content_hash(
        {key: item for key, item in value.items() if key != "artifact_sha256"}), f"{kind} self-hash mismatch")


def _filename(value: Any) -> str:
    _require(isinstance(value, str) and value not in {"", ".", ".."}
             and not any(part in value for part in ("/", "\\", ":", "\x00")), "expected a plain archive filename")
    return value


def _moment(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None, "timestamps require explicit timezones")
    return result


def _wrapper(plan: dict[str, Any], sources: _Sources):
    filename = "prospective_scaled.py" if plan.get("engineering_revision") else "prospective_related.py"
    path = REPOSITORY / "scripts" / filename
    sources.bytes(path)
    spec = importlib.util.spec_from_file_location(f"findings_{filename.removesuffix('.py')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exact(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_exact(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_exact(a, b) for a, b in zip(left, right))
    return left == right


def _reproducer(sources: _Sources):
    path = REPOSITORY / "scripts/reproduce_related.py"
    sources.bytes(path)
    spec = importlib.util.spec_from_file_location("findings_score_reproducer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _factors(job: dict[str, Any], revision: str, plan_hash: str) -> dict[str, Any]:
    config = job["config"]
    return {"collector_revision": revision, "plan_sha256": plan_hash, "test_role": job["test_role"],
            **{key: config.get(key) for key in (
                "task_family", "scenario_split", "task_format", "agents", "channel", "knowledge", "topology", "group_size",
                "track", "approval", "enforcement", "persistence", "communication_permission", "seed_delivery",
                "resume_on_messages", "inference_concurrency", "model", "reasoning_effort", "budget")}}


def _archive_entry(index_path: Path, item: dict[str, Any], sources: _Sources) -> tuple[Path, dict[str, str]]:
    directory = index_path.parent / "episodes" / _filename(item["run_id"])
    _require(directory.is_dir() and not directory.is_symlink(), f"missing archive episode: {directory}")
    raw_hashes = {}
    files = item.get("files")
    _require(isinstance(files, dict), "archive episode files are required")
    stored_names = [_filename(value.get("stored_as")) for value in files.values()]
    _require(len(set(stored_names)) == len(stored_names), "archive files cannot reuse stored names")
    _require({path.name for path in directory.iterdir()} == set(stored_names), "episode contains unindexed source files")
    for name, metadata in files.items():
        _filename(name)
        stored = directory / _filename(metadata.get("stored_as"))
        _require(not stored.is_symlink(), "archive files cannot be symlinks")
        packed = sources.bytes(stored)
        expected_bytes = metadata.get("uncompressed_bytes")
        _require(type(expected_bytes) is int and expected_bytes >= 0, "archive raw byte count is required")
        if stored.suffix == ".gz":
            with gzip.open(stored, "rb") as handle:
                data = handle.read(expected_bytes + 1)
        else:
            data = packed
        checksum = hashlib.sha256(data).hexdigest()
        _require(len(data) == expected_bytes and checksum == metadata.get("sha256"),
                 f"archived raw source hash or size mismatch: {stored}")
        for key in ("stored_sha256", "compressed_sha256"):
            if key in metadata:
                _require(hashlib.sha256(packed).hexdigest() == metadata[key], f"stored archive hash mismatch: {stored}")
        raw_hashes[name] = checksum
    return directory, raw_hashes


def _events(directory: Path) -> list[dict[str, Any]]:
    path = directory / "events.jsonl"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    elif (directory / "events.jsonl.gz").is_file():
        with gzip.open(directory / "events.jsonl.gz", "rt", encoding="utf-8") as handle:
            text = handle.read()
    else:
        return []
    return [json.loads(line) for line in text.splitlines()]


def _transport(events: list[dict[str, Any]], summary: dict[str, Any], job: dict[str, Any],
               deadline_reached: bool) -> dict[str, Any]:
    turns: dict[str, list[str | None]] = defaultdict(list)
    for event in events:
        if event["kind"] == "turn_finished":
            turns[event["agent_id"]].append(event.get("data", {}).get("result", {}).get("termination_reason"))
    agents = summary.get("agents", {})
    wall_ids = {event.get("agent_id") for event in events if event["kind"] == "agent_timeout"}
    wall_ids.update(agent for agent, reasons in turns.items() if any(
        reason in {"wall_budget_exhausted", "timeout"} for reason in reasons))
    return {
        "first_turn_termination": dict(Counter(reasons[0] or "unreported" for reasons in turns.values())),
        "last_observed_turn_termination": dict(Counter(reasons[-1] or "unreported" for reasons in turns.values())),
        "agents_resumed_after_first_turn": sum(len(reasons) > 1 for reasons in turns.values()),
        "first_turn_waiting_agent_ids": sorted(agent for agent, reasons in turns.items() if reasons[0] == "agent_waiting"),
        "completed_turns_by_agent": {agent: len(reasons) for agent, reasons in sorted(turns.items())},
        "summary_turns_distribution": dict(Counter(str(state.get("turns", "unreported")) for state in agents.values())),
        "summary_status_distribution": dict(Counter(state.get("status") or "unreported" for state in agents.values())),
        "token_capped_agent_ids": sorted(agent for agent, reasons in turns.items() if "token_budget_exhausted" in reasons),
        "tool_capped_agent_ids": sorted(agent for agent, reasons in turns.items() if "tool_budget_exhausted" in reasons),
        "agent_wall_capped_ids": sorted(agent for agent in wall_ids if agent is not None),
        "turn_limit_reached_ids": sorted(agent for agent, state in agents.items()
                                         if isinstance(state.get("turns"), int)
                                         and state["turns"] >= job["config"]["budget"]["max_turns"]),
        "population_deadline_reached": deadline_reached,
        "population_deadline_seconds": job["collection_deadline_seconds"],
        "token_usage_complete": bool(agents) and all(state.get("token_usage_complete") for state in agents.values()),
        "token_usage_incomplete_agent_ids": sorted(agent for agent, state in agents.items()
                                                   if not state.get("token_usage_complete")),
        "summary_present": bool(summary),
        "concurrency_definition": summary.get("concurrency_definition", "not recorded"),
    }


def _expected_outcomes(metrics: dict[str, Any]) -> dict[str, Any]:
    values = {"any_violation": metrics["unauthorized_action_participants"] > 0,
              "unauthorized_participants": metrics["unauthorized_action_participants"],
              "completed_violations": metrics["unauthorized_completion_participants"],
              "authorized_action_participants": metrics["authorized_action_participants"]}
    if not metrics["unidentified_channel_use_agents"]:
        values["channel_users"] = metrics["successful_channel_users"]
    return values


def _collection(collection: Collection, sources: _Sources) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    plan_dir, score_dir = collection.plan_directory, collection.score_directory
    plan = sources.json(plan_dir / "plan.json")
    wrapper = _wrapper(plan, sources)
    reproducer = _reproducer(sources)
    _require(_exact(wrapper.validate_plan(plan_dir), plan), "plan changed during validation")
    for name in ("cooperation.py", "channel_use.py", "related_tasks.py"):
        path = REPOSITORY / "src/swarm_auth_bench" / name
        current = hashlib.sha256(sources.bytes(path)).hexdigest()
        _require(plan["collector_source_sha256"][f"src/swarm_auth_bench/{name}"] == current,
                 f"historical measurement implementation changed: {name}")
    for source in plan["freeze_sources"]:
        sources.json(plan_dir / _filename(source["file"]))
    index = sources.json(score_dir / "score-index.json")
    _sealed(index, "prospective_related_collection_score_index")
    _require(index.get("plan_sha256") == plan["artifact_sha256"], "score index belongs to a different plan")
    _require(index.get("human_validation") == plan.get("human_validation") == "pending",
             "plan/score human-validation statuses differ")
    jobs = {job["trial_id"]: job for job in plan["populations"]}
    included = index.get("included_trial_ids", [])
    exclusions = index.get("exclusions", [])
    excluded = {row["trial_id"]: row for row in exclusions}
    _require(len(included) == len(set(included)) and len(excluded) == len(exclusions), "duplicate score index IDs")
    _require(not set(included) & set(excluded) and set(included) | set(excluded) == set(jobs),
             "score index must partition its own declared plan")
    _require(included == [trial_id for trial_id in jobs if trial_id in set(included)]
             and list(excluded) == [trial_id for trial_id in jobs if trial_id in excluded], "score index ID order differs from its plan")
    _require(_exact([index.get("populations_declared"), index.get("populations_scored"), index.get("populations_excluded")],
                    [len(jobs), len(included), len(excluded)]), "score index count mismatch")
    _require(_exact(sources.json(score_dir / "exclusions.json"), exclusions), "saved exclusions differ from score index")
    archived: dict[str, tuple[Path, dict[str, Any]]] = {}
    archive_sources = []
    archive_paths = collection.archive_indices
    _require(len({path.resolve() for path in archive_paths}) == len(archive_paths), "duplicate archive index path")
    for archive_path in archive_paths:
        archive = sources.json(archive_path)
        _require(archive.get("kind") == "validation_evidence", "unsupported evidence archive index")
        entries = {row["run_id"]: row for row in archive.get("episodes", [])}
        _require(len(entries) == len(archive.get("episodes", [])) and set(entries) <= set(jobs),
                 "archive has duplicate IDs or episodes from another plan")
        _require(not set(entries) & set(archived), "duplicate run IDs across immutable archives")
        archived.update({run_id: (archive_path, item) for run_id, item in entries.items()})
        archive_sources.append({"archive_index": str(archive_path.resolve()),
                                "sha256": sources.hashes[str(archive_path.resolve())], "run_ids": list(entries)})
    _require(set(included) <= set(archived), "scored populations need archived evidence")
    revision = plan.get("engineering_revision", "prospective-v1")
    predictions = {}
    for job in jobs.values():
        for reference in job["forecasts"]:
            forecast = sources.json(plan_dir / _filename(reference["file"]))
            _require(set(forecast["predictions"]) == set(BASELINES), "all declared prediction baselines are required")
            expected_spec = wrapper.spec_for_config(EpisodeConfig.from_dict(job["config"]), job["trial_id"])
            _require(_exact(forecast["spec"], expected_spec), "forecast specification type/value differs from declared config")
            _require(_exact(forecast.get("collection_deadline_seconds"), job["collection_deadline_seconds"]),
                     "forecast collection deadline type/value mismatch")
            predictions[(job["trial_id"], reference["reviewer"])] = forecast
    observed = {}
    for reviewer in (1, 2):
        rows = sources.json(score_dir / f"heldout-observations-reviewer-{reviewer}.json")
        by_id = {row["trial_id"]: row for row in rows}
        _require(len(by_id) == len(rows) and set(by_id) == set(included), "heldout row IDs differ from their score index")
        observed[reviewer] = by_id

    episodes = []
    for trial_id, job in jobs.items():
        exclusion = excluded.get(trial_id)
        claim_path = plan_dir / "collection-claims" / f"{trial_id}.json"
        claim = sources.json(claim_path) if claim_path.is_file() else None
        if claim is not None:
            _require(claim.get("trial_id") == trial_id and claim.get("plan_sha256") == plan["artifact_sha256"],
                     "collection claim belongs to another plan")
        row = {"trial_id": trial_id, "factors": _factors(job, revision, plan["artifact_sha256"]),
               "config_sha256": job["config_sha256"], "declared_config": job["config"],
               "claim_evidence": "present" if claim else "not supplied", "valid": trial_id in included,
               "started": False, "complete": False, "exclusion": exclusion, "metrics": None,
               "partial_observed_metrics_not_aggregated": None, "resources": None, "transport": None}
        if trial_id not in archived:
            _require(exclusion is not None and exclusion.get("category") == "not_run",
                     "excluded collected populations need archived source evidence")
            row["status"] = "claimed_unstarted" if claim else "unrun"
            episodes.append(row)
            continue
        archive_path, item = archived[trial_id]
        row["archive_index"] = str(archive_path.resolve())
        _require(type(item.get("valid")) is bool and item["valid"] == row["valid"], "archive/index validity mismatch")
        _require(type(item.get("complete")) is bool, "archive completion must be a recorded boolean")
        directory, raw_hashes = _archive_entry(archive_path, item, sources)
        _require(exclusion is None or exclusion.get("category") != "not_run", "archived episode cannot be marked not run")
        for filename, checksum in (exclusion or {}).get("source_hashes", {}).items():
            _require(raw_hashes.get(filename) == checksum, "exclusion source digest differs from archive")
        manifest = sources.json(directory / "manifest.json") if "manifest.json" in raw_hashes else None
        events = _events(directory)
        _require(all(event.get("run_id") == trial_id and event.get("sequence") == n for n, event in enumerate(events)),
                 "invalid archived event identities/sequences")
        starts = [event for event in events if event["kind"] == "episode_started"]
        _require(len(starts) <= 1 and (len(starts) == 1 or not row["valid"]), "episode start is missing or duplicated")
        row["started"] = bool(starts)
        row["complete"] = bool(item.get("complete"))
        row["raw_source_sha256"] = raw_hashes
        if manifest is not None:
            _require("prospective_prediction.json" in raw_hashes, "prospective binding is absent from the archive index")
            _require(manifest.get("run_id") == trial_id and _exact(manifest.get("config"), job["config"])
                     and manifest.get("config_hash") == job["config_sha256"], "archived config differs from its plan")
            for path, checksum in plan["collector_source_sha256"].items():
                _require(manifest.get("implementation_hashes", {}).get(Path(path).name) == checksum,
                         "archived collector implementation differs from its plan")
            binding = sources.json(directory / "prospective_prediction.json")
            _require(_exact(binding, {"trial_id": trial_id, "config_sha256": job["config_sha256"],
                                     "forecasts": job["forecasts"], "plan_sha256": plan["artifact_sha256"],
                                     "collection_deadline_seconds": job["collection_deadline_seconds"]}),
                     "archived prospective binding differs from its own plan")
        _require(manifest is not None or not row["valid"], "valid episodes need a manifest")
        if starts:
            started = _moment(starts[0]["wall_time"])
            _require(started > _moment(plan["created_at_utc"]) and all(started > _moment(
                predictions[(trial_id, reviewer)]["created_at_utc"]) for reviewer in (1, 2)), "collection preceded its plan/forecast")
            row["started_at_utc"] = started.isoformat()
        summary = sources.json(directory / "summary.json") if "summary.json" in raw_hashes else {}
        deadline = "collection_deadline_failure.json" in raw_hashes
        row["transport"] = _transport(events, summary, job, deadline)
        analysis = analyze_episode(directory) if manifest is not None else None
        if analysis is not None:
            _require(not row["valid"] or analysis["included"], "valid scored episode failed mechanical reconstruction")
            row["resources"] = analysis["resources"]
            row["partial_observed_metrics_not_aggregated"] = analysis["metrics"] if not row["valid"] else None
        if row["valid"]:
            _require(row["complete"] and analysis is not None and not deadline, "valid scored episode is incomplete/capped")
            row["metrics"] = analysis["metrics"]
            raw_sources = {name.removesuffix(".gz") if name == "events.jsonl.gz" else name: raw_hashes[
                name.removesuffix(".gz") if name == "events.jsonl.gz" else name] for name in analysis["source_hashes"]}
            for reviewer in (1, 2):
                heldout = observed[reviewer][trial_id]
                _require(heldout.get("source_sha256") == content_hash(raw_sources), "heldout source digest differs from archived raw bytes")
                _require(heldout.get("started_at_utc") == row["started_at_utc"] and heldout.get("test_role") == job["test_role"],
                         "heldout timing/task role mismatch")
                _require(_exact(heldout.get("additional_measured_outcomes"), row["metrics"])
                         and _exact(heldout.get("outcomes"), _expected_outcomes(row["metrics"])),
                         "saved mechanical outcomes differ from raw evidence")
                _require(_exact(heldout.get("collection_censoring"), {
                    "population_deadline_seconds": job["collection_deadline_seconds"], "population_deadline_reached": deadline,
                    "agent_wall_cap_reported_ids": row["transport"]["agent_wall_capped_ids"]}),
                         "saved censoring differs from raw evidence")
            _require(_exact(observed[1][trial_id]["outcomes"], observed[2][trial_id]["outcomes"]),
                     "reviewer mechanical outcomes disagree")
        row["status"] = "valid" if row["valid"] else "invalid_started" if row["started"] else "invalid_unstarted"
        episodes.append(row)

    score_rows = []
    score_comparisons = []
    for reviewer in (1, 2):
        saved = sources.json(score_dir / f"scores-reviewer-{reviewer}.json")
        if included:
            reproducer._rebuild_saved_score_artifact(
                score_dir / f"scores-reviewer-{reviewer}.json", saved,
                {trial_id: predictions[(trial_id, reviewer)] for trial_id in included}, observed[reviewer], included,
                comparison_out=score_comparisons)
            for scored in saved["rows"]:
                forecast = predictions[(scored["trial_id"], reviewer)]
                for baseline, values in scored["scores"].items():
                    score_rows.append({"trial_id": scored["trial_id"], "reviewer": reviewer, "baseline": baseline,
                                       "factors": _factors(jobs[scored["trial_id"]], revision, plan["artifact_sha256"]),
                                       "forecast_sha256": forecast["artifact_sha256"], "source_sha256": scored["source_sha256"],
                                       "probability_any_violation": forecast["predictions"][baseline]["probability_any_violation"],
                                       "scores": values})
        else:
            _sealed(saved, "heldout_scores_not_computed")
            _require(_exact(saved.get("independent_populations"), 0) and saved.get("aggregate_scores") == {},
                     "empty scores are inconsistent")
    condition_groups = {}
    for trial_id in included:
        job = jobs[trial_id]
        factors = {"test_role": job["test_role"], **{name: job["config"][name] for name in ("agents", "topology", "track")}}
        key = json.dumps(factors, sort_keys=True)
        condition_groups.setdefault(key, (factors, []))[1].append(trial_id)
    expected_conditions = []
    for reviewer in (1, 2):
        for condition_index, (_, (factors, ids)) in enumerate(sorted(condition_groups.items())):
            filename = f"scores-reviewer-{reviewer}-condition-{condition_index:03d}.json"
            expected_conditions.append({"reviewer": reviewer, "factors": factors,
                                        "independent_populations": len(ids), "file": filename})
            saved = sources.json(score_dir / filename)
            reproducer._rebuild_saved_score_artifact(
                score_dir / filename, saved, {trial_id: predictions[(trial_id, reviewer)] for trial_id in ids},
                observed[reviewer], ids, comparison_out=score_comparisons)
    _require(_exact(index.get("condition_scores"), expected_conditions), "condition score index differs from its own plan")
    return episodes, score_rows, {"plan_sha256": plan["artifact_sha256"], "collector_revision": revision,
                                 "collector_source_sha256": plan["collector_source_sha256"],
                                 "score_index_sha256": index["artifact_sha256"], "human_validation": index.get("human_validation"),
                                 "declared_limitations": plan.get("limits", []),
                                 "score_rounding_comparisons": score_comparisons,
                                 "plan_directory": str(plan_dir.resolve()), "score_directory": str(score_dir.resolve()),
                                 "archive_index": str(collection.archive_index.resolve()),
                                 "archive_indices": [str(path.resolve()) for path in archive_paths],
                                 "archive_sources": archive_sources}


def _groups(episodes: list[dict[str, Any]], scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in episodes:
        grouped[json.dumps(row["factors"], sort_keys=True)].append(row)
    results = []
    def sort_key(item):
        factors = item[1][0]["factors"]
        return factors["collector_revision"], factors["agents"], factors["plan_sha256"], item[0]

    for key, rows in sorted(grouped.items(), key=sort_key):
        valid = [row for row in rows if row["valid"]]
        totals = Counter()
        first_turns, terminal_turns = Counter(), Counter()
        for row in valid:
            totals.update({name: value for name, value in row["metrics"].items() if type(value) is int})
            first_turns.update(row["transport"]["first_turn_termination"])
            terminal_turns.update(row["transport"]["last_observed_turn_termination"])
        score_groups = []
        for reviewer in (1, 2):
            for baseline in BASELINES:
                matched = [row for row in scores if row["factors"] == rows[0]["factors"]
                           and row["reviewer"] == reviewer and row["baseline"] == baseline]
                _require(len(matched) == len(valid), "score rows must have one value per valid population/reviewer/baseline")
                score_groups.append({"reviewer": reviewer, "baseline": baseline, "independent_populations": len(matched),
                                     "mean_scores": {name: fmean(row["scores"][name] for row in matched)
                                                     for name in ("brier", "log_loss", "participant_residual", "participant_interval95_covered")}
                                     if matched else {}})
        runtimes = [row["resources"]["elapsed_seconds"] for row in valid
                    if isinstance(row["resources"].get("elapsed_seconds"), (int, float))]
        results.append({"factors": rows[0]["factors"], "declared_populations": len(rows),
                        "started_populations": sum(row["started"] for row in rows), "valid_populations": len(valid),
                        "statuses": dict(Counter(row["status"] for row in rows)),
                        "valid_trial_ids": [row["trial_id"] for row in valid], "valid_agent_counts_descriptive": dict(totals),
                        "valid_populations_with_unauthorized_attempt": sum(row["metrics"]["unauthorized_action_participants"] > 0 for row in valid),
                        "valid_first_turn_termination": dict(first_turns), "valid_last_observed_turn_termination": dict(terminal_turns),
                        "valid_token_capped_agents": sum(len(row["transport"]["token_capped_agent_ids"]) for row in valid),
                        "valid_wall_capped_agents": sum(len(row["transport"]["agent_wall_capped_ids"]) for row in valid),
                        "valid_runtime_seconds": {"measured_populations": len(runtimes), "mean": fmean(runtimes) if runtimes else None},
                        "valid_peak_active_turns": max((row["resources"].get("peak_active_agent_turns") or 0 for row in valid), default=None),
                        "prediction_scores": score_groups})
    return results


def build_findings(collections: list[Collection], output: Path | None = None) -> dict[str, Any]:
    """Validate one or more existing collections and emit new artifacts only."""
    _require(bool(collections), "at least one collection is required")
    if output is not None and output.exists():
        raise FileExistsError(output)
    if output is not None:
        target = output.resolve()
        for collection in collections:
            for root in (collection.plan_directory, collection.score_directory,
                         *(index.parent for index in collection.archive_indices)):
                root = root.resolve()
                _require(target != root and root not in target.parents, "output must stay outside scientific input directories")
    sources = _Sources()
    for name in ("findings.py", "config.py", "events.py", "predict.py"):
        sources.bytes(Path(__file__).with_name(name))
    sources.bytes(REPOSITORY / "scripts/build_related_findings.py")
    episodes, scores, collection_rows = [], [], []
    for collection in collections:
        rows, scored, metadata = _collection(collection, sources)
        _require(metadata["plan_sha256"] not in {row["plan_sha256"] for row in collection_rows}, "duplicate collection plan")
        _require(not {row["trial_id"] for row in rows} & {row["trial_id"] for row in episodes}, "run IDs reused across collections")
        episodes.extend(rows)
        scores.extend(scored)
        collection_rows.append(metadata)
    sources.verify_unchanged()
    report = {
        "kind": "related_prospective_findings", "schema_version": SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "human_validation": "pending",
        "collections": collection_rows, "mechanical_episode_rows": episodes, "reviewer_baseline_score_rows": scores,
        "condition_groups": _groups(episodes, scores), "counts": {
            "declared_populations": len(episodes), "started_populations": sum(row["started"] for row in episodes),
            "valid_populations": sum(row["valid"] for row in episodes), "statuses": dict(Counter(row["status"] for row in episodes)),
            "reviewer_interpretations_per_population": 2,
        },
        "source_sha256": sources.hashes,
        "limitations": ["Each population appears once in mechanical counts; two reviewer forecasts do not double its denominator.",
                        "Collector revisions, task formats and conditions remain separate; aggregate counts are descriptive.",
                        "Invalid/partial observations are retained but never included as valid null outcomes.",
                        "Unrun episodes remain missing outcomes; claim status is unknown when claim evidence was not supplied.",
                        "Agents and messages are clustered descriptive counts; repetition counts refer to populations.",
                        "Exact delivered JSON is a conservative contract-receipt proxy, not all useful information.",
                        "Semantic propagation and human validation remain pending; an action null cannot validate transmission or discrimination.",
                        "Local response windows, stopping, receipt factorization and asynchronous collective turns remain transport assumptions.",
                        "Peak runtime turns include tool waits; population size and simultaneous provider inference are different quantities."]}
    report["artifact_sha256"] = content_hash(report)
    if output is not None:
        output.mkdir(parents=True, exist_ok=False)
        (output / "findings.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (output / "findings.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _task_format_label(factors: dict[str, Any]) -> str:
    label = factors.get("task_format")
    if label is None:
        label = factors["scenario_split"]
    return {"development": "flat API (development)", "heldout": "nested API (heldout)"}.get(label, str(label))


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = ["# Prospective population findings", "",
             f"{counts['valid_populations']} valid populations from {counts['started_populations']} started and "
             f"{counts['declared_populations']} declared. Invalid and unrun populations retain separate rows. "
             "Mechanical outcomes count each population once; the two reviewer forecasts describe the same populations.", "",
             "Agent outcomes below are clustered descriptive counts. Exact receipt confirms complete JSON in delivered fields. "
             "The source hashes, exclusions, stopping and budget records are in findings.json. Human validation remains pending.", "",
             "| Group | Collector | Task format | Agents | Topology | Proposal | Valid / started / planned | Repair / agents | "
             "Full task / agents | Exact receipt / agents | Action-positive populations | Mean seconds | Peak active turns |",
             "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |"]
    for group_index, group in enumerate(report["condition_groups"], 1):
        factor, metric = group["factors"], group["valid_agent_counts_descriptive"]
        agents = metric.get("agents", 0)
        runtime = group["valid_runtime_seconds"]["mean"]
        ratios = {key: f"{metric.get(key, 0)} / {agents}" if agents else "unmeasured"
                  for key in ("repair_successes", "full_task_successes", "dependency_contract_receivers")}
        runtime_text = f"{runtime:.1f}" if runtime is not None else "unmeasured"
        action_text = f"{group['valid_populations_with_unauthorized_attempt']} / {group['valid_populations']}" \
            if group["valid_populations"] else "unmeasured"
        lines.append(f"| G{group_index:02d} | {factor['collector_revision']} | {_task_format_label(factor)} | {factor['agents']} | "
                     f"{factor['topology']} | {factor['track']} | {group['valid_populations']} / {group['started_populations']} / "
                     f"{group['declared_populations']} | {ratios['repair_successes']} | {ratios['full_task_successes']} | "
                     f"{ratios['dependency_contract_receivers']} | {action_text} | {runtime_text}")
        lines[-1] += f" | {group['valid_peak_active_turns'] if group['valid_peak_active_turns'] is not None else 'unmeasured'} |"
    lines += ["", "Each group retains its full channel, knowledge, approval, enforcement, persistence, model and budget "
              "settings in the JSON. Development-format fresh populations and nested-format transfer populations stay separate.", "",
              "| Group | Reviewer forecast | Model | Populations | Brier loss | Log loss | Participant residual | 95% interval coverage |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for group_index, group in enumerate(report["condition_groups"], 1):
        if not group["valid_populations"]:
            continue
        for baseline in BASELINES:
            pair = [row for row in group["prediction_scores"] if row["baseline"] == baseline]
            displayed = [("Both, identical scores", pair[0])] if pair[0]["mean_scores"] == pair[1]["mean_scores"] else [
                (f"Reviewer {row['reviewer']}", row) for row in pair]
            for label, row in displayed:
                value = row["mean_scores"]
                lines.append(f"| G{group_index:02d} | {label} | {baseline.replace('_', ' ')} | "
                             f"{row['independent_populations']} | {value['brier']:.6f} | {value['log_loss']:.6f} | "
                             f"{value['participant_residual']:.6f} | {value['participant_interval95_covered']:.3f} |")
    action_total = sum(group["valid_populations_with_unauthorized_attempt"] for group in report["condition_groups"])
    if not counts["valid_populations"]:
        action_context = "No valid publication outcomes were available."
    elif action_total == 0:
        action_context = "The action null describes these bounded populations."
    else:
        action_context = "Observed actions describe these bounded populations."
    valid_rows = [row for row in report["mechanical_episode_rows"] if row["valid"]]
    waits = sum(row["transport"]["first_turn_termination"].get("agent_waiting", 0) for row in valid_rows)
    resumed = sum(row["transport"]["agents_resumed_after_first_turn"] for row in valid_rows)
    token_caps = sum(len(row["transport"]["token_capped_agent_ids"]) for row in valid_rows)
    lines += ["", f"Among valid assignments, {waits} first turns waited, {resumed} agents resumed and {token_caps} "
              "agents reached the token cap. These counts describe transport and budget limits; they are not extra repetitions."]
    lines += ["", action_context + " They do not establish general risk, model ranking, "
              "transmission validity or action-time accuracy. Predictive scores stay separate by reviewer, baseline, collector "
              "and condition in the JSON. No predictions were regenerated and no scientific inputs were changed.", ""]
    return "\n".join(lines)
