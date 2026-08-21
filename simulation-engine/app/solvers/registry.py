"""Solver registry — power-flow engines as named, swappable bus services.

Each solver is a standalone container implementing the solver bus contract
(build / solve / read / reset / teardown under
``{prefix}/command/{service}/{action}``). The simulate request selects one by
name (default ``opendss``); ``register_solver()`` adds more — the same
plug-and-play pattern as every other registry in the stack. The Simulation
Engine itself contains no solver code.
"""

_REGISTRY: dict[str, dict] = {}


def register_solver(name: str, service: str, description: str = "") -> None:
    """Register a solver backend: ``name`` -> the bus service implementing it."""
    _REGISTRY[name] = {"service": service, "description": description}


def resolve_solver_service(name: str) -> str:
    """The bus service name for a registered solver (KeyError when unknown)."""
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown solver '{name}'. Available: {sorted(_REGISTRY)}."
        )
    return _REGISTRY[name]["service"]


def available_solvers() -> list[dict]:
    return [
        {"name": n, "service": _REGISTRY[n]["service"],
         "description": _REGISTRY[n]["description"]}
        for n in sorted(_REGISTRY)
    ]


register_solver(
    "opendss", "opendss-solver",
    "EPRI OpenDSS QSTS power flow (default; opendss-solver container)",
)
register_solver(
    "sincal", "sincal-solver",
    "Siemens PSS SINCAL adapter (sincal-solver service; requires a licensed "
    "PSS SINCAL installation where that adapter runs)",
)
