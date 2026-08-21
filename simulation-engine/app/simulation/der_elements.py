"""Pluggable DER-element registry (solver-agnostic side).

Each DER device type (PV, BESS, EV — and any device a user adds) is a
``DERElement`` that knows which buses carry it, which profile series drives it,
and which solver update op applies its per-timestep setpoint. The simulate
handler sends the per-element bus lists to the selected solver container's
``build`` command (the solver owns the model-building half), and the QSTS loop
iterates whatever elements are *installed*, in ascending ``order`` — so adding
a DER type is: write a ``DERElement`` here, register its builder in the solver
service, call ``register()`` — no edits to the engine loop.

This mirrors the Load Engine's DER-generation plugins and the DR controller's
control plugins, completing the modular DER story end to end.
"""

from app.simulation.loader import get_bess_buses, get_ev_buses, get_pv_buses


class DERElement:
    """Base class: one solver-modelled DER device type."""

    name: str = "der"
    order: int = 100
    # Response field this element's bus count feeds (e.g. "buses_with_pv").
    summary_key: str = ""
    # Profile timeseries key carrying this element's per-timestep value.
    series_key: str = ""

    def buses(self, profiles: dict) -> list[dict]:
        """Bus configs that carry this DER, from the inline profiles payload."""
        raise NotImplementedError

    def update(self, engine, bus_id: int, value: float) -> None:
        """Drive this element's setpoint for the current timestep."""
        raise NotImplementedError

    def value_at(self, bus_data: dict, t: int) -> float:
        """This element's value at timestep ``t`` from a bus's timeseries."""
        return float(bus_data["timeseries"][t].get(self.series_key, 0.0))


class PVElement(DERElement):
    name, order = "pv", 10
    summary_key, series_key = "buses_with_pv", "pv_kw"

    def buses(self, profiles):
        return get_pv_buses(profiles)

    def update(self, engine, bus_id, value):
        engine.update_pv(bus_id, value)


class BESSElement(DERElement):
    name, order = "bess", 20
    summary_key, series_key = "buses_with_bess", "bess_power_kw"

    def buses(self, profiles):
        return get_bess_buses(profiles)

    def update(self, engine, bus_id, value):
        engine.update_bess(bus_id, value)


class EVElement(DERElement):
    name, order = "ev", 30
    summary_key, series_key = "buses_with_ev", "ev_charge_kw"

    def buses(self, profiles):
        return get_ev_buses(profiles)

    def update(self, engine, bus_id, value):
        engine.update_ev(bus_id, value)


_REGISTRY: dict[str, DERElement] = {}


def register(element: DERElement) -> None:
    _REGISTRY[element.name] = element


def installed_elements() -> list[DERElement]:
    """All registered DER elements, in install/update (dependency) order."""
    return sorted(_REGISTRY.values(), key=lambda e: e.order)


def element_names() -> list[str]:
    return [e.name for e in installed_elements()]


for _e in (PVElement(), BESSElement(), EVElement()):
    register(_e)
