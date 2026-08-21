"""Normalised 24-hour per-unit load shapes — archetypes + custom profiles.

A bus's `customer_class` selects its shape:
  - any registered building archetype (residential or commercial),
  - or "custom:<name>" referencing a user-uploaded profile (CustomProfileStore).

Archetypes come from the pluggable registry (``archetypes``); importing the
residential and commercial model modules registers the built-ins. All shapes are
resampled to the requested timestep count, so any resolution works for every
profile source.
"""

import numpy as np

from app.profiles.archetypes import (
    archetypes_in_category,
    available_archetypes,
    get_archetype,
    is_archetype,
)
from app.profiles.custom import CUSTOM_PREFIX, CustomProfileStore, resample_shape

# Importing these modules registers their archetypes in the shared registry.
import app.profiles.residential_models  # noqa: F401,E402
import app.profiles.commercial_models  # noqa: F401,E402

# Residential classes only — the auto-assigner rotates through these so quick
# mode keeps assigning homes across a distribution feeder. The manual per-bus
# editor can pick any registered archetype (see get_available_classes()).
AVAILABLE_CLASSES = archetypes_in_category("residential")


def get_load_shape(
    customer_class: str,
    seed: int,
    season: str = "summer",
    timesteps: int = 96,
    custom_store: CustomProfileStore | None = None,
    day_type: str = "weekday",
) -> np.ndarray:
    """Return a per-unit load shape with `timesteps` points.

    Args:
        customer_class: A registered archetype name, or "custom:<name>".
        seed: RNG seed for reproducible noise (archetype shapes only).
        season: Season selection ("summer", "winter", "shoulder").
        timesteps: Number of points in the returned shape.
        custom_store: Where to resolve "custom:" profiles from.

    Returns:
        numpy array of length `timesteps` with values in [0.0, 1.0].

    Raises:
        ValueError: If customer_class is not recognised.
    """
    if customer_class.startswith(CUSTOM_PREFIX):
        if custom_store is None:
            raise ValueError(
                f"Bus uses '{customer_class}' but no custom profile store is configured."
            )
        name = customer_class[len(CUSTOM_PREFIX):]
        return custom_store.get_shape(name, timesteps, kind="load")

    if not is_archetype(customer_class):
        raise ValueError(
            f"Unknown customer class '{customer_class}'. "
            f"Available: {available_archetypes()} or 'custom:<name>'."
        )

    # Build the absolute archetype shape, add small reproducible noise, and
    # normalise to per-unit of peak before resampling to the requested step count.
    abs_shape = get_archetype(customer_class).build(season, day_type)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.03, len(abs_shape))
    shape = abs_shape + noise * abs_shape.max()
    shape = shape / shape.max()
    shape = np.clip(shape, 0.0, 1.0)
    return np.clip(resample_shape(shape, timesteps), 0.0, 1.0)


def get_available_classes() -> list[str]:
    """All selectable customer classes (residential + commercial)."""
    return available_archetypes()
