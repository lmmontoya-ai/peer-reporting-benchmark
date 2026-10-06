"""Windows ledger replacement regression; no runtime, provider, or guest calls."""

import json
import sys

import pytest

from swarm_auth_bench.peer_reporting.storage import atomic_json


@pytest.mark.xfail(sys.platform == "win32", strict=True,
                   reason="Windows ledger readers deny os.replace, causing simulated execution-check holds")
def test_atomic_ledger_update_while_reader_is_open(tmp_path):
    ledger = tmp_path / "budget-ledger.json"
    old, new = {"reserved_tokens": 75000}, {"reserved_tokens": 0}
    atomic_json(ledger, old)
    with ledger.open("rb") as reader:
        atomic_json(ledger, new)
        assert json.load(reader) == old
    assert json.loads(ledger.read_text(encoding="utf-8")) == new
