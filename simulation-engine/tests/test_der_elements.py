"""Tests for the pluggable sim-side DER-element registry."""

from app.config import settings
from app.simulation import der_elements
from app.simulation.der_elements import (
    DERElement,
    element_names,
    installed_elements,
    register,
)
from app.simulation.qsts import QSTSSimulation


def test_builtin_elements_registered_in_order():
    # PV and BESS generators, then EV loads.
    assert element_names() == ["pv", "bess", "ev"]


class _StubEngine:
    """Records the OpenDSS edits the QSTS loop would issue."""

    def __init__(self):
        self.loads = {}
        self.pv = {}
        self.bess = {}
        self.ev = {}
        self.heatpump = {}

    def update_load(self, bus_id, kw, kvar):
        self.loads[bus_id] = (kw, kvar)

    def update_pv(self, bus_id, kw):
        self.pv[bus_id] = kw

    def update_bess(self, bus_id, kw):
        self.bess[bus_id] = kw

    def update_ev(self, bus_id, kw):
        self.ev[bus_id] = kw

    def update_heatpump(self, bus_id, kw):
        self.heatpump[bus_id] = kw

    def solve(self):
        return True

    def get_bus_voltages_pu(self):
        return {2: 1.0}

    def get_branch_loadings_pct(self):
        return {1: 0.0}

    def get_total_losses_kw(self):
        return 0.0

    def get_total_power_kw(self):
        return 0.0

    def get_max_vuf_pct(self):
        return 0.0


class _StubNet:
    source_bus = 1


def _one_bus_profiles():
    return {
        "metadata": {"scenario_name": "t", "seed": 1, "der_penetration_percent": 0,
                     "timesteps": 1, "resolution_minutes": 15},
        "buses": {
            2: {
                "pv_capacity_kw": 10.0,
                "bess_capacity_kwh": 5.0,
                "ev_charge_rate_kw": 7.0,
                "timeseries": [{
                    "timestamp": "t0", "load_kw": 80.0, "load_kvar": 48.0,
                    "pv_kw": 3.0, "bess_power_kw": 2.0, "ev_charge_kw": 4.0,
                    "net_load_kw": 79.0, "other_der_kw": 1.0, "heatpump_kw": 1.0,
                }],
            },
        },
    }


def test_custom_der_element_installed_and_driven():
    """Registering a DER element makes the simulate handler install it and the
    QSTS loop drive it — no edits to the engine loop."""

    class HeatPumpElement(DERElement):
        name, order = "heatpump", 25  # between BESS (20) and EV (30)
        summary_key, series_key = "buses_with_heatpump", "heatpump_kw"

        def buses(self, profiles):
            return [
                {"bus_id": bid}
                for bid, data in profiles["buses"].items()
                if data["timeseries"][0].get("heatpump_kw", 0.0)
            ]

        def update(self, engine, bus_id, value):
            engine.update_heatpump(bus_id, value)

    saved = dict(der_elements._REGISTRY)
    try:
        register(HeatPumpElement())
        assert "heatpump" in element_names()

        engine = _StubEngine()
        QSTSSimulation(engine, _one_bus_profiles(), settings, _StubNet()).run()

        # Building load carries the +1 kW other-DER (heat pump) fold-in.
        assert engine.loads[2] == (81.0, 48.0)
        # Each built-in element drove its own series...
        assert engine.pv[2] == 3.0
        assert engine.bess[2] == 2.0
        assert engine.ev[2] == 4.0
        # ...and the custom element's setpoint flowed through unchanged.
        assert engine.heatpump[2] == 1.0
    finally:
        der_elements._REGISTRY.clear()
        der_elements._REGISTRY.update(saved)
