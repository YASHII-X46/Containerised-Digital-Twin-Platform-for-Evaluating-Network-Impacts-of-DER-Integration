"""Tests for the standalone DR controller service."""

import pytest

from app.bus import BusParticipant, LoopbackTransport
from app.config import settings
from app.controller import DRController
from app.main import DRControllerService
from app import control_plugins, strategy_registry
from app.control_plugins import ControlPlugin, control_plugin_names, register
from app.openfmb import build_der_control


def _readings(v, pv=0.0, ev=0.0, soc=0.5, cap=0.0, steph=0.25):
    return {
        "voltageMagnitude_pu": v,
        "pvOutput_kW": pv,
        "evCharge_kW": ev,
        "stateOfCharge": soc,
        "bessCapacity_kWh": cap,
        "stepDuration_h": steph,
    }


def test_overvoltage_curtails_pv_on_droop():
    controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_only")
    assert controller.control_for(_readings(1.0, pv=40))["pv_curtailment_kw"] == 0.0
    assert abs(controller.control_for(_readings(1.10, pv=40))["pv_curtailment_kw"] - 40) < 1e-9
    assert abs(controller.control_for(_readings(1.075, pv=40))["pv_curtailment_kw"] - 20) < 1e-9


def test_undervoltage_defers_ev():
    controller = DRController(v_lower=0.95, droop_band=0.05)
    assert controller.control_for(_readings(0.98, ev=7))["ev_curtailment_kw"] == 0.0
    assert abs(controller.control_for(_readings(0.90, ev=7))["ev_curtailment_kw"] - 7) < 1e-9


def test_p2p_absorbs_into_battery_before_curtailing():
    controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_p2p")
    result = controller.control_for(_readings(1.10, pv=40, soc=0.5, cap=13.5, steph=0.25))
    assert abs(result["bess_charge_kw"] - 24.3) < 0.1
    assert abs(result["pv_curtailment_kw"] - (40 - 24.3)) < 0.1


def test_builtin_control_plugins_registered_in_order():
    # Storage first (absorb, then support), PV real then reactive, envelope, EV.
    assert control_plugin_names() == [
        "bess", "bess_support", "pv", "pv_reactive", "envelope", "ev",
    ]


def test_bess_support_discharges_on_undervoltage():
    controller = DRController(v_lower=0.95, droop_band=0.05)
    # Full droop signal at 0.90 pu; energy above min SOC bounds the discharge:
    # (0.5 - 0.1) x 13.5 kWh / 0.25 h = 21.6 kW.
    out = controller.control_for(_readings(0.90, soc=0.5, cap=13.5))
    assert abs(out["bess_discharge_kw"] - 21.6) < 1e-9
    # Half signal halves the support; empty battery gives none.
    half = controller.control_for(_readings(0.925, soc=0.5, cap=13.5))
    assert abs(half["bess_discharge_kw"] - 10.8) < 1e-9
    empty = controller.control_for(_readings(0.90, soc=0.1, cap=13.5))
    assert empty["bess_discharge_kw"] == 0.0


def test_pv_reactive_dispatch_signs():
    controller = DRController(v_upper=1.05, v_lower=0.95, droop_band=0.05)
    r = _readings(0.90, pv=0.0)
    r["pvCapacity_kW"] = 10.0
    inject = controller.control_for(r)
    assert abs(inject["pv_reactive_kvar"] - 4.4) < 1e-9    # +0.44 x 10 x 1.0
    r = _readings(1.10, pv=0.0)
    r["pvCapacity_kW"] = 10.0
    absorb = controller.control_for(r)
    assert abs(absorb["pv_reactive_kvar"] + 4.4) < 1e-9    # absorb at over-voltage
    nominal = controller.control_for({**_readings(1.0), "pvCapacity_kW": 10.0})
    assert nominal["pv_reactive_kvar"] == 0.0


def test_envelope_plugin_stores_then_curtails():
    controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_only")
    # Export 30 - 10 = 20 kW against a 5 kW limit: 15 kW excess. Battery
    # headroom (0.95 - 0.5) x 10 kWh / 0.25 h = 18 kW absorbs it all.
    r = _readings(1.0, pv=30, soc=0.5, cap=10.0)
    r.update({"exportLimit_kW": 5.0, "loadDemand_kW": 10.0})
    out = controller.control_for(r)
    assert abs(out["bess_charge_kw"] - 15.0) < 1e-9
    assert out["pv_curtailment_kw"] == 0.0

    # Nearly full battery: headroom 0.4 kW -> the remainder is curtailed.
    r = _readings(1.0, pv=30, soc=0.94, cap=10.0)
    r.update({"exportLimit_kW": 5.0, "loadDemand_kW": 10.0})
    out = controller.control_for(r)
    assert abs(out["bess_charge_kw"] - 0.4) < 1e-6
    assert abs(out["pv_curtailment_kw"] - 14.6) < 1e-6

    # No battery at all: the full excess curtails.
    r = _readings(1.0, pv=30, cap=0.0)
    r.update({"exportLimit_kW": 5.0, "loadDemand_kW": 10.0})
    assert abs(controller.control_for(r)["pv_curtailment_kw"] - 15.0) < 1e-9


def test_envelope_combines_with_voltage_curtailment_by_max():
    controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_only")
    # Voltage at 1.10 wants the full 30 kW; the envelope only needs 15 kW —
    # the stricter (larger) curtailment wins.
    r = _readings(1.10, pv=30, cap=0.0)
    r.update({"exportLimit_kW": 5.0, "loadDemand_kW": 10.0})
    assert abs(controller.control_for(r)["pv_curtailment_kw"] - 30.0) < 1e-9


def test_no_envelope_reading_means_no_envelope_action():
    controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_only")
    out = controller.control_for(_readings(1.0, pv=30))
    assert out["pv_curtailment_kw"] == 0.0 and out["bess_charge_kw"] == 0.0


def test_custom_control_plugin_is_picked_up():
    """Registering a control device makes the controller emit its setpoint."""
    class WaterHeaterPlugin(ControlPlugin):
        name, order = "waterheater", 25  # between PV (20) and EV (30)

        def compute(self, ctx):
            if ctx.over_signal > 0.0:  # shed a controllable load on over-voltage
                ctx.setpoints["waterheater_shed_kw"] = 3.0

    saved = dict(control_plugins._REGISTRY)
    try:
        register(WaterHeaterPlugin())
        assert "waterheater" in control_plugin_names()
        controller = DRController(v_upper=1.05, droop_band=0.05, mode="dr_only")
        out = controller.control_for(_readings(1.10, pv=40))
        assert out["waterheater_shed_kw"] == 3.0          # custom setpoint emitted
        assert abs(out["pv_curtailment_kw"] - 40) < 1e-9  # built-in behaviour intact
    finally:
        control_plugins._REGISTRY.clear()
        control_plugins._REGISTRY.update(saved)


def test_build_der_control_carries_custom_setpoints():
    msg = build_der_control(18, "ts", pv_curtailment_kw=5.0, waterheater_shed_kw=3.0)
    readings = msg["readings"]
    assert readings["curtailment_kW"] == 5.0
    assert readings["waterheater_shed_kw"] == 3.0


def test_control_devices_command_lists_plugins():
    transport = LoopbackTransport()
    service_participant = BusParticipant(transport, "dr-controller")
    DRControllerService(settings).register(service_participant)
    service_participant.start()
    client = BusParticipant(transport, "client")
    resp = client.request("dr-controller", "control-devices", {})
    assert resp["status"] == "ok"
    assert resp["payload"]["control_devices"] == [
        "bess", "bess_support", "pv", "pv_reactive", "envelope", "ev",
    ]


def test_rejects_bad_mode_and_band():
    with pytest.raises(ValueError):
        DRController(mode="bogus")
    with pytest.raises(ValueError):
        DRController(droop_band=0.0)


def test_strategy_registry_builtins():
    names = {strategy["name"] for strategy in strategy_registry.available()}
    assert {"dr_only", "dr_p2p", "pv_curtail_only"} <= names
    assert isinstance(strategy_registry.create("dr_only", settings), DRController)


def test_controller_service_command_roundtrip():
    transport = LoopbackTransport()
    service_participant = BusParticipant(transport, "dr-controller")
    DRControllerService(settings).register(service_participant)
    service_participant.start()

    client = BusParticipant(transport, "client")
    config = client.request("dr-controller", "configure", {
        "session_id": "s1",
        "mode": "dr_only",
    })
    assert config["status"] == "ok"
    assert config["payload"]["max_iterations"] > 0

    controls = client.request("dr-controller", "control", {
        "session_id": "s1",
        "statuses": [
            {
                "bus_id": 18,
                "payload": {
                    "mRID": "bus-018-der",
                    "timestamp": "ts",
                    "readings": _readings(1.10, pv=30),
                },
            }
        ],
    })
    bus18 = controls["payload"]["controls"][0]
    assert bus18["bus_id"] == 18
    assert bus18["topic"] == "openfmb/DERControlProfile/bus-018-der-control"
    assert bus18["readings"]["curtailment_kW"] > 0
