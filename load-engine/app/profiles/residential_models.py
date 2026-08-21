"""Australian residential archetype load models.

Six archetypes representing Australian housing stock (detached homes,
townhouses, apartments). Each 96-point (15-min resolution) shape is built by
summing physically-motivated component curves whose timing (morning shoulder,
evening peak) is consistent with the daily demand shape published by
Australian DNSPs and retailers nationally.

The shapes are normalised; absolute bus demand is set from the network base loads
and the after-diversity maximum demand (see DiversityConfig.admd_kw). Per-dwelling
daily energy spans the national range — roughly 12-13 kWh/day in VIC/SA up to
20+ kWh/day in TAS/NT/QLD, national average ~15 kWh/day — and is matched by the
base loads of the chosen network rather than hardcoded here.

The HVAC component is a temperate-Australia design-day representation; strongly
climate-driven regions (tropical QLD/NT, arid inland) are better matched by
selecting the `season` or supplying a custom profile / explicit bus_data.
Weather traces do not enter load shapes — weather drives PV generation only.
"""

import numpy as np

from app.profiles.archetypes import register_archetype

TIMESTEPS = 96
HOURS = np.linspace(0, 24, TIMESTEPS, endpoint=False)


# ---------------------------------------------------------------------------
# Component curve functions
# ---------------------------------------------------------------------------

def _gaussian(center_h: float, sigma_h: float, peak_kw: float) -> np.ndarray:
    """Gaussian pulse centred at center_h with given sigma and peak."""
    return peak_kw * np.exp(-0.5 * ((HOURS - center_h) / sigma_h) ** 2)


def _rectangular(start_h: float, end_h: float, level_kw: float) -> np.ndarray:
    """Rectangular pulse between start_h and end_h."""
    mask = (HOURS >= start_h) & (HOURS < end_h)
    return np.where(mask, level_kw, 0.0)


def base_appliance_curve(floor_area_m2: float, n_occupants: int, watt_per_m2: float = 5.0,
                         day_type: str = "weekday") -> np.ndarray:
    """Continuous appliance load with occupancy-modulated lighting.

    On weekends occupancy starts later and stays higher through the middle of the
    day (people home rather than at work/school).
    """
    fridge = np.full(TIMESTEPS, 0.15)
    standby = np.full(TIMESTEPS, 0.05 * n_occupants)

    occupancy = np.zeros(TIMESTEPS)
    for i, h in enumerate(HOURS):
        if day_type == "weekend":
            if 8.0 <= h < 10.0:
                occupancy[i] = 0.40 * (h - 8.0) / 2.0
            elif 10.0 <= h < 17.0:
                occupancy[i] = 0.40
            elif 17.0 <= h < 19.0:
                occupancy[i] = 0.40 + 0.60 * (h - 17.0) / 2.0
            elif 19.0 <= h < 23.0:
                occupancy[i] = 1.0
            elif 23.0 <= h < 24.0:
                occupancy[i] = 1.0 - 0.85 * (h - 23.0) / 1.0
            else:
                occupancy[i] = 0.05
        else:
            if 6.0 <= h < 8.0:
                occupancy[i] = 0.3 * (h - 6.0) / 2.0
            elif 8.0 <= h < 16.0:
                occupancy[i] = 0.10
            elif 16.0 <= h < 17.5:
                occupancy[i] = 0.10 + 0.70 * (h - 16.0) / 1.5
            elif 17.5 <= h < 22.5:
                occupancy[i] = 1.0
            elif 22.5 <= h < 23.5:
                occupancy[i] = 1.0 - 0.85 * (h - 22.5) / 1.0
            else:
                occupancy[i] = 0.05

    active_kw = (floor_area_m2 * watt_per_m2 / 1000.0) * occupancy
    return fridge + standby + active_kw


def cooking_curve(n_occupants: int, day_type: str = "weekday") -> np.ndarray:
    """Electric cooktop + oven peaks. Weekends add a later breakfast and a lunch."""
    if day_type == "weekend":
        morning = _gaussian(8.5, 0.6, 0.35 * n_occupants)
        lunch = _gaussian(12.5, 0.5, 0.40 * n_occupants)
        evening = _gaussian(18.5, 0.6, 0.8 * n_occupants)
        return morning + lunch + evening
    morning = _gaussian(7.0, 0.4, 0.3 * n_occupants)
    evening = _gaussian(18.5, 0.5, 0.8 * n_occupants)
    return morning + evening


def hot_water_curve(tank_element_kw: float, n_occupants: int) -> np.ndarray:
    """Electric storage or heat pump hot water system."""
    scale = min(n_occupants / 3.0, 1.0)
    morning = _gaussian(6.5, 0.5, tank_element_kw * 0.8) * scale
    evening = _gaussian(19.0, 0.5, tank_element_kw * 0.4) * scale
    overnight = _rectangular(1.0, 5.0, tank_element_kw * 0.1) * scale
    return morning + evening + overnight


def hvac_cooling_curve(capacity_kw: float, shared_wall_factor: float = 1.0) -> np.ndarray:
    """Reverse-cycle AC cooling on a south-eastern Australian summer design day (~35-38C).

    Afternoon-peaked with a slower evening decay (AC keeps running into the
    evening). Built as a single asymmetric gaussian so the curve is continuous
    and tapers to ~0 overnight on its own. The previous hard 08:00/23:00
    cut-offs were applied to a curve that was already near-zero there, which
    only introduced small step discontinuities.
    """
    sigma = np.where(HOURS <= 15.0, 2.5, 3.5)
    profile = capacity_kw * 0.7 * np.exp(-0.5 * ((HOURS - 15.0) / sigma) ** 2)
    return profile * shared_wall_factor


def hvac_heating_curve(capacity_kw: float, shared_wall_factor: float = 1.0) -> np.ndarray:
    """Reverse-cycle AC heating on a south-eastern Australian winter day (~7-8C).

    Smooth morning warm-up + evening peak. The two gaussians already taper to
    ~0 midday and overnight, so no hard mask is needed. The previous
    rectangular mask chopped the gaussians mid-rise and produced non-physical
    step discontinuities in the winter profile (0 -> 0.8 kW at 05:30 and
    0 -> 0.4 kW at 16:00); removing it makes the warm-up and evening ramps
    continuous.
    """
    morning = _gaussian(7.0, 1.0, capacity_kw * 0.5)
    evening = _gaussian(19.0, 1.5, capacity_kw * 0.6)
    return (morning + evening) * shared_wall_factor


def pool_pump_curve(pump_kw: float = 1.5) -> np.ndarray:
    """Timer-controlled pool pump."""
    return _rectangular(9.0, 17.0, pump_kw)


# ---------------------------------------------------------------------------
# Archetype registry
# ---------------------------------------------------------------------------

RESIDENTIAL_ARCHETYPES: dict[str, dict] = {
    "res_detached_small": {
        "floor_area_m2": 120, "occupants": 2,
        "hvac_cooling_kw": 2.5, "hvac_heating_kw": 2.5,
        "hw_tank_kw": 3.6, "watt_per_m2": 5.0,
        "shared_wall_factor": 1.0,
        "has_pool": False, "pool_pump_kw": 0.0,
    },
    "res_detached_medium": {
        "floor_area_m2": 180, "occupants": 3,
        "hvac_cooling_kw": 5.0, "hvac_heating_kw": 5.0,
        "hw_tank_kw": 4.8, "watt_per_m2": 5.0,
        "shared_wall_factor": 1.0,
        "has_pool": False, "pool_pump_kw": 0.0,
    },
    "res_detached_large": {
        "floor_area_m2": 250, "occupants": 4,
        "hvac_cooling_kw": 8.0, "hvac_heating_kw": 8.0,
        "hw_tank_kw": 1.5, "watt_per_m2": 5.5,
        "shared_wall_factor": 1.0,
        "has_pool": True, "pool_pump_kw": 1.5,
    },
    "res_townhouse": {
        "floor_area_m2": 140, "occupants": 2,
        "hvac_cooling_kw": 3.5, "hvac_heating_kw": 3.5,
        "hw_tank_kw": 3.6, "watt_per_m2": 5.0,
        "shared_wall_factor": 0.7,
        "has_pool": False, "pool_pump_kw": 0.0,
    },
    "res_apartment_lowrise": {
        "floor_area_m2": 75, "occupants": 2,
        "hvac_cooling_kw": 2.0, "hvac_heating_kw": 2.0,
        "hw_tank_kw": 2.4, "watt_per_m2": 5.0,
        "shared_wall_factor": 0.6,
        "has_pool": False, "pool_pump_kw": 0.0,
    },
    "res_apartment_highrise": {
        "floor_area_m2": 65, "occupants": 1,
        "hvac_cooling_kw": 1.8, "hvac_heating_kw": 1.8,
        "hw_tank_kw": 2.4, "watt_per_m2": 4.5,
        "shared_wall_factor": 0.5,
        "has_pool": False, "pool_pump_kw": 0.0,
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_absolute_shape(
    archetype: str, season: str = "summer", day_type: str = "weekday",
) -> np.ndarray:
    """Build a 96-point absolute kW shape (not normalised).

    HVAC uses the fixed season design-day curves. ``day_type`` shifts the
    occupancy-driven appliance and cooking loads on weekends.
    """
    if archetype not in RESIDENTIAL_ARCHETYPES:
        raise ValueError(
            f"Unknown archetype '{archetype}'. "
            f"Available: {list(RESIDENTIAL_ARCHETYPES.keys())}"
        )

    p = RESIDENTIAL_ARCHETYPES[archetype]

    shape = (
        base_appliance_curve(p["floor_area_m2"], p["occupants"], p["watt_per_m2"], day_type)
        + cooking_curve(p["occupants"], day_type)
        + hot_water_curve(p["hw_tank_kw"], p["occupants"])
    )

    if season == "summer":
        shape += hvac_cooling_curve(p["hvac_cooling_kw"], p["shared_wall_factor"])
    elif season == "winter":
        shape += hvac_heating_curve(p["hvac_heating_kw"], p["shared_wall_factor"])
    elif season == "shoulder":
        shape += 0.3 * hvac_cooling_curve(p["hvac_cooling_kw"], p["shared_wall_factor"])

    if p["has_pool"]:
        shape += pool_pump_curve(p["pool_pump_kw"])

    return shape


def get_residential_shape(archetype: str, seed: int, season: str = "summer") -> np.ndarray:
    """Build a 96-point per-unit load shape for the given archetype.

    Returns a normalised [0, 1] shape with small Gaussian noise added.
    """
    shape = _build_absolute_shape(archetype, season)

    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.03, TIMESTEPS)
    shape = shape + noise * shape.max()

    shape = shape / shape.max()
    return np.clip(shape, 0.0, 1.0)


def get_residential_peak_kw(archetype: str, season: str = "summer") -> float:
    """Return the un-normalised peak demand (kW) for sizing base_load_kw."""
    shape = _build_absolute_shape(archetype, season)
    return float(np.max(shape))


def get_available_archetypes() -> list[str]:
    return list(RESIDENTIAL_ARCHETYPES.keys())


# ---------------------------------------------------------------------------
# Register the residential archetypes in the shared archetype registry, so they
# are selectable customer classes alongside the commercial ones.
# ---------------------------------------------------------------------------


def _residential_builder(name: str):
    return lambda season, day_type="weekday": _build_absolute_shape(
        name, season, day_type
    )


for _name in RESIDENTIAL_ARCHETYPES:
    register_archetype(_name, "residential", _residential_builder(_name))
