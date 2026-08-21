"""Shared pytest fixtures for the Simulation Engine tests."""

import os
import shutil
import tempfile

# The engine ships no built-in networks. Tests keep a sample model under
# tests/data/ and seed a throwaway NETWORKS_DIR with it so the app registry has
# something to serve. Both env vars must be set before app.config is imported
# (Settings reads the environment at import time).
TESTS_DIR = os.path.dirname(__file__)
TESTS_DATA_DIR = os.path.join(TESTS_DIR, "data")

os.environ.setdefault("BUS_ENABLED", "false")  # no broker in tests
_NETWORKS_DIR = tempfile.mkdtemp(prefix="dt_test_networks_")
shutil.copy(os.path.join(TESTS_DATA_DIR, "ieee33.json"), _NETWORKS_DIR)
os.environ.setdefault("NETWORKS_DIR", _NETWORKS_DIR)

import sys

import pytest

from app.network.model import NetworkModel

# Sample (non-shipped) network model used as a test fixture / registry seed.
SAMPLE_NETWORKS_DIR = TESTS_DATA_DIR

# The power-flow solver lives in its own container/package (opendss-solver).
# For tests, the REAL solver service is run in-process on the loopback bus,
# imported from the sibling checkout — the same code the container runs.
OPENDSS_SOLVER_DIR = os.path.abspath(
    os.path.join(TESTS_DIR, "..", "..", "opendss-solver")
)
if os.path.isdir(OPENDSS_SOLVER_DIR) and OPENDSS_SOLVER_DIR not in sys.path:
    sys.path.insert(0, OPENDSS_SOLVER_DIR)


@pytest.fixture
def solver_bus(tmp_path):
    """A loopback bus carrying the real opendss-solver service.

    Returns the Simulation Engine's bus participant on that transport (what
    ``run_simulation`` uses as ``app.state.bus``). Skips when the sibling
    opendss-solver checkout or OpenDSSDirect.py is unavailable.
    """
    solver_service_mod = pytest.importorskip(
        "dss_solver.service",
        reason="requires the sibling opendss-solver checkout + OpenDSSDirect.py",
    )
    from dss_solver.config import Settings as SolverSettings

    from app.bus import BusParticipant, LoopbackTransport

    transport = LoopbackTransport()
    solver_participant = BusParticipant(transport, service="opendss-solver")
    solver = solver_service_mod.OpenDSSSolverService(
        SolverSettings(DSS_DIR=str(tmp_path / "dss_work"))
    )
    solver.register(solver_participant)
    solver_participant.start()

    sim_participant = BusParticipant(transport, service="sim-engine")
    sim_participant.start()
    return sim_participant


@pytest.fixture
def make_remote_engine(solver_bus):
    """Factory building RemoteSolverEngine sessions against the loopback solver."""
    from app.solvers import RemoteSolverEngine

    engines = []

    def make(net, element_buses=None, solve_mode="balanced"):
        engine = RemoteSolverEngine(
            solver_bus, "opendss-solver",
            net if isinstance(net, dict) else net.to_dict(),
            element_buses or {}, solve_mode,
        )
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        engine.close()


@pytest.fixture
def ieee33_network():
    """The IEEE 33-bus sample network, loaded from the tests data directory."""
    return NetworkModel.from_json_file(os.path.join(TESTS_DATA_DIR, "ieee33.json"))


PV_BUSES = {18, 22}
BESS_BUSES = {18}
EV_BUSES = {25}


@pytest.fixture
def sample_profiles():
    """A minimal but complete inline profiles payload (the Load Engine wire format).

    33 buses x 2 timesteps, with PV on two buses, BESS on one, and EV on one, so
    the QSTS loop exercises every DER element type. Mirrors what the Load Engine
    delivers over the bus — no shared CSV.
    """
    buses = {}
    for bus in range(1, 34):
        has_pv = bus in PV_BUSES
        has_bess = bus in BESS_BUSES
        has_ev = bus in EV_BUSES
        timeseries = [
            {
                "timestep": t + 1,
                "timestamp": f"2024-01-15T00:{t:02d}:00",
                "load_kw": 80.0,
                "load_kvar": 48.0,
                "pv_kw": 30.0 if has_pv else 0.0,
                "bess_power_kw": 5.0 if has_bess else 0.0,
                "bess_soc": 0.5 if has_bess else 0.0,
                "ev_charge_kw": 7.0 if has_ev else 0.0,
                "net_load_kw": 50.0,
                "other_der_kw": 0.0,
            }
            for t in range(2)
        ]
        buses[bus] = {
            "customer_class": "res_detached_medium",
            "base_load_kw": 100.0,
            "base_load_kvar": 60.0,
            "pv_capacity_kw": 50.0 if has_pv else 0.0,
            "bess_capacity_kwh": 13.5 if has_bess else 0.0,
            "ev_charge_rate_kw": 7.0 if has_ev else 0.0,
            "timeseries": timeseries,
        }
    return {
        "metadata": {
            "scenario_name": "test",
            "seed": 42,
            "der_penetration_percent": 100.0,
            "total_buses": 33,
            "timesteps": 2,
            "resolution_minutes": 15,
        },
        "buses": buses,
    }
