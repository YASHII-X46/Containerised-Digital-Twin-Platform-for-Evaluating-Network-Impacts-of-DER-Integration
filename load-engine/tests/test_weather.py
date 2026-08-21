"""Tests for pluggable weather providers and the weather→PV coupling."""

import numpy as np

from app.profiles import weather
from app.profiles.solar_model import calculate_daily_yield_kwh, generate_solar_profile
from app.profiles.weather import (
    SyntheticProvider,
    available_weather_sources,
    temperature_trace,
    temperature_traces,
)


def test_available_sources():
    sources = available_weather_sources()
    assert sources[0] == "none"
    assert {"synthetic", "file"} <= set(sources)


def test_synthetic_diurnal_shape():
    trace = SyntheticProvider().hourly_temperature("summer")
    assert len(trace) == 24
    assert trace[15] > trace[5]  # afternoon warmer than pre-dawn


def test_none_source_disables_weather():
    assert temperature_trace("none", "summer", 96) is None


def test_synthetic_trace_length_matches_timesteps():
    trace = temperature_trace("synthetic", "summer", 48)
    assert trace is not None and len(trace) == 48


def test_failing_provider_falls_back_to_synthetic(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("file unreadable")

    monkeypatch.setattr(weather._PROVIDERS["file"], "hourly_temperature", boom)
    trace = temperature_trace("file", "summer", 96)
    assert trace is not None and len(trace) == 96  # degraded to synthetic, not a failure


def test_temperature_traces_per_day():
    traces = temperature_traces("synthetic", "summer", 3, 96)
    assert traces is not None and len(traces) == 3
    assert all(len(t) == 96 for t in traces)
    assert not np.allclose(traces[0], traces[1])  # day-to-day synoptic variation


def test_temperature_traces_none_disabled():
    assert temperature_traces("none", "summer", 3, 96) is None


def test_pv_derates_on_hot_day():
    hot = np.full(96, 40.0)
    base = generate_solar_profile(5.0, seed=1, season="summer")
    derated = generate_solar_profile(5.0, seed=1, season="summer", temperature=hot)
    assert calculate_daily_yield_kwh(derated) < calculate_daily_yield_kwh(base)


def _ghi_day(peak=900.0, n=24):
    """Simple measured GHI day: zero at night, sine bump over 07:00-17:00."""
    ghi = np.zeros(n)
    hours = np.arange(n)
    day = (hours >= 7) & (hours <= 17)
    ghi[day] = peak * np.sin(np.pi * (hours[day] - 7) / 10.0)
    return ghi


def test_file_provider_reads_temperature_and_irradiance(tmp_path, monkeypatch):
    from app.config import settings

    ghi = _ghi_day()
    lines = [f"{20 + h % 10},{ghi[h]}" for h in range(24)]
    path = tmp_path / "weather.csv"
    path.write_text("temp_C,ghi_Wm2\n" + "\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(settings, "WEATHER_FILE", str(path))

    traces = weather.weather_traces("file", "summer", 2, 96)
    assert traces["temperatures"] is not None and len(traces["temperatures"]) == 2
    assert traces["irradiance"] is not None and len(traces["irradiance"]) == 2
    assert all(len(t) == 96 for t in traces["irradiance"])
    assert traces["irradiance"][0].max() > 800.0    # peak survives resampling
    # A temperature-only file yields no irradiance but still supplies temps.
    path.write_text("\n".join(f"{20 + h % 10}" for h in range(24)), encoding="utf-8")
    traces = weather.weather_traces("file", "summer", 1, 96)
    assert traces["temperatures"] is not None
    assert traces["irradiance"] is None


def test_file_provider_missing_file_degrades_to_synthetic(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "WEATHER_FILE", "Z:/does/not/exist.csv")
    traces = weather.weather_traces("file", "summer", 1, 96)
    assert traces["temperatures"] is not None      # synthetic fallback
    assert traces["irradiance"] is None


def test_irradiance_drives_pv_directly():
    ghi = weather.resample_day(_ghi_day(), 96)
    profile = generate_solar_profile(5.0, seed=1, season="summer", irradiance=ghi)
    # Output follows the measured sky: zero at night, scaled at the peak.
    assert profile[0] == 0.0 and profile[-1] == 0.0
    expected_peak = 5.0 * 0.85 * min(ghi.max() / 1000.0, 1.2)
    assert abs(profile.max() - expected_peak) < 0.05
    # Deterministic (no synthetic clouds): different seeds give the same day.
    again = generate_solar_profile(5.0, seed=99, season="summer", irradiance=ghi)
    np.testing.assert_allclose(profile, again)


def test_weather_traces_none_source():
    traces = weather.weather_traces("none", "summer", 1, 96)
    assert traces == {"temperatures": None, "irradiance": None}
