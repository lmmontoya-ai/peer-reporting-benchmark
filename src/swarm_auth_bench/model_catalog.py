"""Pinned Luna client settings for direct, individually logged tool calls."""

from __future__ import annotations

import hashlib
from pathlib import Path

UPSTREAM_COMMIT = "064c6b8c737f5b41d171fdda80bd9ef10ad06eb3"
CATALOG_PATH = Path(__file__).parent / "data/luna-direct-catalog.json"
CATALOG_SHA256 = "9a3bd7b0899b2bfcbe3b9f0749241e29f63c67fee2f6275c71d28d37b268f193"
OVERRIDES = {
    "tool_mode": "direct",
    "use_responses_lite": False,
    "multi_agent_version": "disabled",
    "experimental_supported_tools": [],
}


def luna_catalog() -> tuple[bytes, dict]:
    """Fail closed if the reviewed asset changes, including in a built wheel."""
    data = CATALOG_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != CATALOG_SHA256:
        raise RuntimeError("Pinned Luna model catalog hash mismatch")
    return data, {
        "sha256": digest,
        "upstream_codex_commit": UPSTREAM_COMMIT,
        "upstream_path": "codex-rs/models-manager/models.json",
        "overrides": OVERRIDES,
        "model_identifier_unchanged": "gpt-6-luna",
        "scope": "client tool configuration; does not pin the served model weights",
    }
