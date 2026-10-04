"""Atomic JSON artifacts and checkpoints, without provider dependencies."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..events import canonical_json, content_hash
from .config import read_json


def atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    encoded = canonical_json(value) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seal(value: dict[str, Any]) -> dict[str, Any]:
    if "seal_hash" in value:
        raise ValueError("already sealed")
    return {**value, "seal_hash": content_hash(value)}


def check_seal(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("sealed object required")
    payload = {key: item for key, item in value.items() if key != "seal_hash"}
    if value.get("seal_hash") != content_hash(payload):
        raise ValueError("artifact seal mismatch")


def read_sealed(path: Path) -> dict[str, Any]:
    value = read_json(path)
    check_seal(value)
    return value


def safe_child(directory: Path, relative: str) -> Path:
    """Treat archive paths as untrusted, including Windows drive/UNC syntax."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("invalid artifact path")
    parts = relative.split("/")
    if relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid artifact path")
    root = directory.resolve()
    result = directory.joinpath(*parts).resolve()
    if not result.is_relative_to(root):
        raise ValueError("artifact path escapes its collection")
    return result
