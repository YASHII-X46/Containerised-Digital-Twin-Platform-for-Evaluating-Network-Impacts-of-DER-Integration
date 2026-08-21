"""Data structures for holding simulation results and violation checking."""

from dataclasses import dataclass, field


@dataclass
class TimestepResult:
    timestep: int
    timestamp: str
    converged: bool
    bus_voltages_pu: dict[int, float]
    branch_loadings_pct: dict[int, float]
    total_losses_kw: float
    total_power_kw: float
    voltage_violations: list[dict] = field(default_factory=list)
    thermal_violations: list[dict] = field(default_factory=list)
    # Worst voltage-unbalance factor (%) this step (0 on balanced solves).
    max_vuf_pct: float = 0.0


@dataclass
class SimulationResult:
    scenario_name: str
    seed: int
    der_penetration_percent: float
    timesteps: list[TimestepResult]
    total_voltage_violations: int
    total_thermal_violations: int
    min_voltage_pu: float
    max_voltage_pu: float
    max_loading_pct: float
    simulation_time_seconds: float
    # Dynamic-operating-envelope outcomes (zeros when no envelope scheme ran).
    doe_active: bool = False
    doe_curtailed_kwh: float = 0.0        # export removed by envelope enforcement
    doe_envelope_kwh: float = 0.0         # total published envelope energy
    doe_export_kwh: float = 0.0           # export actually achieved under it
    doe_envelope_total: list = field(default_factory=list)  # feeder kW per step
    doe_export_total: list = field(default_factory=list)    # feeder kW per step


def converged_or_all(timesteps: list[TimestepResult]) -> list[TimestepResult]:
    """Return only the timesteps whose power flow converged.

    A non-converged solve leaves OpenDSS holding its last (non-physical)
    iterate, so those bus voltages, branch loadings and losses are meaningless
    and must not be folded into summary statistics. Falls back to the full list
    only when *nothing* converged, so callers never face an empty result (the
    non-convergence is surfaced separately via the converged-timestep count).
    """
    converged = [ts for ts in timesteps if ts.converged]
    return converged if converged else timesteps


def check_violations(
    voltages: dict[int, float],
    loadings: dict[int, float],
    v_lower: float = 0.95,
    v_upper: float = 1.05,
    thermal_limit: float = 100.0,
) -> tuple[list[dict], list[dict]]:
    """Check for voltage and thermal violations.

    Returns:
        (voltage_violations, thermal_violations) as lists of dicts.
    """
    voltage_violations = []
    for bus_id, v_pu in voltages.items():
        if v_pu < v_lower:
            voltage_violations.append(
                {"bus_id": bus_id, "voltage_pu": round(v_pu, 6), "type": "under"}
            )
        elif v_pu > v_upper:
            voltage_violations.append(
                {"bus_id": bus_id, "voltage_pu": round(v_pu, 6), "type": "over"}
            )

    thermal_violations = []
    for branch_id, loading_pct in loadings.items():
        if loading_pct > thermal_limit:
            thermal_violations.append(
                {"branch_id": branch_id, "loading_pct": round(loading_pct, 3)}
            )

    return voltage_violations, thermal_violations
