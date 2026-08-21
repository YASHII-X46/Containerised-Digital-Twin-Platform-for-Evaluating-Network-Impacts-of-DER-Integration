"""Tests for the QSTS simulation orchestrator (end-to-end over the solver bus)."""

import pytest

from app.config import settings
from app.simulation.results import TimestepResult, converged_or_all
from app.simulation.loader import (
    get_bess_buses,
    get_ev_buses,
    get_pv_buses,
)
from app.simulation.qsts import QSTSSimulation


def _element_buses(profiles: dict) -> dict:
    return {
        "pv": get_pv_buses(profiles),
        "bess": get_bess_buses(profiles),
        "ev": get_ev_buses(profiles),
    }


def test_profiles_expose_der_buses(sample_profiles):
    assert sample_profiles["metadata"]["total_buses"] == 33
    assert sample_profiles["metadata"]["timesteps"] == 2
    assert sample_profiles["metadata"]["resolution_minutes"] > 0
    assert len(get_pv_buses(sample_profiles)) == 2
    assert len(get_bess_buses(sample_profiles)) == 1
    assert len(get_ev_buses(sample_profiles)) == 1


def test_qsts_runs_and_converges(sample_profiles, ieee33_network, make_remote_engine):
    net = ieee33_network
    profiles = sample_profiles
    engine = make_remote_engine(net, _element_buses(profiles))

    result = QSTSSimulation(engine, profiles, settings, net).run()

    assert len(result.timesteps) == 2
    assert all(ts.converged for ts in result.timesteps)
    assert 0.5 < result.min_voltage_pu <= result.max_voltage_pu < 1.5
    # Every timestep reports a voltage for all 33 buses and loading for 32 branches.
    for ts in result.timesteps:
        assert len(ts.bus_voltages_pu) == 33
        assert len(ts.branch_loadings_pct) == 32


@pytest.mark.parametrize("solve_mode", ["balanced", "unbalanced"])
def test_qsts_runs_in_both_solve_modes(sample_profiles, ieee33_network,
                                       make_remote_engine, solve_mode):
    net = ieee33_network
    profiles = sample_profiles
    engine = make_remote_engine(net, _element_buses(profiles), solve_mode)

    result = QSTSSimulation(engine, profiles, settings, net).run()

    assert all(ts.converged for ts in result.timesteps)
    assert 0.5 < result.min_voltage_pu <= result.max_voltage_pu < 1.5
    for ts in result.timesteps:
        assert len(ts.bus_voltages_pu) == 33
        assert len(ts.branch_loadings_pct) == 32


def _timestep(idx: int, converged: bool, voltage: float) -> TimestepResult:
    return TimestepResult(
        timestep=idx,
        timestamp=f"t{idx}",
        converged=converged,
        bus_voltages_pu={1: voltage},
        branch_loadings_pct={1: 0.0},
        total_losses_kw=0.0,
        total_power_kw=0.0,
    )


def test_converged_or_all_drops_non_converged():
    # A non-converged solve leaves a non-physical voltage (0.2 pu here) that
    # must not leak into summary statistics.
    steps = [_timestep(0, True, 0.98), _timestep(1, False, 0.2)]
    valid = converged_or_all(steps)
    assert [ts.timestep for ts in valid] == [0]
    assert min(v for ts in valid for v in ts.bus_voltages_pu.values()) == 0.98


def test_converged_or_all_falls_back_when_none_converged():
    # If nothing converges we keep the full list so callers never see an empty
    # result; the non-convergence is surfaced via the converged-timestep count.
    steps = [_timestep(0, False, 0.2), _timestep(1, False, 0.3)]
    assert converged_or_all(steps) == steps


class _RecordingEngine:
    """Minimal OpenDSS stand-in that records the building loads it is given."""

    def __init__(self):
        self.loads = {}

    def update_load(self, bus_id, kw, kvar):
        self.loads[bus_id] = (kw, kvar)

    def update_pv(self, *a):
        pass

    def update_bess(self, *a):
        pass

    def update_ev(self, *a):
        pass

    def solve(self):
        return True

    def get_bus_voltages_pu(self):
        return {2: 1.0}

    def get_branch_loadings_pct(self):
        return {1: 0.0}

    def get_total_losses_kw(self):
        return 0.0

    def get_total_power_kw(self):
        return 0.0

    def get_max_vuf_pct(self):
        return 0.0


class _StubNet:
    source_bus = 1


def _one_bus_profiles(extra: dict) -> dict:
    ts = {
        "timestamp": "t0", "load_kw": 80.0, "load_kvar": 48.0,
        "pv_kw": 0.0, "bess_power_kw": 0.0, "ev_charge_kw": 0.0,
        "net_load_kw": 80.0, **extra,
    }
    return {
        "metadata": {"scenario_name": "t", "seed": 1, "der_penetration_percent": 0,
                     "timesteps": 1, "resolution_minutes": 15},
        "buses": {2: {"pv_capacity_kw": 0.0, "bess_capacity_kwh": 0.0,
                      "ev_charge_rate_kw": 0.0, "timeseries": [ts]}},
    }


def test_other_der_kw_folded_into_building_load():
    """A non-builtin DER's net contribution shifts the building load generically."""
    eng = _RecordingEngine()
    profiles = _one_bus_profiles({"other_der_kw": 2.0, "net_load_kw": 82.0})
    QSTSSimulation(eng, profiles, settings, _StubNet()).run()
    assert eng.loads[2] == (82.0, 48.0)  # 80 building + 2 other DER


def test_missing_other_der_kw_defaults_to_building_load():
    eng = _RecordingEngine()
    profiles = _one_bus_profiles({})  # legacy payload: no other_der_kw
    QSTSSimulation(eng, profiles, settings, _StubNet()).run()
    assert eng.loads[2] == (80.0, 48.0)
