"""Shared analysis population and seeded Level P mechanical-negative sample."""

from __future__ import annotations

import hashlib


def analysis_score_rows(rows: list[dict]) -> list[dict]:
    """Scored export rows admitted to analysis, including eligible and ineligible trials."""
    return [row for row in rows if row.get("score") is not None and not row.get("excluded_from_analysis")]


def pressure_negative_sample(scored: list[dict], *, seed: int) -> set[str]:
    """Take ceil(n/10) eligible mechanical negatives per arm from the analysis population."""
    negatives = {}
    for record in scored:
        if record.get("level") != "P":
            continue
        endpoints = record["endpoints"]
        negative = record["eligibility"]["eligible"] and (
            record["variant"] == "violation" and endpoints["focal_hit"]["value"] is False
            or record["variant"] == "twin" and endpoints["false_alarm"]["value"] is False)
        if negative:
            negatives.setdefault(record["arm"], []).append(record["assignment_id"])
    sampled = set()
    for members in negatives.values():
        def key(identifier):
            return hashlib.sha256(f"{seed}||pressure-negative||{identifier}".encode("utf-8")).hexdigest(), identifier
        sampled.update(sorted(members, key=key)[: -(-len(members) // 10)])
    return sampled
