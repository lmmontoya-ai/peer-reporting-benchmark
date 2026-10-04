"""Local publication-preview preparation and audit; no network or Git operations."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent
SOURCE = OUTPUT.parents[1]


def files_to_copy():
    paths = [SOURCE / name for name in ("pyproject.toml", "uv.lock", ".gitattributes")]
    paths += [path for path in (SOURCE / "src").rglob("*")
              if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    paths += sorted((SOURCE / "configs").glob("peer-reporting*.json"))
    paths += sorted((SOURCE / "tests").glob("test_peer_reporting*.py"))
    paths += [SOURCE / "isolation" / "probe.py"]
    paths += [SOURCE / "scripts" / name for name in (
        "summarize_peer_collection.py", "export_peer_compatibility.py", "propose_peer_caps.py",
        "peer_reporting_preflight.py", "monitor_peer_collection.py")]
    docs = (
        "peer-reporting-spec.md", "peer-reporting-protocol.json", "peer-reporting-source-review.md",
        "peer-reporting-source-review.json", "peer-reporting-resource-proposal.json",
        "peer-reporting-fixture-review.json", "peer-reporting-human-review-plan-v1.json",
        "peer-reporting-auth-recovery-proposal.json", "peer-reporting-demo-notes.txt", "runtime.md",
    )
    paths += [SOURCE / "docs" / name for name in docs]
    return paths


def copy_files():
    copied = []
    for source in files_to_copy():
        if not source.is_file():
            raise FileNotFoundError(source.name)
        relative = source.relative_to(SOURCE)
        target = OUTPUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(relative.as_posix())
    return copied


RULES = {
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "openai_like_token": re.compile(r"(?<![\w-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "huggingface_like_token": re.compile(r"(?<![\w])hf_[A-Za-z0-9]{20,}"),
    "github_like_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "credential_assignment_candidate": re.compile(
        r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
        r"[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{16,}[\"']"),
}


def audit(copied):
    entries, findings = [], []
    for path in sorted(OUTPUT.rglob("*")):
        if not path.is_file() or any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"}
                                     for part in path.parts):
            continue
        relative = path.relative_to(OUTPUT).as_posix()
        if relative in {"file-manifest.json", "publication-audit.json", "validation.json"}:
            continue
        raw = path.read_bytes()
        entries.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                        "origin": "copied_unchanged" if relative in copied else "preview_authored"})
        text = raw.decode("utf-8", errors="replace")
        matches = {name: len(pattern.findall(text)) for name, pattern in RULES.items()}
        matches = {name: count for name, count in matches.items() if count}
        if matches:
            findings.append({"path": relative, "rule_counts": matches})
    identical = all(hashlib.sha256((SOURCE / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
                    for entry in entries if entry["origin"] == "copied_unchanged")
    manifest = {"kind": "peer_reporting_publication_file_manifest_v1", "hash_algorithm": "sha256",
                "metadata_excluded_from_self_hash": ["file-manifest.json", "publication-audit.json", "validation.json"],
                "files": entries, "file_count": len(entries), "total_bytes": sum(entry["bytes"] for entry in entries)}
    (OUTPUT / "file-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    data = {
        "kind": "peer_reporting_publication_preview_audit_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "copied_file_count": len(copied), "copied_bytes_match_source": identical,
        "included": ["Python source and synthetic package assets", "dependency metadata and lockfile",
                     "peer-reporting configs and tests", "peer-reporting helper scripts",
                     "specification, source attribution, resource proposal, human review plan and runtime docs",
                     "AI-assisted project README and local preparation script"],
        "excluded": [".git and repository history", "original .local tree and raw dataset cache",
                     "runs, results, study and legacy observed agent data", "authentication and environment files",
                     "compiled files and cache directories", "private review bindings and any live outcomes",
                     "original repository README and unrelated documentation/configs/tests"],
        "scan": {"scope": "every manifest file", "rule_names": list(RULES),
                 "candidate_file_count": len(findings), "candidate_files_and_counts": findings,
                 "matched_values_disclosed": False,
                 "limit": "Pattern scan is not a proof that every possible secret format is absent."},
        "publication_status": "local_preview_only_no_commit_push_visibility_change_or_form_submission",
        "result_status": "no_live_benchmark_outcomes_included",
        "validation_record": "validation.json",
        "known_limits": [
            "Repository source license is not declared; Codex-derived catalog assets retain Apache 2.0 notice/license.",
            "Supporting source modules include legacy software paths, but no legacy observed datasets or run artifacts.",
            "Historical doc references may target documents outside this focused preview.",
            "Human review plan is bound to its original sealed collection plan, not a newly generated offline plan.",
            "Public source review uses original paraphrase and provenance only; AI Village custom terms remain applicable.",
            "AI Digest publication notification remains the repository owner's responsibility.",
            "Live collection requires separately qualified isolation/runtime/model access and evidence gates.",
            "Human must write submission form answers without AI; this README is an AI-assisted project artifact.",
            "Pattern findings must be assessed before publication; no credentials or token values are printed.",
        ],
    }
    (OUTPUT / "publication-audit.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"],
                      "copied_bytes_match_source": identical, "scan_findings": findings}))


if __name__ == "__main__":
    audit(copy_files())
