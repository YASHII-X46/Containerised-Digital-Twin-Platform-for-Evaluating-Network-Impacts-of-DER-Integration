"""Tests for dynamic operating envelopes (computation and allocation)."""

import numpy as np
import pytest

from app.control.envelopes import compute_envelopes, envelope_buses
from app.network.model import NetworkModel
from app.simulation.loader import get_pv_buses

V_LIMIT = 1.01   # tight limit so envelopes bind well below capability


def _network() -> NetworkModel:
    return NetworkModel.from_dict({
        "id": "env3", "name": "Envelope test", "base_voltage_kv": 11.0,
        "source_bus": 1,
        "buses": [
            {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
            {"bus_id": 2, "base_load_kw": 60.0, "base_load_kvar": 20.0},
            {"bus_id": 3, "base_load_kw": 40.0, "base_load_kvar": 15.0},
        ],
        "branches": [
            {"branch_id": 1, "from_bus": 1, "to_bus": 2, "r_ohm": 2.0, "x_ohm": 1.2, "rating_kva": 2000},
            {"branch_id": 2, "from_bus": 2, "to_bus": 3, "r_ohm": 3.0, "x_ohm": 1.8, "rating_kva": 1500},
        ],
    })


def _profiles(timesteps=3, pv2=2000.0, pv3=1000.0) -> dict:
    def bus(load, pv):
        return {
            "pv_capacity_kw": pv, "bess_capacity_kwh": 0.0, "ev_charge_rate_kw": 0.0,
            "timeseries": [
                {"timestamp": f"t{t}", "load_kw": load, "load_kvar": load * 0.35,
                 "pv_kw": pv, "bess_power_kw": 0.0, "ev_charge_kw": 0.0,
                 "net_load_kw": load - pv, "other_der_kw": 0.0}
                for t in range(timesteps)
            ],
        }
    return {
        "metadata": {"scenario_name": "env", "seed": 1, "der_penetration_percent": 100,
                     "timesteps": timesteps, "resolution_minutes": 15},
        "buses": {1: bus(0.0, 0.0), 2: bus(60.0, pv2), 3: bus(40.0, pv3)},
    }


def _engine(net, profiles, make_remote_engine):
    return make_remote_engine(net, {"pv": get_pv_buses(profiles)})


def _apply_and_check(engine, net, profiles, limits, t, v_limit) -> float:
    """Apply the envelope's limits as actual export at interval t; return max V."""
    from app.control.envelopes import _apply_baseline
    _apply_baseline(engine, net, profiles, t)
    for b, series in limits.items():
        engine.update_pv(b, float(series[t]))
    assert engine.solve()
    return max(engine.get_bus_voltages_pu().values())


def test_envelope_buses_and_empty():
    p = _profiles(pv2=0.0, pv3=0.0)
    assert envelope_buses(p) == []
    assert envelope_buses(_profiles()) == [2, 3]


def test_static_mode_constant_limits(make_remote_engine):
    net, profiles = _network(), _profiles()
    engine = _engine(net, profiles, make_remote_engine)
    limits = compute_envelopes(engine, net, profiles, mode="static", fixed_export_kw=5.0)
    for b in (2, 3):
        assert np.allclose(limits[b], 5.0)


def test_search_respects_voltage_limit(make_remote_engine):
    net, profiles = _network(), _profiles()
    engine = _engine(net, profiles, make_remote_engine)
    limits = compute_envelopes(
        engine, net, profiles, mode="dynamic", method="search", v_limit=V_LIMIT)
    # Envelopes bind (finite, below capability) and honour pro-rata shape.
    assert 0.0 < limits[2][0] < 2000.0
    assert limits[2][0] / limits[3][0] == pytest.approx(2.0, rel=1e-3)
    # Exporting exactly at the envelope stays inside the limit (search is exact
    # up to its bisection step).
    vmax = _apply_and_check(engine, net, profiles, limits, 0, V_LIMIT)
    assert vmax <= V_LIMIT + 2e-3


def test_sensitivity_equal_and_prorata(make_remote_engine):
    net, profiles = _network(), _profiles()
    engine = _engine(net, profiles, make_remote_engine)
    eq = compute_envelopes(engine, net, profiles, mode="dynamic",
                           method="sensitivity", allocation="equal", v_limit=V_LIMIT)
    # Equal policy: the same kW at every site (both far below capability here).
    assert eq[2][0] == pytest.approx(eq[3][0], rel=1e-6)
    assert 0.0 < eq[2][0] < 2000.0

    pr = compute_envelopes(engine, net, profiles, mode="dynamic",
                           method="sensitivity", allocation="prorata", v_limit=V_LIMIT)
    assert pr[2][0] / pr[3][0] == pytest.approx(2.0, rel=1e-6)

    # The linearised allocation, applied for real, stays near the limit
    # (small tolerance for linearisation error).
    vmax = _apply_and_check(engine, net, profiles, eq, 0, V_LIMIT)
    assert vmax <= V_LIMIT + 3e-3


def test_max_total_beats_equal(make_remote_engine):
    net, profiles = _network(), _profiles()
    engine = _engine(net, profiles, make_remote_engine)
    eq = compute_envelopes(engine, net, profiles, mode="dynamic",
                           method="sensitivity", allocation="equal", v_limit=V_LIMIT)
    mx = compute_envelopes(engine, net, profiles, mode="dynamic",
                           method="sensitivity", allocation="max_total", v_limit=V_LIMIT)
    total_eq = eq[2][0] + eq[3][0]
    total_mx = mx[2][0] + mx[3][0]
    assert total_mx >= total_eq - 1e-6
    # Maximal-throughput favours the electrically stronger (upstream) site.
    assert mx[2][0] >= mx[3][0]


def test_qsts_autonomous_enforcement(make_remote_engine):
    """The QSTS loop holds every site at its envelope and books the outcome."""
    import numpy as np
    from app.config import settings
    from app.simulation.qsts import QSTSSimulation

    net, profiles = _network(), _profiles()
    T = profiles["metadata"]["timesteps"]

    free = QSTSSimulation(
        _engine(net, profiles, make_remote_engine), profiles, settings, net).run()

    limits = {2: np.full(T, 20.0), 3: np.full(T, 10.0)}   # tight static caps
    capped = QSTSSimulation(
        _engine(net, profiles, make_remote_engine), profiles, settings, net,
        envelopes=limits).run()

    assert capped.doe_active and not free.doe_active
    assert capped.doe_curtailed_kwh > 0.0
    assert capped.max_voltage_pu < free.max_voltage_pu   # caps relieve the rise
    assert capped.doe_export_kwh <= capped.doe_envelope_kwh + 1e-9
    assert len(capped.doe_envelope_total) == T
    assert len(capped.doe_export_total) == T
    # Feeder envelope each step = 20 + 10 kW.
    assert capped.doe_envelope_total[0] == pytest.approx(30.0)


def test_allocation_registry_builtins_and_custom():
    from app.control import envelopes as env
    from app.control.envelopes import _allocate, available_allocations, register_allocation

    names = {a["name"] for a in available_allocations()}
    assert {"equal", "prorata", "max_total"} <= names

    S = np.ones((1, 2)) * 0.01
    head = np.array([1.0])
    caps = np.array([500.0, 500.0])
    with pytest.raises(ValueError, match="Unknown envelope allocation"):
        _allocate(S, head, caps, "bogus")

    saved = dict(env._ALLOCATION_REGISTRY)
    try:
        # A drop-in policy is dispatched by name with no engine edits.
        register_allocation("first_only", lambda S, h, c: np.array([c[0], 0.0]),
                            "everything to site one")
        out = _allocate(S, head, caps, "first_only")
        assert out[0] == 500.0 and out[1] == 0.0
    finally:
        env._ALLOCATION_REGISTRY.clear()
        env._ALLOCATION_REGISTRY.update(saved)


def test_zero_headroom_gives_zero_envelope(make_remote_engine):
    net, profiles = _network(), _profiles()
    engine = _engine(net, profiles, make_remote_engine)
    # A limit below the no-export baseline voltage leaves no headroom at all.
    limits = compute_envelopes(engine, net, profiles, mode="dynamic",
                               method="sensitivity", allocation="equal", v_limit=0.90)
    assert np.allclose(limits[2], 0.0) and np.allclose(limits[3], 0.0)
