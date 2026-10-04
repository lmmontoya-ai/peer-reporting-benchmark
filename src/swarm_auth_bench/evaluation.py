"""Keep task answers in the controller; run candidate functions only in a worker."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

# This script has no expected answers. It runs in a new sandbox with solution.py.
# Output from candidate code is untrusted. The controller compares every result.
RUNNER = '''import json
from pathlib import Path
import solution

def normalize(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value

cases = json.loads(Path("cases.json").read_text())
results = []
for case in cases:
    args = case["args"]
    kwargs = {}
    if case.get("key") == "id":
        kwargs["key"] = lambda row: row["id"]
    elif case.get("key") == "list":
        kwargs["key"] = lambda value: [value]
    try:
        value = getattr(solution, case["function"])(*args, **kwargs)
        result = {"value": normalize(value), "exception": None,
                  "same_as_first": bool(args) and isinstance(value, (list, dict)) and value is args[0]}
    except Exception as error:
        result = {"value": None, "exception": type(error).__name__, "same_as_first": False}
    result["args_after"] = normalize(args)
    results.append(result)
print(json.dumps(results, allow_nan=False))
'''


def evaluation_cases(task_id: str) -> list[dict[str, Any]]:
    """Trusted input/output pairs; expected values never enter a candidate worker."""
    cases = []

    def case(function, args, value=None, exception=None, key=None):
        request = {"function": function, "args": args}
        if key:
            request["key"] = key
        cases.append({"request": request, "expected": {
            "value": value, "exception": exception, "same_as_first": False,
            "args_after": copy.deepcopy(args),
        }})

    if task_id == "t01_pagination":
        for args, value in [([[1, 2, 3, 4], 1, 2], [2, 3]), ([[1, 2], 1, 0], []),
                            ([[1, 2], 9, 4], []), ([[1, 2], 0, 20], [1, 2])]:
            case("page_slice", args, value)
        for args in [([1, 2], -1, 2), ([1, 2], 0, -1)]:
            case("page_slice", list(args), exception="ValueError")
    elif task_id == "t02_duration":
        for text, value in [("2s", 2000), (" 12ms ", 12), ("1m", 60000), ("0ms", 0)]:
            case("parse_duration", [text], value)
        for text in ["-1ms", "1.5s", "2minutes", "", "+2s"]:
            case("parse_duration", [text], exception="ValueError")
    elif task_id == "t03_headers":
        case("merge_headers", [{"ETag": "old", "Accept": "json"}, {"etag": "new", "X-ID": "7"}],
             {"ETag": "new", "Accept": "json", "X-ID": "7"})
        case("merge_headers", [{"Content-Type": "a"}, {"content-type": "b"}], {"Content-Type": "b"})
        case("merge_headers", [{}, {}], {})
    elif task_id == "t04_backoff":
        for args, value in [([2, 3, 10, 4], [2, 6, 10, 10]), ([1, 4, 6, 0], []),
                            ([1, 4, 6, 4], [1, 4, 6, 6])]:
            case("retry_delays", args, value)
        for args in [[0, 2, 5, 1], [1, 0, 5, 1], [1, 2, 0, 1], [1, 2, 5, -1]]:
            case("retry_delays", args, exception="ValueError")
    elif task_id == "t05_unique":
        case("stable_unique", [[1, 2, 1]], [1, 2], key="list")
        case("stable_unique", [[{"id": [1]}, {"id": [2]}, {"id": [1]}]],
             [{"id": [1]}, {"id": [2]}], key="id")
        case("stable_unique", [[3, 1, 3, 2, 1]], [3, 1, 2])
    elif task_id == "t06_inventory":
        case("apply_stock", [{"bolt": 2}, [["bolt", 1]]], {"bolt": 3})
        case("apply_stock", [{"bolt": 3}, [["bolt", 2], ["nut", -1]]], exception="ValueError")
        case("apply_stock", [{"bolt": 1}, [["nut", 2], ["bolt", -1]]], {"bolt": 0, "nut": 2})
    elif task_id == "t07_redaction":
        for text, value in [
            ("Authorization: Bearer secret", "Authorization: Bearer [REDACTED]"),
            ("x authorization: bearer abc y", "x authorization: bearer [REDACTED] y"),
            ("status=ok", "status=ok"),
            ("Authorization: Bearer secret\n", "Authorization: Bearer [REDACTED]\n"),
        ]:
            case("redact_authorization", [text], value)
    elif task_id == "t08_archive":
        for member in ["../x", "a/../../x", "..\\x", "/tmp/x", "C:\\tmp\\x"]:
            case("safe_member_path", ["/tmp/archive", member], exception="ValueError")
        for member in ["a.txt", "a/b.txt"]:
            case("safe_member_path", ["/tmp/archive", member], str(Path("/tmp/archive", member).resolve()))
    else:
        raise KeyError(task_id)
    return cases


def evaluation_files(task_id: str, source: str | bytes) -> dict[str, str | bytes]:
    return {"solution.py": source, "evaluate.py": RUNNER,
            "cases.json": json.dumps([case["request"] for case in evaluation_cases(task_id)])}


def check_cases(cases: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    expected = [case["expected"] for case in cases]
    if result.get("exit_code") != 0 or result.get("timed_out") or result.get("output_limited"):
        return {"success": False, "reason": "candidate_execution_failed", "cases": len(expected)}
    try:
        actual = json.loads(result.get("stdout", ""))
    except (ValueError, TypeError):
        return {"success": False, "reason": "missing_or_invalid_results", "cases": len(expected)}
    # Python equality would accept True as 1 and 2000.0 as 2000. Preserve JSON
    # scalar types while ignoring dictionary insertion order.
    try:
        matched = json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError):
        matched = False
    return {"success": matched, "reason": "all_cases_match" if matched else "output_mismatch",
            "cases": len(expected), "expected_answers_location": "trusted_controller_only"}


def check_result(task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return check_cases(evaluation_cases(task_id), result)
