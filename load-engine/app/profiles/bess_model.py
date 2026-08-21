"""Battery energy storage system (BESS) model with SOC tracking.

Single-node energy balance — no chemistry or thermal modelling. State-of-health
ageing (cycle + calendar fade) is modelled separately via ``degrade_soh`` and the
usable capacity it derates (see ``create_bess``).
"""

import numpy as np


class BESSModel:
    """Battery energy storage with SOC tracking."""

    def __init__(
        self,
        capacity_kwh: float,
        max_charge_kw: float,
        max_discharge_kw: float,
        initial_soc: float = 0.5,
        min_soc: float = 0.1,
        max_soc: float = 0.95,
        efficiency: float = 0.92,
    ):
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if max_charge_kw < 0 or max_discharge_kw < 0:
            raise ValueError("max power ratings must be non-negative")
        if not (0.0 <= initial_soc <= 1.0):
            raise ValueError("initial_soc must be between 0 and 1")
        if not (0.0 <= min_soc < max_soc <= 1.0):
            raise ValueError("SOC limits must satisfy 0 <= min_soc < max_soc <= 1")
        if not (0.0 < efficiency <= 1.0):
            raise ValueError("efficiency must be in (0, 1]")

        self.capacity_kwh = capacity_kwh
        self.max_charge_kw = max_charge_kw
        self.max_discharge_kw = max_discharge_kw
        self.min_soc = min_soc
        self.max_soc = max_soc
        # `efficiency` is the round-trip efficiency (the value quoted on battery
        # datasheets). Split it evenly over charge and discharge so the modelled
        # round trip equals it, rather than applying it on each leg (which would
        # make the effective round trip efficiency**2).
        self.efficiency = efficiency
        self._one_way_eff = efficiency ** 0.5
        self._initial_soc = initial_soc
        self._energy_kwh = initial_soc * capacity_kwh

    @property
    def soc(self) -> float:
        return self._energy_kwh / self.capacity_kwh

    @property
    def energy_kwh(self) -> float:
        return self._energy_kwh

    def available_charge_kw(self, duration_hours: float = 0.25) -> float:
        """Max power that can be absorbed (charging) over a timestep of the
        given duration, bounded by SOC headroom and the charge rating."""
        headroom_kwh = (self.max_soc * self.capacity_kwh) - self._energy_kwh
        max_from_soc = headroom_kwh / duration_hours
        return min(self.max_charge_kw, max(0.0, max_from_soc))

    def available_discharge_kw(self, duration_hours: float = 0.25) -> float:
        """Max power that can be delivered (discharging) over a timestep of the
        given duration, bounded by usable energy and the discharge rating."""
        available_kwh = self._energy_kwh - (self.min_soc * self.capacity_kwh)
        max_from_soc = available_kwh / duration_hours
        return min(self.max_discharge_kw, max(0.0, max_from_soc))

    def step(self, power_kw: float, duration_hours: float = 0.25) -> float:
        """Execute one timestep.

        Args:
            power_kw: Positive = discharge, negative = charge.
            duration_hours: Timestep duration (default 15 min).

        Returns:
            Actual power delivered/absorbed (may be less than requested).
        """
        if power_kw > 0:
            # Discharging
            available_kwh = self._energy_kwh - (self.min_soc * self.capacity_kwh)
            max_power = min(self.max_discharge_kw, available_kwh / duration_hours)
            actual_power = min(power_kw, max(0.0, max_power))
            energy_removed = actual_power * duration_hours / self._one_way_eff
            self._energy_kwh -= energy_removed
        else:
            # Charging
            headroom_kwh = (self.max_soc * self.capacity_kwh) - self._energy_kwh
            max_power = min(self.max_charge_kw, headroom_kwh / duration_hours)
            actual_power = max(power_kw, -max(0.0, max_power))
            energy_added = abs(actual_power) * duration_hours * self._one_way_eff
            self._energy_kwh += energy_added

        self._energy_kwh = np.clip(
            self._energy_kwh,
            self.min_soc * self.capacity_kwh,
            self.max_soc * self.capacity_kwh,
        )
        return actual_power

    def reset(self, soc: float | None = None) -> None:
        """Reset battery state."""
        if soc is None:
            soc = self._initial_soc
        if not (0.0 <= soc <= 1.0):
            raise ValueError("soc must be between 0 and 1")
        self._energy_kwh = soc * self.capacity_kwh


# ---------------------------------------------------------------------------
# Named configs for common Australian residential batteries
# ---------------------------------------------------------------------------

BESS_CONFIGS: dict[str, dict] = {
    "powerwall_2": {
        "capacity_kwh": 13.5,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "efficiency": 0.90,
    },
    "powerwall_3": {
        "capacity_kwh": 13.5,
        "max_charge_kw": 11.5,
        "max_discharge_kw": 11.5,
        "efficiency": 0.92,
    },
    "byd_hvs": {
        "capacity_kwh": 10.24,
        "max_charge_kw": 5.12,
        "max_discharge_kw": 5.12,
        "efficiency": 0.93,
    },
    "enphase_5p": {
        "capacity_kwh": 5.0,
        "max_charge_kw": 3.84,
        "max_discharge_kw": 3.84,
        "efficiency": 0.89,
    },
    "generic_small": {
        "capacity_kwh": 5.0,
        "max_charge_kw": 2.5,
        "max_discharge_kw": 2.5,
        "efficiency": 0.90,
    },
    "generic_medium": {
        "capacity_kwh": 10.0,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "efficiency": 0.90,
    },
    # Asymmetric inverter: charges slowly, discharges fast (separate ratings).
    "hybrid_asym_3_6": {
        "capacity_kwh": 9.6,
        "max_charge_kw": 3.3,
        "max_discharge_kw": 6.6,
        "efficiency": 0.945,
    },
}


# Battery ageing: state-of-health (SoH) falls with energy throughput (cycling)
# and with calendar time. Defaults give ~80% SoH near 4000 equivalent full
# cycles plus ~2%/year of calendar fade — representative of modern Li-ion.
CYCLE_FADE_PER_EFC = 5.0e-5          # SoH lost per equivalent full cycle
CALENDAR_FADE_PER_DAY = 0.02 / 365.0  # SoH lost per day


def degrade_soh(soh: float, equivalent_cycles: float, days: float = 1.0) -> float:
    """New state of health after `equivalent_cycles` of throughput over `days`."""
    loss = CYCLE_FADE_PER_EFC * equivalent_cycles + CALENDAR_FADE_PER_DAY * days
    return max(0.0, soh - loss)


def create_bess(
    config_name: str = "powerwall_2",
    initial_soc: float = 0.5,
    initial_soh: float = 1.0,
    *,
    capacity_kwh: float | None = None,
    max_charge_kw: float | None = None,
    max_discharge_kw: float | None = None,
) -> BESSModel:
    """Create a BESSModel from a named configuration.

    Any of ``capacity_kwh``, ``max_charge_kw``, ``max_discharge_kw`` override the
    named config, so a bus can set independent charge and discharge power limits.
    ``initial_soh`` (0-1) shrinks the usable capacity to model an aged battery.
    """
    if config_name not in BESS_CONFIGS:
        raise ValueError(
            f"Unknown BESS config '{config_name}'. "
            f"Available: {list(BESS_CONFIGS.keys())}"
        )
    params = dict(BESS_CONFIGS[config_name])
    if capacity_kwh is not None:
        params["capacity_kwh"] = capacity_kwh
    if max_charge_kw is not None:
        params["max_charge_kw"] = max_charge_kw
    if max_discharge_kw is not None:
        params["max_discharge_kw"] = max_discharge_kw
    # Usable capacity is the nameplate scaled by state of health.
    params["capacity_kwh"] = params["capacity_kwh"] * initial_soh
    return BESSModel(initial_soc=initial_soc, **params)


def generate_self_consumption_schedule(
    load_kw: np.ndarray, pv_kw: np.ndarray, bess: BESSModel, step_hours: float = 0.25
) -> dict:
    """Default BESS dispatch: charge from excess PV, discharge during deficit.

    Args:
        step_hours: Timestep duration in hours (set from the scenario
            resolution; 0.25 = 15-minute steps).

    Returns:
        Dict with bess_power_kw, bess_soc, energy_charged_kwh,
        energy_discharged_kwh, and cycles.
    """
    n = len(load_kw)
    bess_power = np.zeros(n)
    bess_soc = np.zeros(n)
    energy_charged = 0.0
    energy_discharged = 0.0

    for i in range(n):
        net = load_kw[i] - pv_kw[i]

        if net < 0:
            # Excess PV → charge battery
            request = net  # negative = charge
        else:
            # Deficit → discharge battery
            request = net  # positive = discharge

        actual = bess.step(request, duration_hours=step_hours)
        bess_power[i] = actual
        bess_soc[i] = bess.soc

        if actual < 0:
            energy_charged += abs(actual) * step_hours
        else:
            energy_discharged += actual * step_hours

    cycles = energy_discharged / bess.capacity_kwh if bess.capacity_kwh > 0 else 0.0

    return {
        "bess_power_kw": bess_power,
        "bess_soc": bess_soc,
        "energy_charged_kwh": energy_charged,
        "energy_discharged_kwh": energy_discharged,
        "cycles": cycles,
    }


def _in_window(hour: float, window: tuple[float, float]) -> bool:
    """Whether hour-of-day falls in [start, end), with midnight wrap-around."""
    start, end = window
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def generate_time_of_use_schedule(
    load_kw: np.ndarray,
    bess: BESSModel,
    step_hours: float = 0.25,
    charge_window: tuple[float, float] = (1.0, 6.0),
    discharge_window: tuple[float, float] = (17.0, 21.0),
) -> dict:
    """Time-of-use dispatch: charge during the off-peak window (at the charge
    rating), discharge during the peak window (at the discharge rating, bounded
    by demand). Idle otherwise. Honours the separate charge/discharge limits.
    """
    n = len(load_kw)
    bess_power = np.zeros(n)
    bess_soc = np.zeros(n)
    energy_charged = 0.0
    energy_discharged = 0.0

    for i in range(n):
        hour = (i * step_hours) % 24.0
        if _in_window(hour, charge_window):
            request = -bess.available_charge_kw(step_hours)        # negative = charge
        elif _in_window(hour, discharge_window):
            request = min(bess.available_discharge_kw(step_hours), max(0.0, float(load_kw[i])))
        else:
            request = 0.0

        actual = bess.step(request, duration_hours=step_hours)
        bess_power[i] = actual
        bess_soc[i] = bess.soc

        if actual < 0:
            energy_charged += abs(actual) * step_hours
        else:
            energy_discharged += actual * step_hours

    cycles = energy_discharged / bess.capacity_kwh if bess.capacity_kwh > 0 else 0.0

    return {
        "bess_power_kw": bess_power,
        "bess_soc": bess_soc,
        "energy_charged_kwh": energy_charged,
        "energy_discharged_kwh": energy_discharged,
        "cycles": cycles,
    }


def dispatch_bess(
    mode: str,
    load_kw: np.ndarray,
    pv_kw: np.ndarray,
    bess: BESSModel,
    step_hours: float = 0.25,
    charge_window: tuple[float, float] = (1.0, 6.0),
    discharge_window: tuple[float, float] = (17.0, 21.0),
) -> dict:
    """Run the selected BESS dispatch strategy.

    'self_consumption' (default) charges from excess PV and discharges on
    deficit; 'time_of_use' charges off-peak and discharges at peak.
    """
    if mode == "time_of_use":
        return generate_time_of_use_schedule(
            load_kw, bess, step_hours, charge_window, discharge_window
        )
    return generate_self_consumption_schedule(load_kw, pv_kw, bess, step_hours)
