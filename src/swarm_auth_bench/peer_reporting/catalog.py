"""Reviewed direct-tool client settings for the three exact study model IDs.

This is a new runtime branch. It preserves the historical Luna asset and adapter.
The pinned catalog controls the client, not the remotely served model weights.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from ..model_catalog import OVERRIDES, UPSTREAM_COMMIT, luna_catalog
from ..runtime import CodexRuntime, RuntimeProtocolError
from .config import MODELS

CATALOG_VERSION = "peer-direct-catalog-v1"
UPSTREAM_SHA256 = "365eaf1afa7c70df1495721d59f75ad1968918b6b004f473525c166e530ebe5b"
CATALOG_HASHES = {
    "gpt-6-sol": "76f3d97946113579fb919821c1ef738cc75d658069c289f248ac809bbb697b34",
    "gpt-6-astra": "d44e2c0896abafa01ca5f862c934b392aef238b547e79e649fe53d4f90fbf589",
}
DATA = Path(__file__).parent / "data"


def reviewed_catalog(model: str) -> tuple[bytes, dict[str, Any]]:
    if model not in MODELS:
        raise RuntimeProtocolError("unreviewed peer-reporting model; no fallback is permitted")
    if model == "gpt-6-luna":
        data, legacy = luna_catalog()
        expected = legacy["sha256"]
    else:
        data = (DATA / f"{model}-direct.json").read_bytes()
        expected = CATALOG_HASHES[model]
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise RuntimeProtocolError("peer model catalog differs from its reviewed hash")
    entries = json.loads(data)["models"]
    if len(entries) != 1 or entries[0].get("slug") != model:
        raise RuntimeProtocolError("peer catalog must contain the exact requested model only")
    if any(entries[0].get(key) != value for key, value in OVERRIDES.items()):
        raise RuntimeProtocolError("peer catalog direct-tool overrides differ")
    return data, {
        "version": CATALOG_VERSION, "requested_model": model, "catalog_sha256": digest,
        "upstream_commit": UPSTREAM_COMMIT, "upstream_catalog_sha256": UPSTREAM_SHA256,
        "upstream_path": "codex-rs/models-manager/models.json", "reviewed_overrides": deepcopy(OVERRIDES),
        "served_snapshot_pinned": False, "scope": "client tool configuration only",
    }


class ReviewedPeerRuntime(CodexRuntime):
    def __init__(self, *, model: str, reasoning_effort: str = "xhigh", **kwargs: Any):
        reviewed_catalog(model)
        if reasoning_effort != "xhigh":
            raise RuntimeProtocolError("peer study requires the declared xhigh reasoning setting")
        super().__init__(model=model, reasoning_effort=reasoning_effort, **kwargs)

    @property
    def metadata(self) -> dict[str, Any]:
        return {**super().metadata, "peer_catalog_override": reviewed_catalog(self.model)[1]}

    async def _launch(self, *, home: Path, env_extra: Mapping[str, str] | None = None,
                      overrides: tuple[str, ...] = ()):
        data, _ = reviewed_catalog(self.model)
        if any(setting.startswith("model_catalog_json=") for setting in overrides):
            raise RuntimeProtocolError("caller cannot override the reviewed model catalog")
        if self.model == "gpt-6-luna":
            # The unchanged parent applies exactly the same legacy bytes.
            return await super()._launch(home=home, env_extra=env_extra, overrides=overrides)
        path = home / "peer-model-catalog.json"
        path.write_bytes(data)
        return await super()._launch(home=home, env_extra=env_extra, overrides=(
            f"model_catalog_json={json.dumps(path.as_posix())}", *overrides))
