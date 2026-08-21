"""EV charging load profile generator for residential buses.

Charger ratings, session energy, and arrival spread are calibrated to public EV
charging data (Caltech/JPL ACN-Data) and Australian residential driving: a
typical day tops up only the energy used by daily driving (~10-12 kWh), not a
full pack, and home arrivals are spread across the evening rather than
synchronised. The per-session physics (energy = ΔSOC × capacity) is unchanged.
"""

import numpy as np

TIMESTEPS = 96  # default daily resolution (15-min)
STEP_HOURS = 0.25

EV_CONFIGS: dict[str, dict] = {
    "level2_7kw": {
        "charge_rate_kw": 7.0,
        "battery_kwh": 60.0,
        "description": "Standard Australian Level 2 home charger (32A single-phase)",
    },
    "level2_11kw": {
        "charge_rate_kw": 11.0,
        "battery_kwh": 75.0,
        "description": "Three-phase 11 kW home charger",
    },
    "level1_2kw": {
        "charge_rate_kw": 2.3,
        "battery_kwh": 40.0,
        "description": "Standard GPO trickle charge (10A)",
    },
}


CHARGING_MODES = ("uncontrolled", "offpeak", "smart")


def generate_ev_charging_profile(
    charge_rate_kw: float = 7.0,
    arrival_hour: float = 18.0,
    departure_hour: float = 7.0,
    # Evening plug-in SOC for a daily-driven EV: a ~0.7 -> 0.9 top-up on a 60 kWh
    # pack is ~12 kWh, matching ACN-Data session energy (~8-12 kWh) and typical
    # Australian daily driving — not the full 30%->90% recharge assumed before.
    initial_soc: float = 0.7,
    battery_kwh: float = 60.0,
    seed: int = 42,
    timesteps: int = TIMESTEPS,
    mode: str = "uncontrolled",
    offpeak_start_hour: float = 23.0,
) -> dict:
    """Generate an EV charging load profile over a 24-hour day.

    The EV arrives home at arrival_hour with initial_soc and must reach 0.9 SOC
    by departure_hour. The charging *strategy* is set by ``mode``:

      - "uncontrolled": charge at full rate the moment it plugs in — the
        evening-peak-stacking residential default.
      - "offpeak": a timer that defers full-rate charging to
        ``offpeak_start_hour`` (e.g. an off-peak tariff window).
      - "smart": spread the required energy evenly across the plugged-in window
        at the minimum rate that still reaches the target by departure,
        valley-filling the overnight trough.

    Returns:
        Dict with ev_charge_kw, ev_soc, energy_consumed_kwh, charge_duration_hours.
    """
    if mode not in CHARGING_MODES:
        raise ValueError(f"Unknown EV mode '{mode}'. Available: {list(CHARGING_MODES)}")

    hours = np.linspace(0, 24, timesteps, endpoint=False)
    step_hours = 24.0 / timesteps

    rng = np.random.default_rng(seed)
    actual_arrival = arrival_hour + rng.uniform(-30, 30) / 60.0

    target_soc = 0.9
    energy_needed_kwh = max(0.0, (target_soc - initial_soc) * battery_kwh)

    def _in_window(h: float) -> bool:
        if departure_hour < arrival_hour:
            # Overnight: arrival in evening, departure next morning.
            return h >= actual_arrival or h < departure_hour
        return actual_arrival <= h < departure_hour

    # Hours the EV is plugged in (arrival -> departure, wrapping past midnight).
    window_hours = (departure_hour - actual_arrival) % 24.0 or 24.0

    # Mode sets where charging starts and at what power.
    if mode == "smart":
        # Lowest constant power that still delivers the energy within the window.
        charge_power_kw = min(charge_rate_kw, energy_needed_kwh / window_hours)
        start_hour = actual_arrival
    elif mode == "offpeak":
        charge_power_kw = charge_rate_kw
        start_hour = offpeak_start_hour
    else:  # uncontrolled
        charge_power_kw = charge_rate_kw
        start_hour = actual_arrival

    start_idx = int(np.floor(start_hour / step_hours)) % timesteps

    ev_charge = np.zeros(timesteps)
    current_soc = initial_soc
    energy_consumed = 0.0
    charging_steps = 0

    for k in range(timesteps):
        i = (start_idx + k) % timesteps
        if not _in_window(hours[i]):
            continue
        if current_soc >= target_soc:
            break

        energy_this_step = charge_power_kw * step_hours
        remaining_energy = (target_soc - current_soc) * battery_kwh
        actual_energy = min(energy_this_step, remaining_energy)

        ev_charge[i] = actual_energy / step_hours
        current_soc += actual_energy / battery_kwh
        energy_consumed += actual_energy
        charging_steps += 1

    # SOC trace in *charging* order (walking from the charging start, wrapping
    # past midnight), not clock order — otherwise a session running past midnight
    # would show SOC rising in the early-morning hours before charging began.
    ev_soc = np.empty(timesteps)
    soc = initial_soc
    for k in range(timesteps):
        i = (start_idx + k) % timesteps
        soc += ev_charge[i] * step_hours / battery_kwh
        ev_soc[i] = soc

    charge_duration_hours = charging_steps * step_hours

    return {
        "ev_charge_kw": ev_charge,
        "ev_soc": ev_soc,
        "energy_consumed_kwh": energy_consumed,
        "charge_duration_hours": charge_duration_hours,
    }


def generate_diversified_ev_profile(
    config_name: str = "level2_7kw",
    n_evs: int = 32,
    seed: int = 42,
    arrival_hour: float = 18.0,
    departure_hour: float = 7.0,
    arrival_sigma_minutes: float = 90.0,
    timesteps: int = TIMESTEPS,
    mode: str = "uncontrolled",
    offpeak_start_hour: float = 23.0,
) -> dict:
    """Aggregate the EV charging demand of a bus serving several EVs.

    A bus does not have a single EV plugging in at exactly the same minute every
    day. This averages ``n_evs`` independent single-EV profiles whose plug-in
    times are drawn from Normal(``arrival_hour``, ``arrival_sigma_minutes``) and
    whose initial SOC varies between vehicles. The result is the *expected* EV
    demand per charger at the bus: the same daily energy as one EV, but with the
    sharp synchronous charging block smeared across the realistic arrival window
    (uncontrolled residential charging diversity).

    Deterministic in ``seed``.
    """
    if config_name not in EV_CONFIGS:
        raise ValueError(
            f"Unknown EV config '{config_name}'. "
            f"Available: {list(EV_CONFIGS.keys())}"
        )
    config = EV_CONFIGS[config_name]
    n = max(1, int(n_evs))
    rng = np.random.default_rng(seed)
    sigma_hours = arrival_sigma_minutes / 60.0

    charge_sum = np.zeros(timesteps)
    soc_sum = np.zeros(timesteps)
    energy_sum = 0.0

    for _ in range(n):
        arrival_i = arrival_hour + float(rng.normal(0.0, sigma_hours))
        # Per-EV evening plug-in SOC ~0.7 (daily top-up), spread across the fleet;
        # ACN-Data shows most sessions replenish only a partial charge.
        initial_soc_i = float(np.clip(rng.normal(0.7, 0.10), 0.5, 0.9))
        ev_seed = int(rng.integers(0, 2**31 - 1))
        res = generate_ev_charging_profile(
            charge_rate_kw=config["charge_rate_kw"],
            battery_kwh=config["battery_kwh"],
            arrival_hour=arrival_i,
            departure_hour=departure_hour,
            initial_soc=initial_soc_i,
            seed=ev_seed,
            timesteps=timesteps,
            mode=mode,
            offpeak_start_hour=offpeak_start_hour,
        )
        charge_sum += res["ev_charge_kw"]
        soc_sum += res["ev_soc"]
        energy_sum += res["energy_consumed_kwh"]

    return {
        "ev_charge_kw": charge_sum / n,
        "ev_soc": soc_sum / n,
        "energy_consumed_kwh": energy_sum / n,
        "charge_duration_hours": float(np.count_nonzero(charge_sum) * 24.0 / timesteps),
    }


def create_ev_profile(
    config_name: str = "level2_7kw", seed: int = 42, **kwargs
) -> dict:
    """Create an EV charging profile from a named configuration.

    Additional kwargs (including `timesteps`) are passed to
    generate_ev_charging_profile, overriding config defaults.
    """
    if config_name not in EV_CONFIGS:
        raise ValueError(
            f"Unknown EV config '{config_name}'. "
            f"Available: {list(EV_CONFIGS.keys())}"
        )
    config = EV_CONFIGS[config_name]
    params = {
        "charge_rate_kw": config["charge_rate_kw"],
        "battery_kwh": config["battery_kwh"],
        "seed": seed,
    }
    params.update(kwargs)
    return generate_ev_charging_profile(**params)
