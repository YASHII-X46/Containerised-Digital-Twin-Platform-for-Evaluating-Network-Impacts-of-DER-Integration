"""IEEE 33-bus scenario data — **test fixtures only**.

The load engine no longer hardcodes any network: in production the bus list (with
base loads) comes from the selected network model via `network_buses`. These
constants/helper exist solely so the tests can build a 33-bus default scenario;
they are not imported by `app/`.
"""

import numpy as np

from app.profiles.ev_model import EV_CONFIGS
from app.profiles.generator import build_bus_data

IEEE33_BASE_LOADS = {
    2: 100, 3: 90, 4: 120, 5: 60, 6: 60,
    7: 200, 8: 200, 9: 60, 10: 60, 11: 45, 12: 60,
    13: 250, 14: 250, 15: 60, 16: 300, 17: 350,
    18: 400, 19: 150, 20: 200, 21: 250, 22: 300,
    23: 150, 24: 200, 25: 250, 26: 60, 27: 60,
    28: 120, 29: 200, 30: 150, 31: 210, 32: 60, 33: 60,
}

IEEE33_ARCHETYPE_MAP = {
    2: "res_detached_medium", 3: "res_detached_medium", 4: "res_detached_medium",
    5: "res_detached_small", 6: "res_detached_small",
    7: "res_detached_large", 8: "res_detached_large",
    9: "res_townhouse", 10: "res_townhouse",
    11: "res_apartment_lowrise",
    12: "res_apartment_highrise",
    13: "res_detached_large", 14: "res_detached_large",
    15: "res_detached_small", 16: "res_detached_large",
    17: "res_detached_large", 18: "res_detached_large",
    19: "res_detached_medium", 20: "res_detached_medium",
    21: "res_detached_large", 22: "res_detached_large",
    23: "res_townhouse", 24: "res_townhouse",
    25: "res_detached_medium", 26: "res_detached_small",
    27: "res_detached_small", 28: "res_townhouse",
    29: "res_detached_medium", 30: "res_townhouse",
    31: "res_detached_medium", 32: "res_apartment_lowrise",
    33: "res_apartment_lowrise",
}

# The 33-bus network as the load engine receives it (network_buses), for API tests.
IEEE33_NETWORK_BUSES = [{"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0}] + [
    {"bus_id": bid, "base_load_kw": float(kw)} for bid, kw in IEEE33_BASE_LOADS.items()
]


def default_bus_data(
    der_penetration_percent: float = 100,
    pv_buses: list[int] | None = None,
    bess_penetration: float = 0.3,
    bess_config: str = "powerwall_2",
    ev_penetration: float = 0.2,
    ev_config: str = "level2_7kw",
) -> list[dict]:
    """Build the default IEEE 33-bus bus configuration (was the engine default)."""
    if pv_buses is None:
        pv_buses = [18, 22, 25, 29, 30, 31, 32, 33]

    buses = build_bus_data(
        IEEE33_NETWORK_BUSES,
        der_penetration_percent=der_penetration_percent,
        pv_buses=pv_buses,
        bess_penetration=bess_penetration,
        bess_config=bess_config,
        ev_penetration=0.0,
        ev_config=ev_config,
        archetype_map=IEEE33_ARCHETYPE_MAP,
        source_bus=1,
    )

    # Limit EV to the first residential pocket (buses 2-12).
    residential_buses = list(range(2, 13))
    rng_ev = np.random.default_rng(13)
    n_ev = int(round(ev_penetration * len(residential_buses)))
    ev_buses = set(rng_ev.choice(residential_buses, size=n_ev, replace=False)) if n_ev > 0 else set()
    ev_params = EV_CONFIGS.get(ev_config, {})
    for bus in buses:
        if bus["bus_id"] in ev_buses:
            bus["ev_config"] = ev_config
            bus["ev_charge_rate_kw"] = ev_params.get("charge_rate_kw", 0.0)
    return buses
