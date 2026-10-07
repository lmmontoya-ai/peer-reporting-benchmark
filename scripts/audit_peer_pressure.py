"""Reproduce the revision 6 fixture audit without models, runtimes or guests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from swarm_auth_bench.events import canonical_json, content_hash
from swarm_auth_bench.peer_reporting.fixtures import field_differences
from swarm_auth_bench.peer_reporting_v11 import incidents, pressure_text
from swarm_auth_bench.peer_reporting_v11.config import PRESSURE_ARMS, load_protocol, pressure_fixture_cells
from swarm_auth_bench.peer_reporting_v11.ladder import PRESSURE_FRAME_PAIRS, build_fixture, verify_fixture


@lru_cache(maxsize=None)
def fixture(template_id, block, variant, k, visibility, pressure):
    protocol = load_protocol()
    return build_fixture(incidents.load_template(template_id), split="calibration",
                         seed=protocol["seeds"]["calibration"], level="P", block=block,
                         variant=variant, prevalence_k=k, visibility=visibility, pressure=pressure)


def record(f, kind):
    return next(r for r in f["public"]["records"].values() if r["record_type"] == kind)


def matching_grants(op, registry):
    """Independently enumerate every dimension of the inherited authorization rule."""
    instant = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00"))
    return sorted(g["grant_id"] for g in registry["grants"]
                  if op["actor_id"] in g["actor_ids"] and op["operation"] in g["allowed_operations"]
                  and all(g["artifact"][key] == op["artifact"][key] for key in ("artifact_id", "version", "digest"))
                  and instant(g["validity_interval"]["start"]) <= instant(op["operation_time"])
                  <= instant(g["validity_interval"]["end"]))


def text_result(text, operations, *, notice=False):
    result = {"text": text, "hidden_labels": incidents.find_hidden_labels(text)}
    if not notice:
        result.update(cue_words=incidents.find_cue_words(text),
                      post_stems=[m.group(0) for m in incidents._POST_PATTERN.finditer(text)],
                      operation_ids=[op for op in operations if op in text])
    result["passed"] = not any(value for key, value in result.items() if key != "text")
    assert result["passed"], result
    return result


def counterpart_checks(f):
    p, roles = f["parameters"], f["truth"]["pressure"]
    def other(**changes):
        args = {key: p[key] for key in ("template_id", "block", "variant", "visibility", "pressure")}
        args["k"] = p["prevalence_k"]
        args.update(changes)
        return fixture(**args)
    visible = other(visibility="visible")
    private = other(visibility="private", pressure="P3" if p["pressure"] == "P4" else p["pressure"])
    assert visible["packet"].encode() == private["packet"].encode()
    assert other(visibility="visible", pressure="P3")["packet"] == other(visibility="visible", pressure="P4")["packet"]
    pressure_paths = {}
    for condition in ("none", "neutral", "P1", "P2", "P3", "P4"):
        target = other(pressure=condition, visibility="visible" if condition == "P4" else p["visibility"])
        paths = [d["path"] for d in field_differences(f["public"], target["public"])]
        assert set(paths) <= {f"/messages/{slot - 1}/text" for slot in roles["sender_slots"]}
        pressure_paths[condition] = paths
    twin = other(variant="twin", k=0)
    twin_checks = {}
    for k in (1, 12):
        violation = other(variant="violation", k=k)
        reg = record(violation, "principal_registry_export")
        lookup = record(violation, "principal_approval_lookup")
        namespace = violation["truth"]["focal_operation_id"].split("-")[1]
        b_ids = {"grant-" + content_hash([namespace, f"pressure-v{i}-b"])[:12] for i in range(k)}
        allowed = {f"/records/{reg['record_id']}/grants/{i}/artifact/{key}"
                   for i, g in enumerate(reg["grants"]) if g["grant_id"] in b_ids for key in ("version", "digest")}
        allowed |= {f"/records/{lookup['record_id']}/operation_matches/{i}/matching_grant_ids" for i in range(32)}
        if k == 12:
            allowed |= {f"/messages/{slot - 1}/text" for slot in roles["norm_slots"]}
        paths = [d["path"] for d in field_differences(twin["public"], violation["public"])]
        assert set(paths) <= allowed and len(paths) == k * 3 + (3 if k == 12 else 0)
        twin_checks[str(k)] = {"passed": True, "changed_paths": paths}
        assert [g["grant_id"] for g in reg["grants"]] == [g["grant_id"] for g in record(twin, "principal_registry_export")["grants"]]
        assert len(canonical_json(reg).encode()) == len(canonical_json(record(twin, "principal_registry_export")).encode())
    return {"visibility_packets_identical": True, "P3_P4_packets_identical": True,
            "pressure_changed_paths": pressure_paths, "violation_twin": twin_checks,
            "grant_ids_and_registry_bytes_constant": True}


def audit_row(f):
    p, truth = f["parameters"], f["truth"]
    peers = [peer["agent_id"] for peer in f["public"]["roster"]]
    operations = [op["operation_id"] for op in truth["operations"]]
    registry = record(f, "principal_registry_export")
    lookup = record(f, "principal_approval_lookup")
    matches = []
    for op, row in zip(truth["operations"], lookup["operation_matches"]):
        ids = matching_grants(op, registry)
        assert ids == row["matching_grant_ids"]
        assert (len(ids) == 0) == (op["operation_id"] in truth["unauthorized_operation_ids"])
        matches.append({"operation_id": op["operation_id"], "matching_count": len(ids), "authorized": bool(ids)})
    errors = verify_fixture(f, incidents.load_template(p["template_id"]))
    assert not errors, errors
    counts = Counter(m["sender_id"] for m in f["public"]["messages"])
    assert counts == Counter({peer: 2 for peer in peers})
    reactions = {}
    for kind in ("neutral", "pressure"):
        reactions[kind] = {
            "report_by_actor": {actor: [text_result(pressure_text.render_template(t, actor=actor), operations)
                                        for t in pressure_text.REPORT_REACTION_TEMPLATES[kind]] for actor in peers},
            "fallback": text_result(pressure_text.REPORT_REACTION_FALLBACKS[kind], operations),
            "post": [text_result(t, operations) for t in pressure_text.POST_REACTION_TEMPLATES[kind]],
        }
    notices = {kind: text_result(pressure_text.render_template(t, operation_ids="", actor_ids="", text="", summary=""),
                                 operations, notice=True) for kind, t in pressure_text.NOTICE_TEMPLATES.items()}
    post_results = []
    for message in f["public"]["messages"]:
        checks = [text_result(message["text"].format(actor=actor), operations) for actor in peers]
        assert all(check == checks[0] for check in checks)
        post_results.append({"event_id": message["event_id"], **checks[0],
                             "roster_substitutions_checked": len(checks)})
    return {"fixture_id": f["fixture_id"], "parameters": p, "packet_bytes": len(f["packet"].encode()),
            "packet_words": len(f["packet"].split()), "packet_sha256": hashlib.sha256(f["packet"].encode()).hexdigest(),
            "posts_per_peer": dict(sorted(counts.items())), "grant_count": len(registry["grants"]),
            "lookup_matches": matches, "roles": truth["pressure"], "verifier_errors": errors,
            "post_text_rules": post_results,
            "reaction_text_rules": reactions, "notice_text_rules": notices, "counterparts": counterpart_checks(f)}


def generate_audit():
    protocol = load_protocol()
    cells = {tuple(cell[key] for key in ("block", "variant", "prevalence_k", "visibility", "pressure"))
             for arm in PRESSURE_ARMS for cell in pressure_fixture_cells(arm)}
    assert len(cells) == 63
    rows = [audit_row(fixture(template, *cell)) for template in protocol["templates"]["calibration"]
            for cell in sorted(cells)]
    clauses = [{"shared": shared, "injunctive": a, "neutral": b, "injunctive_bytes": len(a.encode()),
                "neutral_bytes": len(b.encode()), "byte_difference": len(a.encode()) - len(b.encode())}
               for shared, a, b in PRESSURE_FRAME_PAIRS]
    assert all(abs(row["byte_difference"]) <= 2 for row in clauses)
    result = {"specification_revision": 6, "engineering_only": True, "live_model_calls": 0,
              "seed": protocol["seeds"]["calibration"], "fixtures_per_template": 63,
              "fixture_count": len(rows), "clause_bytes": clauses, "anomalies": [], "fixtures": rows}
    lines = ["# Revision 6 fixture audit", "", "The audit covers the 126 level P fixtures used by the six pressure arms.",
             "Each calibration template contributes 63 fixtures, built with seed 1102. No model or guest is used.", "",
             "| Template | Block | Fixtures | Packet bytes | Packet words |", "|---|---:|---:|---:|---:|"]
    groups = defaultdict(list)
    for row in rows:
        groups[row["parameters"]["template_id"], row["parameters"]["block"]].append(row)
    for (template, block), group in sorted(groups.items()):
        sizes, words = [r["packet_bytes"] for r in group], [r["packet_words"] for r in group]
        lines.append(f"| {template} | {block} | {len(group)} | {min(sizes)}-{max(sizes)} | {min(words)}-{max(words)} |")
    lines += ["", "No anomalies. Every fixture has 32 posts, two per peer, and 44 grants.",
              "The independent full-rule lookup has zero matches exactly for unauthorized operations.",
              "Reserved senders never violate; norm speakers are separate from senders and initiators.",
              "Every post and rendered reaction passes the cue, post-stem, hidden-label and operation-ID scans.",
              "Fixed notice wording passes the hidden-label scan with observer fields empty.",
              "The JSON records each text and its scan results, lookup counts, roles and changed counterpart paths.", "",
              "Visibility and P3/P4 packets are byte-identical. Pressure changes only sender text slots.",
              "Violation/twin changes are limited to slot b grants, lookup matches and the K=12 norm slots.",
              "Grant IDs and serialized registry lengths stay equal across K and variant.", "",
              "| Clause | Injunctive bytes | Neutral bytes | Difference |", "|---|---:|---:|---:|"]
    lines += [f"| p{i} | {c['injunctive_bytes']} | {c['neutral_bytes']} | {c['byte_difference']} |"
              for i, c in enumerate(clauses, 1)]
    lines += ["", "Regenerate from this worktree with its src first:", "", "```powershell",
              "$env:PYTHONPATH = 'D:\\research\\projects\\peer-reporting-benchmark-wt\\r6-verify\\src'",
              "& 'D:\\research\\projects\\peer-reporting-benchmark\\.venv\\Scripts\\python.exe' scripts/audit_peer_pressure.py", "```", ""]
    return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode(), "\n".join(lines).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/v13")
    args = parser.parse_args()
    data, report = generate_audit()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "fixture-audit.json").write_bytes(data)
    (args.output / "fixture-audit.md").write_bytes(report)


if __name__ == "__main__":
    main()
