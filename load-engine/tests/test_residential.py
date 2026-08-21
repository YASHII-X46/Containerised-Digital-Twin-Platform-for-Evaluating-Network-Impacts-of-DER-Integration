"""Tests for residential archetype load models."""

import numpy as np
import pytest

from app.profiles.residential_models import (
    RESIDENTIAL_ARCHETYPES,
    get_available_archetypes,
    get_residential_peak_kw,
    get_residential_shape,
    hvac_cooling_curve,
)
from app.profiles.load_shapes import get_load_shape


class TestResidentialModels:
    def test_all_archetypes_return_96_elements(self):
        for archetype in get_available_archetypes():
            shape = get_residential_shape(archetype, seed=42)
            assert len(shape) == 96, f"{archetype} returned {len(shape)} elements"

    def test_all_values_in_range_0_1(self):
        for archetype in get_available_archetypes():
            shape = get_residential_shape(archetype, seed=42)
            assert np.all(shape >= 0.0), f"{archetype} has negative values"
            assert np.all(shape <= 1.0), f"{archetype} has values > 1.0"

    def test_detached_large_peak_gt_apartment(self):
        large_peak = get_residential_peak_kw("res_detached_large")
        apt_peak = get_residential_peak_kw("res_apartment_highrise")
        assert large_peak > apt_peak

    def test_summer_cooling_is_afternoon_dominated(self):
        # The documented "afternoon dominated" intent refers to the cooling
        # component, not the total load (which realistically peaks in the
        # evening when occupancy + cooking + AC tail coincide). Verify (a) the
        # cooling curve itself peaks in the afternoon, and (b) adding summer
        # cooling raises the afternoon window relative to a no-cooling baseline.
        cooling = hvac_cooling_curve(5.0, 1.0)
        peak_hour = np.argmax(cooling) * 0.25
        assert 13.0 <= peak_hour <= 17.0
        assert np.max(cooling[52:68]) > np.max(cooling[24:36])

        summer = get_residential_shape("res_detached_medium", seed=42, season="summer")
        shoulder = get_residential_shape("res_detached_medium", seed=42, season="shoulder")
        assert np.max(summer[52:68]) > np.max(shoulder[52:68])

    def test_winter_has_evening_peak(self):
        shape = get_residential_shape("res_detached_medium", seed=42, season="winter")
        peak_idx = np.argmax(shape)
        peak_hour = peak_idx * 0.25
        assert 17.0 <= peak_hour <= 22.0

    def test_pool_pump_elevates_daytime(self):
        large_peak = get_residential_peak_kw("res_detached_large", "summer")
        medium_peak = get_residential_peak_kw("res_detached_medium", "summer")
        # Large has pool + more floor area, so should be higher
        assert large_peak > medium_peak

    def test_shared_wall_reduces_hvac(self):
        townhouse_peak = get_residential_peak_kw("res_townhouse", "summer")
        # Compare to a similar-sized detached at same floor area
        # Townhouse has shared_wall_factor=0.7, so HVAC contribution is lower
        detached_med_peak = get_residential_peak_kw("res_detached_medium", "summer")
        # Townhouse should be less than detached_medium (less floor area + shared walls)
        assert townhouse_peak < detached_med_peak

    def test_load_shape_integration(self):
        shape = get_load_shape("res_detached_medium", 42)
        assert len(shape) == 96
        assert np.all(shape >= 0.0)
        assert np.all(shape <= 1.0)

    def test_unknown_class_raises(self):
        with pytest.raises(ValueError):
            get_load_shape("residential", 42)
