"""Pluggable building load-archetype registry.

An archetype builds a 96-point absolute-kW day-shape for a season, in an
Australian context. Residential archetypes are component-curve Australian
homes; commercial archetypes are occupancy-driven office and education
buildings with season design-day HVAC. Registering an archetype makes it a
selectable ``customer_class`` everywhere with no edits to the generator — the
same plug-and-play pattern as the DER plugins.
"""

import numpy as np

TIMESTEPS = 96
HOURS = np.linspace(0, 24, TIMESTEPS, endpoint=False)


def gaussian(center_h: float, sigma_h: float, peak: float) -> np.ndarray:
    """Gaussian pulse centred at center_h with the given sigma and peak."""
    return peak * np.exp(-0.5 * ((HOURS - center_h) / sigma_h) ** 2)


def rectangular(start_h: float, end_h: float, level: float) -> np.ndarray:
    """Rectangular pulse between start_h and end_h."""
    return np.where((HOURS >= start_h) & (HOURS < end_h), level, 0.0)


def occupancy_window(start_h: float, end_h: float, ramp_h: float = 1.0) -> np.ndarray:
    """A 0..1 occupancy profile that ramps up at start_h and down at end_h."""
    up = np.clip((HOURS - start_h) / ramp_h, 0.0, 1.0)
    down = np.clip((end_h - HOURS) / ramp_h, 0.0, 1.0)
    return np.clip(np.minimum(up, down), 0.0, 1.0)


class Archetype:
    """One building load archetype: a season -> absolute-kW day-shape builder."""

    def __init__(self, name: str, category: str, builder):
        self.name = name
        self.category = category
        self._builder = builder

    def build(self, season: str = "summer", day_type: str = "weekday") -> np.ndarray:
        """Absolute-kW 96-point day-shape (deterministic; noise added downstream).

        ``day_type`` is "weekday" or "weekend" and shifts occupancy-driven demand.
        Weather does not enter load shapes — it drives PV only.
        """
        return self._builder(season, day_type)

    def peak_kw(self, season: str = "summer") -> float:
        return float(np.max(self.build(season)))


_REGISTRY: dict[str, Archetype] = {}


def register_archetype(name: str, category: str, builder) -> None:
    _REGISTRY[name] = Archetype(name, category, builder)


def get_archetype(name: str) -> Archetype:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown archetype '{name}'. Available: {available_archetypes()}"
        )
    return _REGISTRY[name]


def is_archetype(name: str) -> bool:
    return name in _REGISTRY


def available_archetypes() -> list[str]:
    """All registered archetype names (residential + commercial)."""
    return list(_REGISTRY)


def archetypes_in_category(category: str) -> list[str]:
    return [n for n, a in _REGISTRY.items() if a.category == category]
