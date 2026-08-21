"""Tests for the pluggable coordination-strategy catalog."""

from app.control import strategy_catalog
from app.control.strategy_catalog import (
    available_strategies,
    is_registered_strategy,
    register_strategy,
)


def test_builtin_strategies_registered():
    names = {s["name"] for s in available_strategies()}
    assert names == {"dr_only", "dr_p2p", "pv_curtail_only"}
    assert is_registered_strategy("dr_only")
    assert not is_registered_strategy("uncoordinated")


def test_a_custom_strategy_is_picked_up():
    """Registering a strategy makes it selectable/valid — no engine edits."""
    saved = dict(strategy_catalog._REGISTRY)
    try:
        register_strategy("demo_strategy", "A demo coordination strategy")
        assert is_registered_strategy("demo_strategy")
        listed = {s["name"]: s["description"] for s in available_strategies()}
        assert listed["demo_strategy"] == "A demo coordination strategy"
    finally:
        strategy_catalog._REGISTRY.clear()
        strategy_catalog._REGISTRY.update(saved)
