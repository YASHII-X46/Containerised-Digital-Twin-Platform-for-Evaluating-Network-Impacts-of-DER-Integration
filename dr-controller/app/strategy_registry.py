"""Registry of DR control strategies."""

from collections.abc import Callable

from app.controller import DRController

StrategyFactory = Callable[..., object]

_REGISTRY: dict[str, dict] = {}


def register(name: str, factory: StrategyFactory, description: str = "") -> None:
    _REGISTRY[name] = {"factory": factory, "description": description}


def available() -> list[dict]:
    return [
        {"name": name, "description": _REGISTRY[name]["description"]}
        for name in sorted(_REGISTRY)
    ]


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def create(name: str, settings, topic_prefix: str = "openfmb"):
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown coordination strategy '{name}'. "
            f"Available: {[strategy['name'] for strategy in available()]} "
            f"(or 'uncoordinated')."
        )
    return _REGISTRY[name]["factory"](settings, topic_prefix)


def _dr_only(settings, topic_prefix):
    return DRController(
        v_upper=settings.VOLTAGE_UPPER_PU,
        v_lower=settings.VOLTAGE_LOWER_PU,
        mode="dr_only",
        topic_prefix=topic_prefix,
    )


def _dr_p2p(settings, topic_prefix):
    return DRController(
        v_upper=settings.VOLTAGE_UPPER_PU,
        v_lower=settings.VOLTAGE_LOWER_PU,
        mode="dr_p2p",
        topic_prefix=topic_prefix,
    )


def _pv_curtail_only(settings, topic_prefix):
    return DRController(
        v_upper=settings.VOLTAGE_UPPER_PU,
        v_lower=settings.VOLTAGE_LOWER_PU,
        mode="dr_only",
        control_ev=False,
        topic_prefix=topic_prefix,
    )


register("dr_only", _dr_only, "Volt-Watt PV curtailment + EV deferral")
register("dr_p2p", _dr_p2p, "dr_only + self-absorption into local battery")
register("pv_curtail_only", _pv_curtail_only, "Volt-Watt PV curtailment only (no EV)")
