"""Tests for the pluggable tariff registry and its KPI coupling."""

import pytest

from app.metrics import KpiContext, compute_kpis
from app.metrics import tariffs as tariffs_module
from app.metrics.tariffs import Tariff, available_tariffs, get_tariff, register_tariff
from app.simulation.results import SimulationResult, TimestepResult


def test_builtin_tariffs_registered():
    names = {t["name"] for t in available_tariffs()}
    assert {"tou_residential", "flat"} <= names


def test_flat_tariff_is_hour_independent():
    flat = get_tariff("flat")
    assert flat.import_rate(3.0) == flat.import_rate(18.0)


def test_tou_tariff_honours_peak_window():
    tou = get_tariff("tou_residential")
    assert tou.import_rate(18.0) == tou.peak_rate       # inside 15:00-21:00
    assert tou.import_rate(3.0) == tou.offpeak_rate


def test_unknown_tariff_raises_with_names():
    with pytest.raises(KeyError, match="flat"):
        get_tariff("bogus")


def test_custom_tariff_registers_and_prices_kpis():
    ts = TimestepResult(
        timestep=0, timestamp="t0", converged=True,
        bus_voltages_pu={1: 1.0}, branch_loadings_pct={},
        total_losses_kw=0.0, total_power_kw=100.0,   # importing 100 kW
    )
    result = SimulationResult(
        scenario_name="t", seed=1, der_penetration_percent=100, timesteps=[ts],
        total_voltage_violations=0, total_thermal_violations=0,
        min_voltage_pu=1.0, max_voltage_pu=1.0, max_loading_pct=0.0,
        simulation_time_seconds=0.0,
    )
    saved = dict(tariffs_module._REGISTRY)
    try:
        register_tariff(Tariff(
            name="ev_night_saver", description="test", peak_rate=0.50,
            offpeak_rate=0.08, feed_in_rate=0.02, peak_start=15.0, peak_end=21.0,
        ))
        tariff = get_tariff("ev_night_saver")
        # Hour 0 is off-peak: 100 kW x 0.25 h x 0.08 AUD/kWh.
        k = compute_kpis(result, KpiContext(step_hours=0.25, tariff=tariff))
        assert abs(k["energy_cost_aud"] - 100 * 0.25 * 0.08) < 1e-9
    finally:
        tariffs_module._REGISTRY.clear()
        tariffs_module._REGISTRY.update(saved)
