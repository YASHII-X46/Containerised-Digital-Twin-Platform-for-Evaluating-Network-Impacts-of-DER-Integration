"""Tests for load shape generation."""

import numpy as np
import pytest

from app.profiles.load_shapes import get_available_classes, get_load_shape


class TestLoadShapes:
    def test_all_classes_produce_96_points(self):
        for cls in get_available_classes():
            shape = get_load_shape(cls, seed=42)
            assert len(shape) == 96, f"{cls} returned {len(shape)}"

    def test_values_in_range(self):
        for cls in get_available_classes():
            shape = get_load_shape(cls, seed=42)
            assert np.all(shape >= 0.0)
            assert np.all(shape <= 1.0)

    def test_determinism(self):
        s1 = get_load_shape("res_detached_medium", seed=42)
        s2 = get_load_shape("res_detached_medium", seed=42)
        np.testing.assert_array_equal(s1, s2)

    def test_different_seeds(self):
        s1 = get_load_shape("res_detached_medium", seed=1)
        s2 = get_load_shape("res_detached_medium", seed=99)
        assert not np.array_equal(s1, s2)

    def test_unknown_class_raises(self):
        with pytest.raises(ValueError):
            get_load_shape("unknown_class", seed=42)

    def test_archetype_classes_available(self):
        classes = get_available_classes()
        assert "res_detached_small" in classes
        assert "res_apartment_highrise" in classes
