"""Shared real-template WP6 study. Caps are test-only and authorize no execution."""

import pytest

from swarm_auth_bench.peer_reporting.storage import read_sealed
from swarm_auth_bench.peer_reporting_v11.collection import STUDY_MANIFEST, build_study
from swarm_auth_bench.peer_reporting_v11.config import load_protocol
from swarm_auth_bench.peer_reporting_v11.incidents import load_all_templates

from .live_fakes import caps_record


@pytest.fixture(scope="session")
def wp6_inputs():
    return {"protocol": load_protocol(), "templates": load_all_templates(), "caps_record": caps_record()}


@pytest.fixture(scope="session")
def wp6_study(tmp_path_factory, wp6_inputs):
    directory = tmp_path_factory.mktemp("wp6") / "study"
    result = build_study(directory, **wp6_inputs)
    manifest = read_sealed(directory / STUDY_MANIFEST)
    return directory, manifest, result
