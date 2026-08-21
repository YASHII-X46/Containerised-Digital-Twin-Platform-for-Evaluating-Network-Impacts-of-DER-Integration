"""Shared fixtures for the OpenDSS solver tests."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from dss_solver.network import SolverNetwork

TESTS_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture
def ieee33_network():
    """The IEEE 33-bus sample network, loaded from the tests data directory."""
    with open(os.path.join(TESTS_DATA_DIR, "ieee33.json"), encoding="utf-8") as f:
        return SolverNetwork(json.load(f))
