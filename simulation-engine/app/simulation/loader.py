"""Derive DER element lists from an inline profiles payload.

The profiles arrive inline over the message bus (the Load Engine's wire format);
these helpers pick out which buses carry PV, BESS, and EV so the solver's model
builders know which elements to create.
"""


def get_pv_buses(profiles: dict) -> list[dict]:
    """Returns a list of PV bus configs for buses with pv_capacity_kw > 0."""
    pv_buses = []
    for bus_id, bus_data in profiles["buses"].items():
        if bus_data["pv_capacity_kw"] > 0:
            pv_buses.append({
                "bus_id": bus_id,
                "pv_capacity_kw": bus_data["pv_capacity_kw"],
            })
    return pv_buses


def get_bess_buses(profiles: dict) -> list[dict]:
    """Returns a list of BESS bus configs for buses with bess_capacity_kwh > 0."""
    bess_buses = []
    for bus_id, bus_data in profiles["buses"].items():
        if bus_data["bess_capacity_kwh"] > 0:
            bess_buses.append({
                "bus_id": bus_id,
                "bess_capacity_kwh": bus_data["bess_capacity_kwh"],
            })
    return bess_buses


def get_ev_buses(profiles: dict) -> list[dict]:
    """Returns a list of EV bus configs for buses with ev_charge_rate_kw > 0."""
    ev_buses = []
    for bus_id, bus_data in profiles["buses"].items():
        if bus_data["ev_charge_rate_kw"] > 0:
            ev_buses.append({
                "bus_id": bus_id,
                "ev_charge_rate_kw": bus_data["ev_charge_rate_kw"],
            })
    return ev_buses
