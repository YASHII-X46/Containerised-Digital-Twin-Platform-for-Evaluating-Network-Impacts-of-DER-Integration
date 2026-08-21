"""Pluggable KPI (key performance indicator) registry.

A KPI is a named function ``(SimulationResult, KpiContext) -> float`` that scores
a simulation's network impact. Register one to add a metric to every run's
``kpis`` output and to the study runner — no engine edits. These are the impact
measures a DER-penetration study compares across penetration levels / strategies.
"""

from dataclasses import dataclass

from app.simulation.results import SimulationResult, converged_or_all


@dataclass
class KpiContext:
    """Thresholds, timing, tariffs, and network facts a KPI may need."""

    step_hours: float = 0.25
    v_lower: float = 0.95
    v_upper: float = 1.05
    thermal_limit_pct: float = 100.0
    # Branch ids that are transformers (for transformer-specific KPIs).
    transformer_branch_ids: frozenset = frozenset()
    # Time-of-use tariff (AUD/kWh) for cost KPIs; peak window on a 24-h clock.
    tariff_peak_rate: float = 0.45
    tariff_offpeak_rate: float = 0.22
    tariff_feed_in_rate: float = 0.05
    tariff_peak_start: float = 15.0
    tariff_peak_end: float = 21.0
    # Expected feeder net load (kW) per timestep, from the generated profiles.
    # Used by the energy-balance self-check; empty when unavailable.
    expected_net_kw: tuple = ()
    # Named tariff structure (from the tariff registry); when set it overrides
    # the flat TOU fields below for the cost KPIs.
    tariff: object = None
    # Ambient air temperature (deg C) for the transformer hot-spot/ageing KPI.
    transformer_ambient_c: float = 25.0
    # Grid emissions intensity for imported energy (kg CO2e per kWh).
    emissions_kg_per_kwh: float = 0.60

    def import_rate(self, hour: float) -> float:
        """The import tariff (AUD/kWh) applying at an hour of day."""
        if self.tariff is not None:
            return self.tariff.import_rate(hour)
        s, e = self.tariff_peak_start, self.tariff_peak_end
        in_peak = (s <= hour < e) if s <= e else (hour >= s or hour < e)
        return self.tariff_peak_rate if in_peak else self.tariff_offpeak_rate

    def feed_in_rate(self) -> float:
        """The export (feed-in) rate in AUD/kWh."""
        if self.tariff is not None:
            return self.tariff.feed_in_rate
        return self.tariff_feed_in_rate


Kpi = callable  # (SimulationResult, KpiContext) -> float

_REGISTRY: dict[str, dict] = {}


def register(name: str, fn, description: str = "", unit: str = "") -> None:
    _REGISTRY[name] = {"fn": fn, "description": description, "unit": unit}


def kpi_names() -> list[dict]:
    return [
        {"name": n, "description": _REGISTRY[n]["description"], "unit": _REGISTRY[n]["unit"]}
        for n in sorted(_REGISTRY)
    ]


def compute_kpis(result: SimulationResult, ctx: KpiContext) -> dict[str, float]:
    """Evaluate every registered KPI for a simulation result."""
    return {name: round(float(meta["fn"](result, ctx)), 4) for name, meta in _REGISTRY.items()}


# ---- built-in KPIs --------------------------------------------------------

def _max_voltage_pu(result, ctx):
    return result.max_voltage_pu


def _min_voltage_pu(result, ctx):
    return result.min_voltage_pu


def _max_thermal_loading_pct(result, ctx):
    return result.max_loading_pct


def _voltage_violations(result, ctx):
    return result.total_voltage_violations


def _thermal_violations(result, ctx):
    return result.total_thermal_violations


def _voltage_violation_rate(result, ctx):
    """Fraction of (bus × timestep) samples outside the voltage band."""
    valid = converged_or_all(result.timesteps)
    samples = sum(len(ts.bus_voltages_pu) for ts in valid)
    return (result.total_voltage_violations / samples) if samples else 0.0


def _total_losses_kwh(result, ctx):
    return sum(ts.total_losses_kw for ts in converged_or_all(result.timesteps)) * ctx.step_hours


def _reverse_power_hours(result, ctx):
    """Hours the substation exports (net reverse power) — a DER-penetration tell."""
    return sum(
        ctx.step_hours for ts in converged_or_all(result.timesteps) if ts.total_power_kw < 0
    )


def _converged_fraction(result, ctx):
    total = len(result.timesteps)
    return (sum(1 for ts in result.timesteps if ts.converged) / total) if total else 1.0


def _max_transformer_loading_pct(result, ctx):
    """Highest transformer loading — the EV-fleet / DER stress figure for the
    distribution transformer. 0 when the network has no transformers."""
    if not ctx.transformer_branch_ids:
        return 0.0
    peak = 0.0
    for ts in converged_or_all(result.timesteps):
        for branch_id in ctx.transformer_branch_ids:
            peak = max(peak, ts.branch_loadings_pct.get(branch_id, 0.0))
    return peak


def _hour_of_day(ts, ctx) -> float:
    """Hour-of-day of a timestep (multi-day runs wrap every 24 h)."""
    return (ts.timestep * ctx.step_hours) % 24.0


def _energy_cost_aud(result, ctx):
    """Import energy cost under the time-of-use tariff (substation import only)."""
    return sum(
        max(0.0, ts.total_power_kw) * ctx.step_hours * ctx.import_rate(_hour_of_day(ts, ctx))
        for ts in converged_or_all(result.timesteps)
    )


def _export_revenue_aud(result, ctx):
    """Feed-in revenue for energy exported through the substation."""
    return sum(
        max(0.0, -ts.total_power_kw) * ctx.step_hours * ctx.feed_in_rate()
        for ts in converged_or_all(result.timesteps)
    )


def _net_energy_cost_aud(result, ctx):
    return _energy_cost_aud(result, ctx) - _export_revenue_aud(result, ctx)


def _energy_balance_error_pct(result, ctx):
    """Self-check: source power should equal expected net load plus losses.

    Computed against the generated profiles, so a healthy uncoordinated run sits
    near 0%. Autonomous Volt-Watt or DR curtailment deliberately moves the
    actual injections away from the expected profiles, so under those modes this
    figure also carries the control action — read it as model+control deviation.
    """
    expected = ctx.expected_net_kw
    if not expected:
        return 0.0
    err_kwh = 0.0
    ref_kwh = 0.0
    for ts in converged_or_all(result.timesteps):
        if ts.timestep >= len(expected):
            continue
        net = float(expected[ts.timestep])
        err_kwh += abs(ts.total_power_kw - ts.total_losses_kw - net) * ctx.step_hours
        ref_kwh += abs(net) * ctx.step_hours
    return (err_kwh / ref_kwh * 100.0) if ref_kwh > 1e-9 else 0.0


register("max_voltage_pu", _max_voltage_pu, "Highest bus voltage", "pu")
register("min_voltage_pu", _min_voltage_pu, "Lowest bus voltage", "pu")
register("max_thermal_loading_pct", _max_thermal_loading_pct, "Highest branch loading", "%")
register("voltage_violations", _voltage_violations, "Voltage-violation sample count", "count")
register("thermal_violations", _thermal_violations, "Thermal-violation sample count", "count")
register("voltage_violation_rate", _voltage_violation_rate, "Fraction of bus×time samples out of band", "fraction")
register("total_losses_kwh", _total_losses_kwh, "Energy losses over the day", "kWh")
register("reverse_power_hours", _reverse_power_hours, "Hours of net reverse power at the substation", "h")
register("converged_fraction", _converged_fraction, "Fraction of timesteps that converged", "fraction")
register("max_transformer_loading_pct", _max_transformer_loading_pct,
         "Highest transformer loading", "%")
register("energy_cost_aud", _energy_cost_aud,
         "Import energy cost under the time-of-use tariff", "AUD")
register("export_revenue_aud", _export_revenue_aud,
         "Feed-in revenue for exported energy", "AUD")
register("net_energy_cost_aud", _net_energy_cost_aud,
         "Import cost minus export revenue", "AUD")
register("energy_balance_error_pct", _energy_balance_error_pct,
         "Self-check: |source - losses - expected net| vs expected energy", "%")


def _doe_curtailed_kwh(result, ctx):
    """Export removed by operating-envelope enforcement (0 when no scheme ran)."""
    return getattr(result, "doe_curtailed_kwh", 0.0)


def _doe_envelope_utilisation_pct(result, ctx):
    """Achieved export as a share of the published envelope (autonomous mode)."""
    envelope = getattr(result, "doe_envelope_kwh", 0.0)
    if envelope <= 1e-9:
        return 0.0
    return getattr(result, "doe_export_kwh", 0.0) / envelope * 100.0


register("doe_curtailed_kwh", _doe_curtailed_kwh,
         "Export curtailed by the operating-envelope scheme", "kWh")
register("doe_envelope_utilisation_pct", _doe_envelope_utilisation_pct,
         "Share of the published envelope actually exported", "%")


def _max_vuf_pct(result, ctx):
    """Worst voltage-unbalance factor across the run (IEC planning limit 2%)."""
    return max(
        (getattr(ts, "max_vuf_pct", 0.0) for ts in converged_or_all(result.timesteps)),
        default=0.0,
    )


# Transformer insulation ageing (IEC 60076-7-style simplified model): hot-spot
# temperature rises HOTSPOT_RISE_K above ambient at rated load with a 1.6
# loading exponent; ageing doubles every 6 K above the 98 degC reference; the
# insulation's normal life is INSULATION_LIFE_HOURS at reference.
HOTSPOT_RISE_K = 78.0
HOTSPOT_REFERENCE_C = 98.0
INSULATION_LIFE_HOURS = 180_000.0


def _transformer_loss_of_life_pct(result, ctx):
    """Worst-transformer insulation life consumed over the horizon (%).

    Steady-state per-interval hot-spot from the loading factor K:
    theta_hs = ambient + 78 * K^1.6; ageing acceleration 2^((theta_hs - 98)/6).
    A transformer held at rated load in a 20 degC ambient ages at ~1x; the
    evening EV peak pushes K (and ageing) up exponentially.
    """
    if not ctx.transformer_branch_ids:
        return 0.0
    worst = 0.0
    for branch_id in ctx.transformer_branch_ids:
        aged_hours = 0.0
        for ts in converged_or_all(result.timesteps):
            k = ts.branch_loadings_pct.get(branch_id, 0.0) / 100.0
            theta_hs = ctx.transformer_ambient_c + HOTSPOT_RISE_K * (k ** 1.6)
            faa = 2.0 ** ((theta_hs - HOTSPOT_REFERENCE_C) / 6.0)
            aged_hours += faa * ctx.step_hours
        worst = max(worst, aged_hours / INSULATION_LIFE_HOURS * 100.0)
    return worst


def _emissions_kg_co2e(result, ctx):
    """Emissions attributed to imported energy (grid intensity x import kWh)."""
    import_kwh = sum(
        max(0.0, ts.total_power_kw) * ctx.step_hours
        for ts in converged_or_all(result.timesteps)
    )
    return import_kwh * ctx.emissions_kg_per_kwh


register("max_vuf_pct", _max_vuf_pct,
         "Worst voltage-unbalance factor (negative/positive sequence)", "%")
register("transformer_loss_of_life_pct", _transformer_loss_of_life_pct,
         "Worst-transformer insulation life consumed over the horizon", "%")
register("emissions_kg_co2e", _emissions_kg_co2e,
         "Emissions from imported energy at the configured grid intensity", "kg")
