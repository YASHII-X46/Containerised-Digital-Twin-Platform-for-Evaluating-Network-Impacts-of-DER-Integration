"""Coordination strategy catalog exposed by the Simulation Engine.

A registry of selectable coordination strategy names: register one to make it
appear in the engine's options and pass request validation — no engine edits.
The strategies themselves *execute* in the dr-controller service (reached over
the OpenFMB bus); this is the sim-engine-side catalog of selectable names, kept
in sync with that service's strategy registry. Same plug-and-play pattern as the
KPI registry and the Load Engine's DER plugins.
"""

_REGISTRY: dict[str, dict] = {}


def register_strategy(name: str, description: str = "") -> None:
    _REGISTRY[name] = {"description": description}


def available_strategies() -> list[dict]:
    return [
        {"name": name, "description": _REGISTRY[name]["description"]}
        for name in sorted(_REGISTRY)
    ]


def is_registered_strategy(name: str) -> bool:
    return name in _REGISTRY


register_strategy("dr_only", "Volt-Watt PV curtailment + EV deferral")
register_strategy("dr_p2p", "dr_only + self-absorption into local battery")
register_strategy("pv_curtail_only", "Volt-Watt PV curtailment only (no EV)")
