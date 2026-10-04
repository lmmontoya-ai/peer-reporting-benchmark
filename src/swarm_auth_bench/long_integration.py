"""Cross-package candidate execution through isolated workers only.

The controller treats source snapshots, sandbox stdout and sender wire values as
untrusted data. This module never imports, compiles or executes candidate source.
Expected decoded values stay on the controller, outside every sandbox.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import time
import uuid
from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from .evaluation import RUNNER
from .events import content_hash
from .long_tasks import LONG_CANDIDATE_FILES, long_integration_cases
from .tasks import TaskSpec

_MAX_FILE_BYTES = 1_000_000
_MAX_WIRE_BYTES = 65_536
_STATUSES = ("passed", "failed", "blocked_missing_candidate", "invalid_candidate", "invalid_output",
             "execution_failed", "infrastructure_failed")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON value")


def _parse_results(result: dict[str, Any], requests: list[dict[str, Any]]) -> tuple[str, list[Any] | None]:
    if (type(result.get("exit_code")) is not int or result["exit_code"] != 0
            or result.get("timed_out") or result.get("output_limited")):
        return "execution_failed", None
    stdout = result.get("stdout")
    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > _MAX_FILE_BYTES:
        return "invalid_output", None
    try:
        rows = json.loads(stdout, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
        if not isinstance(rows, list) or len(rows) != len(requests):
            return "invalid_output", None
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"value", "exception", "same_as_first", "args_after"}:
                return "invalid_output", None
            if type(row["same_as_first"]) is not bool or not isinstance(row["args_after"], list):
                return "invalid_output", None
            if row["exception"] is not None and (
                not isinstance(row["exception"], str) or not row["exception"] or row["value"] is not None
            ):
                return "invalid_output", None
        _canonical(rows).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return "invalid_output", None
    return "valid_output", rows


def _successful_row(row: dict[str, Any], request: dict[str, Any]) -> bool:
    return (row["exception"] is None and row["same_as_first"] is False
            and _canonical(row["args_after"]) == _canonical(request["args"]))


def _snapshot_error(snapshot: Any) -> str | None:
    if not isinstance(snapshot, dict) or set(snapshot) != set(LONG_CANDIDATE_FILES):
        return "snapshot must contain exactly the candidate allowlist"
    try:
        for value in snapshot.values():
            if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_FILE_BYTES:
                return "snapshot files must be bounded UTF-8 strings"
    except UnicodeError:
        return "snapshot files must be valid UTF-8 strings"
    return None


async def run_long_integration(
    isolation: Any,
    tasks: list[TaskSpec],
    snapshots: dict[str, dict[str, str]],
    stage: int,
    run_id: str,
    *,
    concurrency: int = 4,
    timeout: float = 30,
    log_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Run sender groups, then receiver groups, in fresh sealed gVisor workers.

    ``isolation`` must be the already qualified gVisor service. It owns the outer
    containment checks. Offline tests may use a fake that returns trusted fixture
    data without executing source. This function never creates principal approval.
    On cancellation every attempted worker creation is reconciled through destroy
    before cancellation propagates. Cleanup failure is reported and cannot yield
    an integration success.
    """
    if type(concurrency) is not int or not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be an integer between 1 and 4")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if not isinstance(run_id, str) or not run_id or not isinstance(snapshots, dict):
        raise ValueError("run_id must be nonempty text and snapshots must be a mapping")
    started = time.monotonic()
    cases = long_integration_cases(tasks, stage)
    owners = {task.controller_data["owner_agent_id"] for task in tasks}
    if snapshots.keys() - owners:
        raise ValueError("snapshot owner is not in the task population")
    # Copy each string mapping before any await, preserving the exact evaluated artifact.
    valid_snapshots, snapshot_errors = {}, {}
    for owner, snapshot in snapshots.items():
        error = _snapshot_error(snapshot)
        if error:
            snapshot_errors[owner] = error
        else:
            valid_snapshots[owner] = dict(snapshot)
    hashes = {owner: content_hash(snapshot) for owner, snapshot in valid_snapshots.items()}
    namespace = content_hash(run_id)[:12] + "-" + uuid.uuid4().hex[:8]
    slots = asyncio.Semaphore(concurrency)
    active_workers: set[str] = set()
    cleanup_interrupted = False
    cleanup_errors: dict[str, str] = {}
    executions: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    async def emit(kind: str, **fields: Any) -> None:
        if log_callback is not None:
            value = log_callback({"kind": kind, "stage": stage, **fields})
            if inspect.isawaitable(value):
                await value

    async def destroy(worker_id: str) -> bool:
        try:
            await isolation.destroy(worker_id)
        except Exception as error:
            cleanup_errors[worker_id] = type(error).__name__
            return False
        active_workers.discard(worker_id)
        cleanup_errors.pop(worker_id, None)
        return True

    async def protected_cleanup(worker_id: str) -> None:
        nonlocal cleanup_interrupted
        # shield keeps a second cancellation from abandoning an in-flight destroy.
        operation = asyncio.create_task(destroy(worker_id))
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                cleanup_interrupted = True
        await operation

    async def group(phase: str, owner: str, indexed_requests: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        queued = time.monotonic()
        worker_id = f"lgi-{namespace}-{phase[0]}-{content_hash(owner)[:12]}"
        requests = [request for _, request in indexed_requests]
        record = {"phase": phase, "owner_agent_id": owner, "worker_id": worker_id,
                  "requests": len(requests), "artifact_sha256": hashes[owner]}
        execution_result: dict[str, Any] = {}
        rows = None
        async with slots:
            group_started = time.monotonic()
            record["queue_seconds"] = group_started - queued
            # Register before create: cancellation may happen after the container
            # exists but before create returns to this coroutine.
            active_workers.add(worker_id)
            try:
                request_text = _canonical(requests)
                if len(request_text.encode("utf-8")) > _MAX_FILE_BYTES:
                    record.update(status="invalid_output", reason="integration request bytes exceed limit")
                else:
                    await emit("integration_group_started", **record)
                    await isolation.create(worker_id, {**valid_snapshots[owner], "evaluate.py": RUNNER,
                                                       "cases.json": request_text}, readonlywork=True)
                    executed_at = time.monotonic()
                    raw_result = await isolation.execute(worker_id, ["python", "evaluate.py"], timeout=timeout)
                    record["execute_wall_seconds"] = time.monotonic() - executed_at
                    if is_dataclass(raw_result) and not isinstance(raw_result, type):
                        execution_result = asdict(raw_result)
                    elif isinstance(raw_result, dict):
                        execution_result = dict(raw_result)
                    else:
                        execution_result = {}
                    status, rows = _parse_results(execution_result, requests)
                    record["status"] = status
                    stdout, stderr = execution_result.get("stdout", ""), execution_result.get("stderr", "")
                    record["execution"] = {
                        "exit_code": execution_result.get("exit_code"), "timed_out": bool(execution_result.get("timed_out")),
                        "output_limited": bool(execution_result.get("output_limited")),
                        "duration_s": execution_result.get("duration_s"),
                        "stdout_sha256": content_hash(stdout) if isinstance(stdout, str) else None,
                        "stderr_tail": stderr[-2000:] if isinstance(stderr, str) else "invalid stderr type",
                    }
            except asyncio.CancelledError:
                record["status"] = "cancelled"
                raise
            except Exception as error:
                record.update(status="infrastructure_failed", error_type=type(error).__name__)
            finally:
                await protected_cleanup(worker_id)
                record["wall_seconds"] = time.monotonic() - group_started
                record["cleanup_success"] = worker_id not in active_workers
                if not record["cleanup_success"]:
                    record["status"] = "infrastructure_failed"
                executions.append(record)
            await emit("integration_group_finished", **record)
        return {"record": record, "rows": rows, "indexed_requests": indexed_requests}

    async def phase(groups: dict[str, list[tuple[int, dict[str, Any]]]], name: str) -> list[dict[str, Any]]:
        running = [asyncio.create_task(group(name, owner, requests)) for owner, requests in sorted(groups.items())]
        try:
            return await asyncio.gather(*running)
        finally:
            for task in running:
                if not task.done():
                    task.cancel()
            # Wait for each group's protected cleanup, including cancellation while
            # other groups were still queued on the concurrency semaphore.
            if running:
                pending = asyncio.ensure_future(asyncio.gather(*running, return_exceptions=True))
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
                await pending

    def fail_edge(index: int, status: str, reason: str, phase_name: str) -> None:
        edges[index].update(status=status, reason=reason, phase=phase_name)

    sender_groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, case in enumerate(cases):
        sender, receiver = case["sender_agent_id"], case["receiver_agent_id"]
        edges.append({"sender_agent_id": sender, "receiver_agent_id": receiver,
                      "role": case["request"]["args"][1], "revision": case["revision"],
                      "sender_artifact_sha256": hashes.get(sender), "receiver_artifact_sha256": hashes.get(receiver)})
        missing = [owner for owner in (sender, receiver) if owner not in snapshots]
        invalid = [owner for owner in (sender, receiver) if owner in snapshot_errors]
        if missing:
            fail_edge(index, "blocked_missing_candidate", "missing snapshot for " + ", ".join(missing), "snapshot")
        elif invalid:
            fail_edge(index, "invalid_candidate", "invalid snapshot for " + ", ".join(invalid), "snapshot")
        else:
            sender_groups.setdefault(sender, []).append((index, copy.deepcopy(case["request"])))

    try:
        sender_results = await phase(sender_groups, "encode")
        receiver_groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for result in sender_results:
            record, rows = result["record"], result["rows"]
            for position, (index, request) in enumerate(result["indexed_requests"]):
                if record["status"] != "valid_output":
                    fail_edge(index, record["status"], "sender group did not return valid results", "encode")
                    continue
                row = rows[position]
                if not _successful_row(row, request):
                    fail_edge(index, "failed", "sender raised an exception, mutated input, or returned an alias", "encode")
                    edges[index]["observed_exception"] = row["exception"]
                    continue
                wire = row["value"]
                if len(_canonical(wire).encode("utf-8")) > _MAX_WIRE_BYTES:
                    fail_edge(index, "invalid_output", "sender wire exceeds integration byte limit", "encode")
                    continue
                receiver = cases[index]["receiver_agent_id"]
                receiver_groups.setdefault(receiver, []).append((index, {"function": cases[index]["receiver_function"],
                                                                          "args": [copy.deepcopy(wire)]}))
                edges[index]["wire_sha256"] = content_hash(wire)
        receiver_results = await phase(receiver_groups, "decode")
        for result in receiver_results:
            record, rows = result["record"], result["rows"]
            for position, (index, request) in enumerate(result["indexed_requests"]):
                if record["status"] != "valid_output":
                    fail_edge(index, record["status"], "receiver group did not return valid results", "decode")
                    continue
                row = rows[position]
                matched = _successful_row(row, request) and _canonical(row["value"]) == _canonical(cases[index]["expected_value"])
                edges[index].update(status="passed" if matched else "failed", phase="decode",
                                    reason="canonical event matched" if matched else "receiver result did not match canonical event",
                                    observed_exception=row["exception"], observed_value_sha256=content_hash(row["value"]))
    finally:
        # Retry failed destroys. The isolation service keeps failed worker IDs
        # registered, so reconciliation must include attempted creates as well.
        for worker_id in sorted(active_workers):
            for _ in range(2):
                await protected_cleanup(worker_id)
                if worker_id not in active_workers:
                    break
        if active_workers:
            await emit("integration_cleanup_failed", worker_ids=sorted(active_workers), errors=dict(cleanup_errors))
    if cleanup_interrupted:
        raise asyncio.CancelledError
    counts = {status: 0 for status in _STATUSES}
    counts.update(Counter(edge["status"] for edge in edges))
    summary = {"success": bool(edges) and counts["passed"] == len(edges) and not active_workers,
               "stage": stage, "edge_count": len(edges), "counts": counts, "edges": edges,
               "executions": executions, "snapshot_errors": snapshot_errors,
               "cleanup_success": not active_workers, "cleanup_failed_workers": sorted(active_workers),
               "timing": {"wall_seconds": time.monotonic() - started, "execution_groups": len(executions),
                          "execute_seconds_sum": sum(record.get("execute_wall_seconds", 0) for record in executions)},
               "expected_answers_location": "trusted_controller_only"}
    await emit("integration_completed", counts=counts, success=summary["success"],
               cleanup_success=summary["cleanup_success"], timing=summary["timing"])
    return summary
