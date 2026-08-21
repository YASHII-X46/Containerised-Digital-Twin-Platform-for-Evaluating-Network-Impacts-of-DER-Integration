"""Pluggable tariff registry — named import/export price structures.

A tariff maps hour-of-day to an import rate and carries a feed-in rate; the
cost KPIs price the substation import/export series with whichever tariff the
simulate request names. Registering a new structure makes it selectable with no
engine edits (the same pattern as the KPI, strategy, and importer registries).

Built-ins (defaults env-overridable through the TARIFF_* / FLAT_RATE settings):

  - ``tou_residential`` — the representative Australian residential
    time-of-use tariff (peak window on a 24-h clock).
  - ``flat``            — a single anytime rate.
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class Tariff:
    """An import/export price structure on a 24-hour clock."""

    name: str
    description: str
    peak_rate: float          # AUD/kWh inside the peak window
    offpeak_rate: float       # AUD/kWh outside it
    feed_in_rate: float       # AUD/kWh credited for exports
    peak_start: float = 0.0   # 24-h clock; start == end means "no peak window"
    peak_end: float = 0.0

    def import_rate(self, hour: float) -> float:
        """The import rate (AUD/kWh) applying at an hour of day."""
        s, e = self.peak_start, self.peak_end
        if s == e:
            return self.offpeak_rate
        in_peak = (s <= hour < e) if s <= e else (hour >= s or hour < e)
        return self.peak_rate if in_peak else self.offpeak_rate


_REGISTRY: dict[str, Tariff] = {}


def register_tariff(tariff: Tariff) -> None:
    _REGISTRY[tariff.name] = tariff


def get_tariff(name: str) -> Tariff:
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown tariff '{name}'. Available: {sorted(_REGISTRY)}."
        )
    return _REGISTRY[name]


def available_tariffs() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "peak_rate": t.peak_rate,
            "offpeak_rate": t.offpeak_rate,
            "feed_in_rate": t.feed_in_rate,
        }
        for _, t in sorted(_REGISTRY.items())
    ]


register_tariff(Tariff(
    name="tou_residential",
    description="Australian residential time-of-use (peak window from settings)",
    peak_rate=settings.TARIFF_PEAK_RATE,
    offpeak_rate=settings.TARIFF_OFFPEAK_RATE,
    feed_in_rate=settings.TARIFF_FEED_IN_RATE,
    peak_start=settings.TARIFF_PEAK_START,
    peak_end=settings.TARIFF_PEAK_END,
))
register_tariff(Tariff(
    name="flat",
    description="Single anytime rate",
    peak_rate=settings.FLAT_RATE,
    offpeak_rate=settings.FLAT_RATE,
    feed_in_rate=settings.TARIFF_FEED_IN_RATE,
))
