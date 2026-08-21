"""Tests for the pluggable building-archetype registry (Australian residential
+ campus commercial)."""

import numpy as np

from app.profiles import archetypes
from app.profiles.archetypes import (
    archetypes_in_category,
    available_archetypes,
    is_archetype,
    register_archetype,
)
from app.profiles.load_shapes import (
    AVAILABLE_CLASSES,
    get_available_classes,
    get_load_shape,
)


def test_registry_has_residential_and_commercial():
    names = available_archetypes()
    assert "res_detached_medium" in names
    assert "com_small_office" in names
    assert "com_education" in names


def test_categories():
    assert "com_education" in archetypes_in_category("commercial")
    assert set(archetypes_in_category("residential")) >= {"res_detached_small", "res_townhouse"}


def test_auto_assign_classes_are_residential_only():
    # AVAILABLE_CLASSES drives the auto-assigner's rotation — keep it homes-only.
    assert AVAILABLE_CLASSES and all(c.startswith("res_") for c in AVAILABLE_CLASSES)
    assert "com_small_office" not in AVAILABLE_CLASSES


def test_commercial_classes_selectable_per_bus():
    classes = get_available_classes()
    assert "com_medium_office" in classes
    assert "com_education" in classes


def test_commercial_shapes_valid_and_normalised():
    for cls in archetypes_in_category("commercial"):
        for season in ("summer", "winter", "shoulder"):
            shape = get_load_shape(cls, seed=7, season=season)
            assert len(shape) == 96
            assert np.all(shape >= 0.0) and np.all(shape <= 1.0)
            assert abs(shape.max() - 1.0) < 1e-6  # normalised to peak


def test_education_runs_into_the_evening():
    # Evening classes keep a teaching building loaded well past office close.
    from app.profiles.archetypes import get_archetype

    edu = get_archetype("com_education").build("summer")
    office = get_archetype("com_small_office").build("summer")
    evening = slice(76, 84)  # 19:00-21:00
    assert edu[evening].mean() / edu.max() > office[evening].mean() / office.max()


def test_office_near_empty_on_weekend():
    from app.profiles.archetypes import get_archetype

    office = get_archetype("com_small_office")
    weekday = office.build("summer", day_type="weekday")
    weekend = office.build("summer", day_type="weekend")
    # Midday (10:00-17:00, steps 40-68) collapses on weekends.
    assert weekend[40:68].mean() < 0.5 * weekday[40:68].mean()


def test_residential_weekday_weekend_differ():
    from app.profiles.archetypes import get_archetype

    home = get_archetype("res_detached_medium")
    assert not np.allclose(
        home.build("summer", day_type="weekday"),
        home.build("summer", day_type="weekend"),
    )


def test_custom_archetype_is_picked_up():
    """Registering a new archetype makes it a selectable customer class."""
    saved = dict(archetypes._REGISTRY)
    try:
        register_archetype(
            "com_data_centre", "commercial",
            lambda season, day_type="weekday": np.full(96, 5.0),
        )
        assert is_archetype("com_data_centre")
        assert "com_data_centre" in get_available_classes()
        shape = get_load_shape("com_data_centre", seed=3)
        assert len(shape) == 96 and np.all(shape <= 1.0)
    finally:
        archetypes._REGISTRY.clear()
        archetypes._REGISTRY.update(saved)
