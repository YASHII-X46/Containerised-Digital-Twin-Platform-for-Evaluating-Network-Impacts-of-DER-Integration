"""Tests for solar profile generation."""

import numpy as np
import pytest

from app.profiles.solar_model import (
    calculate_daily_yield_kwh,
    generate_solar_profile,
    get_daylight_hours,
)


class TestSolarModel:
    def test_profile_length(self):
        profile = generate_solar_profile(5.0, seed=42)
        assert len(profile) == 96

    def test_zero_capacity(self):
        profile = generate_solar_profile(0.0, seed=42)
        assert np.all(profile == 0)

    def test_no_negative_generation(self):
        profile = generate_solar_profile(5.0, seed=42)
        assert np.all(profile >= 0)

    def test_max_within_capacity(self):
        cap = 6.5
        profile = generate_solar_profile(cap, seed=42)
        assert np.max(profile) <= cap

    def test_zero_at_night(self):
        timesteps = 96
        profile = generate_solar_profile(5.0, seed=42, timesteps=timesteps)
        sunrise_h, sunset_h = get_daylight_hours()
        hours = np.linspace(0, 24, timesteps, endpoint=False)
        night = (hours < sunrise_h) | (hours > sunset_h)
        assert np.all(profile[night] == 0)

    def test_other_resolutions(self):
        for timesteps in (24, 48, 288):
            profile = generate_solar_profile(5.0, seed=42, timesteps=timesteps)
            assert len(profile) == timesteps
            assert np.all(profile >= 0)
            assert np.max(profile) > 0

    def test_daily_yield_reasonable(self):
        profile = generate_solar_profile(5.0, seed=42)
        yield_kwh = calculate_daily_yield_kwh(profile)
        assert 10 < yield_kwh < 40

    def test_determinism(self):
        p1 = generate_solar_profile(5.0, seed=42)
        p2 = generate_solar_profile(5.0, seed=42)
        np.testing.assert_array_equal(p1, p2)

    def test_winter_yields_less_than_summer(self):
        summer = calculate_daily_yield_kwh(
            generate_solar_profile(5.0, seed=42, season="summer")
        )
        shoulder = calculate_daily_yield_kwh(
            generate_solar_profile(5.0, seed=42, season="shoulder")
        )
        winter = calculate_daily_yield_kwh(
            generate_solar_profile(5.0, seed=42, season="winter")
        )
        # Shorter, lower-sun winter day -> meaningfully less energy than summer.
        assert winter < shoulder < summer
        assert winter < 0.6 * summer

    def test_winter_window_is_shorter(self):
        s_sunrise, s_sunset = get_daylight_hours("summer")
        w_sunrise, w_sunset = get_daylight_hours("winter")
        assert (w_sunset - w_sunrise) < (s_sunset - s_sunrise)
        # Winter sun rises later and sets earlier.
        assert w_sunrise > s_sunrise and w_sunset < s_sunset

    def test_summer_is_unchanged_baseline(self):
        # Summer must remain the original calibration.
        assert get_daylight_hours("summer") == (6.75, 17.25)
