"""Tests for the pluggable DER device registry."""

import numpy as np

from app.profiles import der_plugins
from app.profiles.der_plugins import (
    DERPlugin,
    GenerationContext,
    der_types,
    installed_plugins,
    register,
)
from app.profiles.generator import ProfileGenerator, build_profiles_payload
from tests.ieee33_data import default_bus_data


def test_builtin_plugins_registered_in_dependency_order():
    names = der_types()
    assert names == ["load", "pv", "ev", "bess"]  # load/PV/EV before BESS


def test_context_get_defaults_to_zeros():
    ctx = GenerationContext(
        {"base_load_kw": 0, "base_load_kvar": 0, "customer_class": "res_townhouse"},
        7, timesteps=96, resolution_minutes=15, season="summer",
        reactive_floor=0.0, diversity={"enabled": False}, ev_diversity={"enabled": False},
        ev_charging_mode="uncontrolled", ev_offpeak_start_hour=23.0, custom_store=None,
    )
    assert np.all(ctx.get("pv_kw") == 0)
    assert len(ctx.get("pv_kw")) == 96


def test_a_custom_der_plugin_is_picked_up(monkeypatch):
    """Registering a new DER type makes the generator produce it — no edits."""
    class HeatPumpPlugin(DERPlugin):
        name, order = "heatpump", 35  # between EV (30) and BESS (40)

        def applies_to(self, bus):
            return bool(bus.get("has_heatpump"))

        def generate(self, ctx):
            ctx.series["heatpump_kw"] = np.full(ctx.timesteps, 2.0)

    # Register only for this test, then restore the registry.
    saved = dict(der_plugins._REGISTRY)
    try:
        register(HeatPumpPlugin())
        assert "heatpump" in der_types()
        bus = {
            "bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0,
            "customer_class": "res_townhouse", "has_heatpump": True,
        }
        ctx = GenerationContext(
            bus, 7, timesteps=96, resolution_minutes=15, season="summer",
            reactive_floor=0.0, diversity={"enabled": False},
            ev_diversity={"enabled": False}, ev_charging_mode="uncontrolled",
            ev_offpeak_start_hour=23.0, custom_store=None,
        )
        for p in installed_plugins():
            if p.applies_to(bus):
                p.generate(ctx)
        assert np.allclose(ctx.series["heatpump_kw"], 2.0)
        assert "load_kw" in ctx.series  # the load plugin still ran
    finally:
        der_plugins._REGISTRY.clear()
        der_plugins._REGISTRY.update(saved)


def test_custom_der_plugin_flows_into_net_load():
    """A registered DER's contribution reaches net_load with no generator edits."""
    class HeatPumpPlugin(DERPlugin):
        name, order = "heatpump", 35
        net_load = {"heatpump_kw": +1.0}  # a consumer: adds to net load

        def applies_to(self, bus):
            return bool(bus.get("has_heatpump"))

        def generate(self, ctx):
            ctx.series["heatpump_kw"] = np.full(ctx.timesteps, 2.0)

    saved = dict(der_plugins._REGISTRY)
    try:
        register(HeatPumpPlugin())
        bus = {
            "bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0,
            "customer_class": "res_townhouse", "has_heatpump": True,
        }
        cfg = {
            "bus_data": [bus], "seed": 42, "timesteps": 96,
            "resolution_minutes": 15, "season": "summer",
        }
        profiles = ProfileGenerator(cfg).generate_all_profiles()
        p = profiles[2]
        # The heat pump series is preserved...
        assert np.allclose(p["heatpump_kw"], 2.0)
        # ...and its +2 kW is reflected in net load.
        expected = (
            p["load_kw"] - p["pv_kw"] + p["ev_charge_kw"]
            - p["bess_power_kw"] + p["heatpump_kw"]
        )
        np.testing.assert_allclose(p["net_load_kw"], expected, atol=1e-9)
    finally:
        der_plugins._REGISTRY.clear()
        der_plugins._REGISTRY.update(saved)


def test_custom_der_plugin_carried_in_wire_payload():
    """A registered DER reaches the Sim wire payload: named series + other_der_kw."""
    class HeatPumpPlugin(DERPlugin):
        name, order = "heatpump", 35
        net_load = {"heatpump_kw": +1.0}

        def applies_to(self, bus):
            return bool(bus.get("has_heatpump"))

        def generate(self, ctx):
            ctx.series["heatpump_kw"] = np.full(ctx.timesteps, 2.0)

    saved = dict(der_plugins._REGISTRY)
    try:
        register(HeatPumpPlugin())
        bus = {
            "bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0,
            "customer_class": "res_townhouse", "has_heatpump": True,
            "pv_capacity_kw": 0.0,
        }
        cfg = {
            "bus_data": [bus], "seed": 42, "timesteps": 96,
            "resolution_minutes": 15, "season": "summer",
        }
        profiles = ProfileGenerator(cfg).generate_all_profiles()
        payload = build_profiles_payload(profiles, [bus], "s", 42, 0.0, 15)
        assert payload["metadata"]["extra_der_series"] == ["heatpump_kw"]
        entry = payload["buses"][2]["timeseries"][0]
        # The named series is carried, and the +2 kW heat pump shows up as the
        # generic other_der_kw the Sim Engine applies (net - the four built-ins).
        assert entry["heatpump_kw"] == 2.0
        assert abs(entry["other_der_kw"] - 2.0) < 1e-6
    finally:
        der_plugins._REGISTRY.clear()
        der_plugins._REGISTRY.update(saved)


def test_builtin_only_payload_has_zero_other_der():
    cfg = {
        "bus_data": default_bus_data(der_penetration_percent=100),
        "seed": 42, "timesteps": 96, "resolution_minutes": 15, "season": "summer",
    }
    profiles = ProfileGenerator(cfg).generate_all_profiles()
    payload = build_profiles_payload(
        profiles, default_bus_data(der_penetration_percent=100), "s", 42, 100.0, 15)
    assert payload["metadata"]["extra_der_series"] == []
    for bus in payload["buses"].values():
        for entry in bus["timeseries"]:
            assert abs(entry["other_der_kw"]) < 1e-6


def test_generator_via_registry_matches_expected_keys():
    cfg = {
        "bus_data": default_bus_data(der_penetration_percent=100),
        "seed": 42, "timesteps": 96, "resolution_minutes": 15, "season": "summer",
    }
    profiles = ProfileGenerator(cfg).generate_all_profiles()
    p = profiles[2]
    for key in ("load_kw", "load_kvar", "pv_kw", "ev_charge_kw",
                "bess_power_kw", "bess_soc", "net_load_kw"):
        assert key in p and len(p[key]) == 96
