"""Tests for autonomous smart-inverter Volt-VAr and Volt-Watt (AS/NZS 4777.2)."""

import pytest

from app.config import settings
from app.control.volt_var import VoltVarCurve, VoltWattCurve
from app.simulation.loader import get_pv_buses
from app.simulation.qsts import QSTSSimulation


def test_curve_factor_breakpoints():
    c = VoltVarCurve()
    assert c.factor(0.90) == 1.0   # below v1 -> full injection
    assert c.factor(1.00) == 0.0   # deadband -> no reactive
    assert c.factor(1.20) == -1.0  # above v4 -> full absorption
    assert c.factor(0.95) == pytest.approx((0.98 - 0.95) / (0.98 - 0.92))
    assert c.factor(1.05) == pytest.approx(-(1.05 - 1.02) / (1.08 - 1.02))


def test_kvar_sign_convention():
    c = VoltVarCurve()
    assert c.kvar(0.90, 10.0) > 0    # inject at low voltage (raises it)
    assert c.kvar(1.20, 10.0) < 0    # absorb at high voltage (lowers it)
    assert c.kvar(1.00, 10.0) == 0   # deadband


def _high_pv_profiles():
    """33 buses, large PV at the feeder end (bus 18) to force over-voltage."""
    buses = {}
    for bus in range(1, 34):
        pv = 800.0 if bus == 18 else 0.0
        buses[bus] = {
            "pv_capacity_kw": pv,
            "bess_capacity_kwh": 0.0,
            "ev_charge_rate_kw": 0.0,
            "timeseries": [
                {
                    "timestamp": f"2024-01-15T12:0{t}:00",
                    "load_kw": 10.0, "load_kvar": 5.0,
                    "pv_kw": pv, "bess_power_kw": 0.0, "ev_charge_kw": 0.0,
                    "net_load_kw": 10.0 - pv, "other_der_kw": 0.0,
                }
                for t in range(2)
            ],
        }
    return {
        "metadata": {"scenario_name": "hv", "seed": 1, "der_penetration_percent": 300,
                     "timesteps": 2, "resolution_minutes": 15},
        "buses": buses,
    }


def _run(net, make_remote_engine, profiles, volt_var=None, volt_watt=None):
    engine = make_remote_engine(net, {"pv": get_pv_buses(profiles)})
    return QSTSSimulation(
        engine, profiles, settings, net, volt_var=volt_var, volt_watt=volt_watt
    ).run()


def test_volt_var_reduces_overvoltage(ieee33_network, make_remote_engine):
    net = ieee33_network
    profiles = _high_pv_profiles()

    base = _run(net, make_remote_engine, profiles, volt_var=None)
    assert base.max_voltage_pu > 1.02, "scenario should produce over-voltage to correct"

    with_vv = _run(net, make_remote_engine, profiles, volt_var=VoltVarCurve())
    assert with_vv.max_voltage_pu < base.max_voltage_pu  # reactive absorption pulls it down


def test_volt_watt_curve_breakpoints():
    c = VoltWattCurve()
    assert c.factor(1.00) == 1.0                    # normal voltage: full output
    assert c.factor(1.09) == 1.0                    # at the knee: still full
    assert c.factor(1.095) == pytest.approx(0.6)    # halfway down the ramp
    assert c.factor(1.10) == pytest.approx(0.2)     # at/above the end: floor
    assert c.factor(1.20) == pytest.approx(0.2)


# A curve whose knee sits inside this scenario's over-voltage (~1.047 pu), so
# the response engages; the standard 1.09/1.10 defaults are covered by the
# breakpoint unit test above.
_TIGHT_VW = VoltWattCurve(v_start=1.02, v_end=1.03, p_min_frac=0.2)


def test_volt_watt_reduces_overvoltage_via_real_power(ieee33_network, make_remote_engine):
    net = ieee33_network
    profiles = _high_pv_profiles()

    base = _run(net, make_remote_engine, profiles)
    assert base.max_voltage_pu > 1.03, "scenario should engage the test knee"

    with_vw = _run(net, make_remote_engine, profiles, volt_watt=_TIGHT_VW)
    # Real-power reduction pulls the over-voltage down.
    assert with_vw.max_voltage_pu < base.max_voltage_pu


def test_volt_var_and_volt_watt_combine(ieee33_network, make_remote_engine):
    net = ieee33_network
    profiles = _high_pv_profiles()
    only_vv = _run(net, make_remote_engine, profiles, volt_var=VoltVarCurve())
    both = _run(net, make_remote_engine, profiles, volt_var=VoltVarCurve(), volt_watt=_TIGHT_VW)
    # Adding the real-power backstop can only help (or match) the reactive mode.
    assert both.max_voltage_pu <= only_vv.max_voltage_pu + 1e-9
