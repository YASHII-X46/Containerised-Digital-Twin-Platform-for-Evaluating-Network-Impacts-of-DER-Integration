"""Pluggable weather providers — diurnal air-temperature and irradiance traces.

A provider returns a representative day of air temperature (and, when it can,
global horizontal irradiance) for a season. Weather drives PV generation
only: temperature sets the PV cell-temperature derating, and irradiance,
when available, drives PV output directly — replacing the synthetic cloud
model with the measured sky. Load shapes use their fixed season curves. Any
provider error falls back to the offline synthetic model, so a run never
fails.

  - synthetic : offline diurnal model from the season (default; provides no
                irradiance — the PV cloud model applies)
  - file      : local CSV of hourly rows "temp_C[,ghi_Wm2]" (WEATHER_FILE) —
                measured on-site data; fully reproducible runs
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

_HOURS_24 = np.arange(24)  # hour-of-day for the 24 hourly samples a provider gives


def resample_day(hourly: np.ndarray, n: int) -> np.ndarray:
    """Resample a 24-point hourly trace to n points across one day (wrap-aware)."""
    hourly = np.asarray(hourly, dtype=float)
    target_h = np.linspace(0, 24, n, endpoint=False)
    ext_x = np.append(_HOURS_24, 24.0)
    ext_y = np.append(hourly, hourly[0])
    return np.interp(target_h, ext_x, ext_y)


class WeatherProvider:
    name = "weather"

    def hourly_temperature(self, season: str) -> np.ndarray:
        """Return 24 hourly air temperatures (°C) for one representative day."""
        raise NotImplementedError

    def daily_hourly_temperature(self, season, days):
        """Return a list of `days` 24-point hourly traces. Default repeats one day;
        providers with multi-day data override this."""
        one = self.hourly_temperature(season)
        return [one for _ in range(days)]

    def hourly_irradiance(self, season: str):
        """24 hourly global horizontal irradiance values (W/m²), or None when
        this provider has no irradiance data (the PV cloud model applies)."""
        return None

    def daily_hourly_irradiance(self, season, days):
        """A list of `days` 24-point GHI traces, or None when unavailable."""
        one = self.hourly_irradiance(season)
        return None if one is None else [one for _ in range(days)]


class SyntheticProvider(WeatherProvider):
    """Offline diurnal model. Mean and swing depend on the season; minimum
    near 05:00, maximum near 15:00."""

    name = "synthetic"
    _SEASON = {"summer": (28.0, 8.0), "shoulder": (20.0, 6.0), "winter": (12.0, 5.0)}

    def hourly_temperature(self, season):
        mean, amp = self._SEASON.get(season, self._SEASON["shoulder"])
        phase = (_HOURS_24 - 15.0) / 24.0 * 2 * np.pi
        return mean + amp * np.cos(phase)

    def daily_hourly_temperature(self, season, days):
        # Slow synoptic swing across days (~5-day cycle, +/- 3 C) so each day differs.
        base = self.hourly_temperature(season)
        return [base + 3.0 * np.sin(2 * np.pi * d / 5.0) for d in range(days)]


class FileProvider(WeatherProvider):
    """Measured on-site weather from a local CSV (fully reproducible runs).

    ``WEATHER_FILE`` points at a CSV of hourly rows ``temp_C`` or
    ``temp_C,ghi_Wm2`` (an optional header line is skipped). Multi-day runs
    consume consecutive 24-row blocks, wrapping around when the file is
    shorter than the horizon. Irradiance is available only if the second
    column is present throughout.
    """

    name = "file"

    def _rows(self) -> list[list[float]]:
        from app.config import settings

        path = settings.WEATHER_FILE
        if not path:
            raise ValueError("WEATHER_FILE is not configured for the file weather source")
        rows: list[list[float]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                cells = [c.strip() for c in line.strip().split(",") if c.strip()]
                if not cells:
                    continue
                try:
                    rows.append([float(c) for c in cells])
                except ValueError:
                    continue  # header or comment line
        if len(rows) < 24:
            raise ValueError(f"weather file {path} needs at least 24 hourly rows")
        return rows

    def _day(self, rows, day: int, column: int) -> np.ndarray:
        out = []
        for h in range(24):
            row = rows[(day * 24 + h) % len(rows)]
            if column >= len(row):
                raise ValueError("weather file has no irradiance column")
            out.append(row[column])
        return np.array(out, dtype=float)

    def hourly_temperature(self, season):
        return self._day(self._rows(), 0, 0)

    def daily_hourly_temperature(self, season, days):
        rows = self._rows()
        return [self._day(rows, d, 0) for d in range(days)]

    def hourly_irradiance(self, season):
        rows = self._rows()
        if len(rows[0]) < 2:
            return None
        return self._day(rows, 0, 1)

    def daily_hourly_irradiance(self, season, days):
        rows = self._rows()
        if len(rows[0]) < 2:
            return None
        return [self._day(rows, d, 1) for d in range(days)]


_PROVIDERS: dict[str, WeatherProvider] = {}


def register_provider(provider: WeatherProvider) -> None:
    _PROVIDERS[provider.name] = provider


for _p in (SyntheticProvider(), FileProvider()):
    register_provider(_p)


def available_weather_sources() -> list[str]:
    """Selectable weather sources; 'none' uses the fixed season curves."""
    return ["none"] + list(_PROVIDERS)


def temperature_trace(source, season, timesteps) -> np.ndarray | None:
    """Return a `timesteps`-length temperature trace (°C), or None if disabled.

    A failed provider falls back to the offline synthetic model.
    """
    if not source or source == "none":
        return None
    provider = _PROVIDERS.get(source)
    if provider is None:
        return None
    try:
        hourly = provider.hourly_temperature(season)
    except Exception as exc:  # noqa: BLE001 — any failure degrades to synthetic
        logger.warning("Weather provider '%s' failed (%s); using synthetic.", source, exc)
        hourly = _PROVIDERS["synthetic"].hourly_temperature(season)
    return resample_day(np.asarray(hourly, dtype=float), timesteps)


def weather_traces(source, season, days, steps_per_day) -> dict:
    """Per-day temperature and irradiance traces (each steps_per_day long).

    Returns ``{"temperatures": list|None, "irradiance": list|None}``.
    Temperature degrades to the synthetic model on any provider failure;
    irradiance is all-or-nothing (None whenever the provider cannot supply a
    complete horizon), in which case the PV cloud model applies instead.
    """
    if not source or source == "none" or source not in _PROVIDERS:
        return {"temperatures": None, "irradiance": None}
    provider = _PROVIDERS[source]
    try:
        daily = provider.daily_hourly_temperature(season, days)
    except Exception as exc:  # noqa: BLE001 — any failure degrades to synthetic
        logger.warning("Weather provider '%s' failed (%s); using synthetic.", source, exc)
        daily = _PROVIDERS["synthetic"].daily_hourly_temperature(season, days)
    temperatures = [resample_day(np.asarray(h, dtype=float), steps_per_day) for h in daily]

    irradiance = None
    try:
        ghi_daily = provider.daily_hourly_irradiance(season, days)
        if ghi_daily is not None and all(g is not None for g in ghi_daily):
            irradiance = [
                np.clip(resample_day(np.asarray(g, dtype=float), steps_per_day), 0.0, None)
                for g in ghi_daily
            ]
    except Exception as exc:  # noqa: BLE001 — irradiance is best-effort
        logger.warning("Weather provider '%s' irradiance failed (%s); "
                       "PV keeps its cloud model.", source, exc)
    return {"temperatures": temperatures, "irradiance": irradiance}


def temperature_traces(source, season, days, steps_per_day):
    """Return a list of `days` per-day temperature traces (each steps_per_day long),
    or None if disabled. A failed provider degrades to synthetic."""
    return weather_traces(source, season, days, steps_per_day)["temperatures"]
