"""Solar PV generation profile model for Australian rooftop PV.

The day's effective PV window and peak are derived from a representative solar
declination for the season at a fixed Australian reference geometry, so winter
produces a shorter, lower day than summer. Magnitudes are calibrated to
Australian rooftop-PV field data (APVI): a performance ratio of ~0.80 and a
mean cloud derate of ~10% give daily yields in the nationally observed
~3.6-5 kWh/kWp range (national mean ~4).
"""

from math import acos, degrees, radians, sin, tan

import numpy as np

TIMESTEPS = 96  # default daily resolution (15-min)
PERFORMANCE_RATIO = 0.80
SOLAR_NOON = 12.0
# Cloud intermittency as an AR(1) process: clouds persist between timesteps
# (passing fronts) rather than flickering independently each step. CLOUD_RHO is
# the step-to-step persistence; CLOUD_SIGMA the stationary std of the cloud
# index. Only positive excursions attenuate, so clear stretches alternate with
# autocorrelated cloudy stretches (mean derate ~10%).
CLOUD_RHO = 0.85
CLOUD_SIGMA = 0.25
CLOUD_MAX_ATTENUATION = 0.8
# Effective PV generation window for an Australian *summer* day. Narrower than
# the astronomical day length because low morning/evening sun yields little;
# winter and shoulder are scaled off this baseline using the sunrise-equation
# geometry at the reference latitude below.
SUMMER_DAYLIGHT_HOURS = 10.5

# Representative solar declination (degrees) per season, southern hemisphere
# (summer sun is overhead the southern tropics -> negative declination).
_SEASON_DECLINATION = {"summer": -21.0, "shoulder": 0.0, "winter": 21.0}

# Fixed reference latitude for the seasonal geometry: the population-weighted
# Australian mean (Sydney/Melbourne/Brisbane/Perth/Adelaide).
_REFERENCE_LATITUDE = -34.0


def _astronomical_day_length(latitude_deg: float, declination_deg: float) -> float:
    """Daylight length (hours) from latitude and declination (sunrise equation)."""
    cos_omega = -tan(radians(latitude_deg)) * tan(radians(declination_deg))
    cos_omega = max(-1.0, min(1.0, cos_omega))
    return 2.0 * degrees(acos(cos_omega)) / 15.0


def _noon_elevation_sin(latitude_deg: float, declination_deg: float) -> float:
    """sin(solar elevation) at solar noon — proportional to peak irradiance."""
    elevation_deg = 90.0 - abs(latitude_deg - declination_deg)
    return max(0.0, sin(radians(elevation_deg)))


def _seasonal_factors(season: str) -> tuple[float, float]:
    """Return (effective day length hours, peak factor relative to summer)."""
    declination = _SEASON_DECLINATION.get(season, _SEASON_DECLINATION["summer"])
    summer_declination = _SEASON_DECLINATION["summer"]

    day_ratio = (
        _astronomical_day_length(_REFERENCE_LATITUDE, declination)
        / _astronomical_day_length(_REFERENCE_LATITUDE, summer_declination)
    )
    peak_rel = (
        _noon_elevation_sin(_REFERENCE_LATITUDE, declination)
        / _noon_elevation_sin(_REFERENCE_LATITUDE, summer_declination)
    )
    return SUMMER_DAYLIGHT_HOURS * day_ratio, peak_rel


# Crystalline-silicon temperature coefficient of power (~ -0.4 %/°C) and the
# cell-temperature rise at full irradiance over ambient (NOCT-style).
PV_TEMP_COEFF = 0.004
PV_CELL_RISE = 25.0


# Reference irradiance (W/m²) at which a PV array delivers nameplate DC power,
# and the system (wiring/inverter/soiling) derate applied to measured-GHI runs.
# The measured sky already carries the clouds, so no synthetic attenuation is
# added on that path.
STC_IRRADIANCE_WM2 = 1000.0
IRRADIANCE_SYSTEM_DERATE = 0.85


def generate_solar_profile(
    capacity_kw: float,
    seed: int,
    season: str = "summer",
    timesteps: int = TIMESTEPS,
    temperature: np.ndarray | None = None,
    irradiance: np.ndarray | None = None,
) -> np.ndarray:
    """Generate a solar PV generation profile for a single day.

    Args:
        capacity_kw: Nameplate PV capacity in kW.
        seed: RNG seed for cloud intermittency noise (unused when a measured
            irradiance trace drives the day).
        season: "summer", "winter" or "shoulder" — selects the representative
            solar declination (shorter, lower days in winter).
        timesteps: Number of points across the 24-hour day.
        temperature: Optional air-temperature trace (°C). When given, hot cells
            derate the output (lower yield on hot afternoons).
        irradiance: Optional global horizontal irradiance trace (W/m²) from a
            weather provider. When given, it drives the output directly —
            replacing the clear-sky curve and the synthetic cloud model with
            the measured/forecast sky — with temperature derating still applied.

    Returns:
        numpy array of length `timesteps` with generation values in kW.
    """
    if capacity_kw == 0:
        return np.zeros(timesteps)

    if irradiance is not None:
        ghi = np.asarray(irradiance, dtype=float)
        if len(ghi) != timesteps:
            src = np.linspace(0, 24, len(ghi), endpoint=False)
            tgt = np.linspace(0, 24, timesteps, endpoint=False)
            ghi = np.interp(tgt, np.append(src, 24.0), np.append(ghi, ghi[0]))
        irradiance_frac = np.clip(ghi / STC_IRRADIANCE_WM2, 0.0, 1.2)
        profile = capacity_kw * IRRADIANCE_SYSTEM_DERATE * irradiance_frac
        if temperature is not None:
            profile = profile * _temperature_derate(
                temperature, np.clip(irradiance_frac, 0.0, 1.0), timesteps
            )
        return np.clip(profile, 0, capacity_kw)

    hours = np.linspace(0, 24, timesteps, endpoint=False)
    day_length, peak_rel = _seasonal_factors(season)

    cos_factor = np.maximum(
        0, np.cos(np.pi * (hours - SOLAR_NOON) / day_length)
    )

    profile = capacity_kw * PERFORMANCE_RATIO * peak_rel * cos_factor

    sunrise, sunset = SOLAR_NOON - day_length / 2.0, SOLAR_NOON + day_length / 2.0
    profile[(hours < sunrise) | (hours > sunset)] = 0.0

    profile = profile * (1 - _cloud_attenuation(seed, timesteps))

    if temperature is not None:
        profile = profile * _temperature_derate(temperature, cos_factor, timesteps)

    return np.clip(profile, 0, capacity_kw)


def _temperature_derate(temperature: np.ndarray, irradiance_frac: np.ndarray, timesteps: int) -> np.ndarray:
    """Per-timestep PV derate factor (<=1) from ambient temperature.

    Cell temperature rises above ambient with irradiance; output falls ~0.4 %
    per °C above the 25 °C rating.
    """
    temp = np.asarray(temperature, dtype=float)
    if len(temp) != timesteps:
        src = np.linspace(0, 24, len(temp), endpoint=False)
        tgt = np.linspace(0, 24, timesteps, endpoint=False)
        ext_x = np.append(src, 24.0)
        ext_y = np.append(temp, temp[0])
        temp = np.interp(tgt, ext_x, ext_y)
    cell_temp = temp + PV_CELL_RISE * irradiance_frac
    return np.clip(1.0 - PV_TEMP_COEFF * np.maximum(0.0, cell_temp - 25.0), 0.5, 1.0)


def _cloud_attenuation(seed: int, timesteps: int) -> np.ndarray:
    """AR(1) cloud attenuation series in [0, CLOUD_MAX_ATTENUATION].

    Clouds persist across timesteps; negative excursions of the cloud index map
    to clear sky (no attenuation), positive ones to autocorrelated cloudy spells.
    """
    rng = np.random.default_rng(seed)
    innovation_std = CLOUD_SIGMA * np.sqrt(1.0 - CLOUD_RHO**2)
    innovations = rng.normal(0.0, innovation_std, timesteps)
    cloud_index = np.zeros(timesteps)
    for t in range(1, timesteps):
        cloud_index[t] = CLOUD_RHO * cloud_index[t - 1] + innovations[t]
    return np.clip(cloud_index, 0.0, CLOUD_MAX_ATTENUATION)


def get_daylight_hours(season: str = "summer") -> tuple[float, float]:
    """Return the (sunrise, sunset) hours of the effective PV window."""
    day_length, _ = _seasonal_factors(season)
    return (SOLAR_NOON - day_length / 2.0, SOLAR_NOON + day_length / 2.0)


def calculate_daily_yield_kwh(profile: np.ndarray, step_hours: float = 0.25) -> float:
    """Integrate a daily profile to get energy yield in kWh."""
    return float(np.sum(profile) * step_hours)
