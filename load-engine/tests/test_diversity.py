"""Tests for the load-diversity (household aggregation) model."""

import numpy as np

from app.profiles.diversity import diversified_shape, households_for_bus
from app.profiles.generator import ProfileGenerator
from tests.ieee33_data import default_bus_data


def _spiky_template(timesteps: int = 96) -> np.ndarray:
    """A single sharp evening-peak household template (peak = 1.0)."""
    t = np.arange(timesteps)
    peak_idx = int(timesteps * 18.5 / 24)  # ~18:30
    shape = np.exp(-0.5 * ((t - peak_idx) / 1.5) ** 2)
    return shape / shape.max()


def test_households_for_bus_scales_with_peak():
    assert households_for_bus(0.0, 1.5, 400) == 1
    assert households_for_bus(1.5, 1.5, 400) == 1
    assert households_for_bus(150.0, 1.5, 400) == 100
    # Capped at max_households.
    assert households_for_bus(10_000.0, 1.5, 400) == 400


def test_preserve_peak_keeps_unit_peak():
    template = _spiky_template()
    shape = diversified_shape(template, n_households=100, seed=7, preserve_peak=True)
    assert abs(shape.max() - 1.0) < 1e-12
    assert len(shape) == len(template)


def test_diversity_broadens_and_fills_valleys():
    template = _spiky_template()
    diversified = diversified_shape(
        template, n_households=200, seed=7, preserve_peak=True
    )
    # Aggregation lifts the near-zero overnight/midday valleys above the
    # essentially-zero template, i.e. demand never fully collapses.
    midday = slice(int(96 * 11 / 24), int(96 * 13 / 24))
    assert diversified[midday].mean() > template[midday].mean()
    # ...and the single sharp peak is spread over more timesteps.
    assert (diversified > 0.5).sum() > (template > 0.5).sum()


def test_diversity_is_deterministic_in_seed():
    template = _spiky_template()
    a = diversified_shape(template, n_households=120, seed=42)
    b = diversified_shape(template, n_households=120, seed=42)
    np.testing.assert_array_equal(a, b)
    c = diversified_shape(template, n_households=120, seed=43)
    assert not np.array_equal(a, c)


def test_feeder_coincidence_factor_below_one_with_diversity():
    """Across a whole feeder, diversified peaks no longer all line up."""
    bus_data = default_bus_data(der_penetration_percent=0)
    config = {
        "bus_data": bus_data,
        "seed": 42,
        "timesteps": 96,
        "resolution_minutes": 15,
        "season": "summer",
    }
    gen = ProfileGenerator(config)
    gen.generate_all_profiles()
    summary = gen.get_summary()
    assert summary["coincidence_factor"] < 1.0
    assert summary["coincidence_factor"] > 0.3  # sanity: not absurdly diverse


def test_disabled_diversity_gives_coincident_peaks():
    """With diversity off, identical archetypes peak together (factor ~ 1)."""
    bus_data = default_bus_data(der_penetration_percent=0)
    config = {
        "bus_data": bus_data,
        "seed": 42,
        "timesteps": 96,
        "resolution_minutes": 15,
        "season": "summer",
        "diversity": {"enabled": False},
    }
    gen = ProfileGenerator(config)
    gen.generate_all_profiles()
    summary = gen.get_summary()
    # Legacy behaviour: buses of the same archetype share an identical shape,
    # so the feeder peak is close to the sum of per-bus peaks.
    assert summary["coincidence_factor"] > 0.95


def test_preserve_peak_keeps_base_kw_as_bus_peak():
    """base_load_kw must stay the bus peak (relied on by the custom-profile path)."""
    bus_data = [
        {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0,
         "customer_class": "res_detached_small"},
        {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0,
         "customer_class": "res_detached_large"},
    ]
    gen = ProfileGenerator({
        "bus_data": bus_data, "seed": 42, "timesteps": 96,
        "resolution_minutes": 15, "season": "summer",
    })
    profiles = gen.generate_all_profiles()
    assert abs(float(np.max(profiles[2]["load_kw"])) - 100.0) < 1e-9
