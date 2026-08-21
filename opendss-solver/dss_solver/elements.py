"""Pluggable OpenDSS DER-element builders and per-step update dispatch.

The `build` command carries per-element bus lists (produced by the Simulation
Engine's DER-element registry); each entry here knows how to create the
matching OpenDSS elements and how to apply a per-timestep update op. Adding a
new DER device type solver-side is: write a builder/updater pair, call
``register()`` — the generic `solve` op stream then drives it with no changes
to the service loop.
"""

import logging

from dss_solver.dss_model import (
    generate_all_bess_dss,
    generate_all_ev_dss,
    generate_all_pv_dss,
)

logger = logging.getLogger(__name__)


class ElementType:
    """One OpenDSS-modelled DER device type: build + per-step update."""

    name: str = "der"

    def install(self, engine, buses: list[dict], network, dss_dir: str,
                solve_mode: str) -> None:
        raise NotImplementedError

    def apply(self, engine, op: dict) -> None:
        """Apply one per-timestep update op ``{op, bus_id, ...}``."""
        raise NotImplementedError


class PVElement(ElementType):
    name = "pv"

    def install(self, engine, buses, network, dss_dir, solve_mode):
        engine.load_pv_systems(generate_all_pv_dss(buses, network, dss_dir, solve_mode))

    def apply(self, engine, op):
        engine.update_pv(int(op["bus_id"]), float(op.get("kw", 0.0)))


class PVReactive(ElementType):
    """Reactive-power setpoint on an existing PV inverter (no install step)."""

    name = "pv_q"

    def install(self, engine, buses, network, dss_dir, solve_mode):
        pass  # rides on the PV element

    def apply(self, engine, op):
        engine.update_pv_reactive(int(op["bus_id"]), float(op.get("kvar", 0.0)))


class BESSElement(ElementType):
    name = "bess"

    def install(self, engine, buses, network, dss_dir, solve_mode):
        engine.load_bess_systems(generate_all_bess_dss(buses, network, dss_dir, solve_mode))

    def apply(self, engine, op):
        engine.update_bess(int(op["bus_id"]), float(op.get("kw", 0.0)))


class EVElement(ElementType):
    name = "ev"

    def install(self, engine, buses, network, dss_dir, solve_mode):
        engine.load_ev_loads(generate_all_ev_dss(buses, network, dss_dir, solve_mode))

    def apply(self, engine, op):
        engine.update_ev(int(op["bus_id"]), float(op.get("kw", 0.0)))


class LoadElement(ElementType):
    """Building load — created by the master file, updated per step."""

    name = "load"

    def install(self, engine, buses, network, dss_dir, solve_mode):
        pass  # Load elements are part of master.dss

    def apply(self, engine, op):
        engine.update_load(
            int(op["bus_id"]), float(op.get("kw", 0.0)), float(op.get("kvar", 0.0))
        )


_REGISTRY: dict[str, ElementType] = {}


def register(element: ElementType) -> None:
    _REGISTRY[element.name] = element


def get_element(name: str) -> ElementType | None:
    return _REGISTRY.get(name)


def element_names() -> list[str]:
    return sorted(_REGISTRY)


def install_elements(engine, element_buses: dict[str, list[dict]], network,
                     dss_dir: str, solve_mode: str) -> dict[str, int]:
    """Create OpenDSS elements for every element type in the build payload.

    Unknown element types are skipped with a logged count (a custom DER type
    needs its solver-side builder registered here), never a silent drop.
    """
    counts: dict[str, int] = {}
    for name, buses in (element_buses or {}).items():
        element = _REGISTRY.get(name)
        if element is None:
            logger.warning(
                "No OpenDSS builder registered for DER element type '%s' "
                "(%d buses skipped) — register one in dss_solver.elements.",
                name, len(buses),
            )
            continue
        if buses:
            element.install(engine, buses, network, dss_dir, solve_mode)
        counts[name] = len(buses)
    return counts


for _e in (LoadElement(), PVElement(), PVReactive(), BESSElement(), EVElement()):
    register(_e)
