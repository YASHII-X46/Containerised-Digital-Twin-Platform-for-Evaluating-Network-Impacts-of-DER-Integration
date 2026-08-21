"""Tests for the pluggable KPI registry."""

from app.metrics import KpiContext, compute_kpis, kpi_names, register
from app.metrics import kpi_registry
from app.simulation.results import SimulationResult, TimestepResult


def _result():
    ts0 = TimestepResult(
        timestep=0, timestamp="t0", converged=True,
        bus_voltages_pu={1: 1.0, 2: 1.06}, branch_loadings_pct={1: 50.0},
        total_losses_kw=10.0, total_power_kw=100.0,
        voltage_violations=[{"bus_id": 2, "type": "over"}], thermal_violations=[],
    )
    ts1 = TimestepResult(
        timestep=1, timestamp="t1", converged=True,
        bus_voltages_pu={1: 1.0, 2: 0.93}, branch_loadings_pct={1: 120.0},
        total_losses_kw=8.0, total_power_kw=-20.0,  # net reverse power
        voltage_violations=[{"bus_id": 2, "type": "under"}],
        thermal_violations=[{"branch_id": 1}],
    )
    return SimulationResult(
        scenario_name="t", seed=42, der_penetration_percent=100, timesteps=[ts0, ts1],
        total_voltage_violations=2, total_thermal_violations=1,
        min_voltage_pu=0.93, max_voltage_pu=1.06, max_loading_pct=120.0,
        simulation_time_seconds=0.0,
    )


def test_builtin_kpis_registered():
    names = {k["name"] for k in kpi_names()}
    assert {"max_voltage_pu", "min_voltage_pu", "total_losses_kwh",
            "reverse_power_hours", "voltage_violation_rate"} <= names


def test_compute_kpis_values():
    k = compute_kpis(_result(), KpiContext(step_hours=0.25))
    assert k["max_voltage_pu"] == 1.06
    assert k["min_voltage_pu"] == 0.93
    assert k["max_thermal_loading_pct"] == 120.0
    assert k["voltage_violations"] == 2
    assert k["thermal_violations"] == 1
    assert abs(k["voltage_violation_rate"] - 0.5) < 1e-9   # 2 of 4 samples
    assert abs(k["total_losses_kwh"] - 4.5) < 1e-9         # (10+8)*0.25
    assert abs(k["reverse_power_hours"] - 0.25) < 1e-9     # one reverse-power step
    assert k["converged_fraction"] == 1.0


def test_transformer_loading_kpi():
    # Branch 1 is a transformer -> its worst loading; no transformers -> 0.
    k = compute_kpis(_result(), KpiContext(transformer_branch_ids=frozenset({1})))
    assert k["max_transformer_loading_pct"] == 120.0
    k0 = compute_kpis(_result(), KpiContext())
    assert k0["max_transformer_loading_pct"] == 0.0


def test_tariff_cost_kpis():
    # ts0 (hour 0.00, off-peak): imports 100 kW -> 100*0.25*0.22 = 5.5 AUD.
    # ts1 (hour 0.25, off-peak): exports 20 kW -> 20*0.25*0.05 = 0.25 AUD.
    k = compute_kpis(_result(), KpiContext(step_hours=0.25))
    assert abs(k["energy_cost_aud"] - 5.5) < 1e-9
    assert abs(k["export_revenue_aud"] - 0.25) < 1e-9
    assert abs(k["net_energy_cost_aud"] - 5.25) < 1e-9
    # A peak window covering hour 0 charges the peak rate instead.
    k_peak = compute_kpis(
        _result(),
        KpiContext(step_hours=0.25, tariff_peak_start=23.0, tariff_peak_end=6.0),
    )
    assert abs(k_peak["energy_cost_aud"] - 100 * 0.25 * 0.45) < 1e-9


def test_energy_balance_self_check():
    # Expected net exactly matches P - losses at both steps -> 0% error.
    exact = (90.0, -28.0)
    k = compute_kpis(_result(), KpiContext(step_hours=0.25, expected_net_kw=exact))
    assert k["energy_balance_error_pct"] == 0.0
    # A 10 kW mismatch at each step against 118 kW of reference -> ~16.95%.
    off = (100.0, -18.0)
    k2 = compute_kpis(_result(), KpiContext(step_hours=0.25, expected_net_kw=off))
    assert abs(k2["energy_balance_error_pct"] - (20.0 / 118.0 * 100)) < 0.01
    # No expected series -> the check is inert.
    assert compute_kpis(_result(), KpiContext())["energy_balance_error_pct"] == 0.0


def test_vuf_kpi_takes_worst_step():
    r = _result()
    r.timesteps[0].max_vuf_pct = 0.4
    r.timesteps[1].max_vuf_pct = 1.7
    k = compute_kpis(r, KpiContext())
    assert k["max_vuf_pct"] == 1.7
    # Absent VUF data (legacy results) reads as balanced.
    assert compute_kpis(_result(), KpiContext())["max_vuf_pct"] == 0.0


def test_transformer_loss_of_life_kpi():
    from app.metrics.kpi_registry import (
        HOTSPOT_REFERENCE_C, HOTSPOT_RISE_K, INSULATION_LIFE_HOURS,
    )
    # Branch 1 as a transformer at 50% then 120% loading, 20 degC ambient.
    ctx = KpiContext(step_hours=0.25, transformer_branch_ids=frozenset({1}),
                     transformer_ambient_c=20.0)
    k = compute_kpis(_result(), ctx)
    expected = 0.0
    for loading in (50.0, 120.0):
        theta = 20.0 + HOTSPOT_RISE_K * (loading / 100.0) ** 1.6
        expected += 2.0 ** ((theta - HOTSPOT_REFERENCE_C) / 6.0) * 0.25
    expected = expected / INSULATION_LIFE_HOURS * 100.0
    # compute_kpis rounds every KPI to 4 decimals.
    assert k["transformer_loss_of_life_pct"] == round(expected, 4)
    # Heavier loading ages faster: the 120% step dominates the 50% one.
    assert k["transformer_loss_of_life_pct"] > 0.0
    # No transformers -> 0.
    assert compute_kpis(_result(), KpiContext())["transformer_loss_of_life_pct"] == 0.0


def test_emissions_kpi_counts_import_only():
    # ts0 imports 100 kW for 0.25 h; ts1 exports (no emissions credit).
    k = compute_kpis(_result(), KpiContext(step_hours=0.25, emissions_kg_per_kwh=0.6))
    assert abs(k["emissions_kg_co2e"] - 100 * 0.25 * 0.6) < 1e-9


def test_register_new_kpi_appears():
    saved = dict(kpi_registry._REGISTRY)
    try:
        register("peak_export_kw", lambda r, c: -min(ts.total_power_kw for ts in r.timesteps),
                 "Peak reverse power", "kW")
        k = compute_kpis(_result(), KpiContext())
        assert k["peak_export_kw"] == 20.0
    finally:
        kpi_registry._REGISTRY.clear()
        kpi_registry._REGISTRY.update(saved)
