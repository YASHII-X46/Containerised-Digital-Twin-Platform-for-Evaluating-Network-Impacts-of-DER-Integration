"""Australian commercial load archetypes — offices and education buildings.

Daily electricity day-shapes for the non-residential buildings found on an
Australian urban distribution feeder or university campus (e.g. Swinburne
Hawthorn): small and medium offices plus a tertiary-education teaching
building. Each shape is the sum of an always-on base (servers / standby), an
occupancy-driven plug-and-lighting load, and an HVAC component that follows
the season design day. Absolute magnitudes are representative only — the
generator normalises each shape and scales it to the bus's base demand, so
the *shape* is what matters here.
"""

import numpy as np

from app.profiles.archetypes import (
    gaussian,
    occupancy_window,
    register_archetype,
)


def _hvac(capacity_kw: float, occ: np.ndarray, season: str) -> np.ndarray:
    """Occupancy-gated HVAC on the season design-day curve (afternoon cooling
    in summer, morning and evening heating in winter). A small setback share
    runs outside occupied hours."""
    gated = 0.25 + 0.75 * occ  # setback when unoccupied, full when occupied
    if season == "winter":
        shape = gaussian(8.0, 1.6, 0.6) + gaussian(16.0, 2.2, 0.5)
    elif season == "shoulder":
        shape = 0.35 * gaussian(15.0, 3.0, 1.0)
    else:  # summer
        shape = gaussian(15.0, 3.2, 1.0)
    return capacity_kw * shape * gated


def _commercial(area_m2, plug_wm2, base_frac, hvac_kw, occ_start, occ_end, ramp=1.0,
                weekend_occ=1.0):
    """Office/education style: occupancy plug+lighting plus gated HVAC. On
    weekends occupancy is scaled by ``weekend_occ`` (near-zero for offices,
    low but non-zero for campus buildings)."""
    def build(season, day_type="weekday"):
        occ = occupancy_window(occ_start, occ_end, ramp)
        if day_type == "weekend":
            occ = occ * weekend_occ
        plug = (plug_wm2 * area_m2 / 1000.0) * (base_frac + (1.0 - base_frac) * occ)
        return plug + _hvac(hvac_kw, occ, season)
    return build


# name -> (category, builder)
_ARCHETYPES = {
    "com_small_office":  ("commercial", _commercial(511, 12.0, 0.18, 14.0, 7.0, 18.0, weekend_occ=0.08)),
    "com_medium_office": ("commercial", _commercial(4982, 11.0, 0.20, 130.0, 7.0, 18.5, weekend_occ=0.10)),
    # University/TAFE teaching building: long teaching day with evening
    # classes, a higher always-on base (labs, IT), and weekend library-level
    # activity rather than a full shutdown.
    "com_education":     ("commercial", _commercial(6871, 10.0, 0.12, 150.0, 8.0, 21.5, weekend_occ=0.15)),
}

for _name, (_category, _builder) in _ARCHETYPES.items():
    register_archetype(_name, _category, _builder)
