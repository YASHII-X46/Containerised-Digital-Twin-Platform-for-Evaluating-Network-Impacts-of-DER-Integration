"""Autonomous smart-inverter Volt-VAr and Volt-Watt responses (AS/NZS 4777.2).

Each PV inverter responds to its local voltage:

  - Volt-VAr: inject VARs to support low voltage, absorb VARs to curb high
    voltage, with a deadband around nominal (the reactive-power mode).
  - Volt-Watt: reduce real-power output linearly once voltage rises past the
    Volt-Watt knee (the standard's real-power backstop, engaging above the
    Volt-VAr range).

These are the autonomous inverter layers that run during the power-flow solve,
independent of (and underneath) any DR coordination, which manages commanded
real-power curtailment.

Applied as a short per-timestep fixed point: read voltages, set each inverter's
kvar (and kW when Volt-Watt is on) from the curves, re-solve, repeat a few times.
"""

from dataclasses import dataclass


@dataclass
class VoltVarCurve:
    """Four-point Volt-VAr curve. Voltages in per-unit; reactive as a fraction of
    inverter rating. Defaults follow the AS/NZS 4777.2 reactive-power mode."""

    v1: float = 0.92   # at/below: full VAr injection (raises voltage)
    v2: float = 0.98   # deadband lower edge
    v3: float = 1.02   # deadband upper edge
    v4: float = 1.08   # at/above: full VAr absorption (lowers voltage)
    q_max_frac: float = 0.44  # max |Q| as a fraction of inverter rating

    def factor(self, voltage: float) -> float:
        """Reactive factor in [-1, +1]: +1 inject (low V), -1 absorb (high V)."""
        if voltage <= self.v1:
            return 1.0
        if voltage < self.v2:
            return (self.v2 - voltage) / (self.v2 - self.v1)
        if voltage <= self.v3:
            return 0.0
        if voltage < self.v4:
            return -(voltage - self.v3) / (self.v4 - self.v3)
        return -1.0

    def kvar(self, voltage: float, rating_kw: float) -> float:
        """Reactive setpoint (kvar) for an inverter at this voltage."""
        return self.q_max_frac * rating_kw * self.factor(voltage)


@dataclass
class VoltWattCurve:
    """Volt-Watt real-power reduction. Defaults follow the AS/NZS 4777.2
    Australia A response: full output at/below 1.09 pu, ramping linearly down to
    the 20% floor at 1.10 pu and above."""

    v_start: float = 1.09   # knee: reduction begins above this voltage
    v_end: float = 1.10     # at/above: output held at the floor
    p_min_frac: float = 0.2  # output floor as a fraction of the scheduled power

    def factor(self, voltage: float) -> float:
        """Real-power factor in [p_min_frac, 1.0] for the local voltage."""
        if voltage <= self.v_start:
            return 1.0
        if voltage >= self.v_end:
            return self.p_min_frac
        frac = (voltage - self.v_start) / (self.v_end - self.v_start)
        return 1.0 - (1.0 - self.p_min_frac) * frac


def apply_inverter_control(
    engine,
    pv_ratings: dict[int, float],
    expected_pv_kw: dict[int, float],
    volt_var: VoltVarCurve | None,
    volt_watt: VoltWattCurve | None,
    iterations: int = 3,
) -> bool:
    """Run the autonomous inverter fixed point for the current timestep.

    Args:
        engine: the OpenDSS engine (already solved for this timestep).
        pv_ratings: bus_id -> inverter rating (kW) for the PV systems.
        expected_pv_kw: bus_id -> this timestep's scheduled PV output (kW),
            the baseline Volt-Watt reduces from.
        volt_var: reactive-power curve, or None to skip.
        volt_watt: real-power reduction curve, or None to skip.
        iterations: fixed-point iterations.

    Returns the final convergence flag.
    """
    converged = True
    for _ in range(iterations):
        voltages = engine.get_bus_voltages_pu()
        for bus_id, rating in pv_ratings.items():
            v = voltages.get(bus_id, 1.0)
            if volt_var is not None:
                engine.update_pv_reactive(bus_id, volt_var.kvar(v, rating))
            if volt_watt is not None:
                scheduled = expected_pv_kw.get(bus_id, 0.0)
                engine.update_pv(bus_id, scheduled * volt_watt.factor(v))
        converged = engine.solve()
    return converged


def apply_volt_var(engine, pv_ratings: dict[int, float], curve: VoltVarCurve,
                   iterations: int = 3) -> bool:
    """Volt-VAr-only fixed point (see ``apply_inverter_control``)."""
    return apply_inverter_control(engine, pv_ratings, {}, curve, None, iterations)
