"""Tests for the Simulation Engine's remote coordination client."""

from app.control.remote_coordinator import RemoteCoordinator
from app.control.volt_var import VoltWattCurve
from app.config import settings


class FakeParticipant:
    def __init__(self):
        self.calls = []

    def request(self, service, action, payload, timeout=60.0):
        self.calls.append((service, action, payload))
        if (service, action) == ("dr-controller", "configure"):
            return {"status": "ok", "payload": {"mode": payload["mode"], "max_iterations": 2}}
        if (service, action) == ("prosumer-shadow-twins", "start"):
            return {"status": "ok", "payload": {"prosumer_twins": 1}}
        if (service, action) == ("prosumer-shadow-twins", "status"):
            return {
                "status": "ok",
                "payload": {
                    "statuses": [
                        {
                            "bus_id": 18,
                            "payload": {
                                "mRID": "bus-018-der",
                                "timestamp": payload["timestamp"],
                                "readings": {"voltageMagnitude_pu": 1.10},
                            },
                        }
                    ]
                },
            }
        if (service, action) == ("dr-controller", "control"):
            return {
                "status": "ok",
                "payload": {
                    "controls": [
                        {
                            "bus_id": 18,
                            "readings": {
                                "curtailment_kW": 10.0,
                                "evCurtailment_kW": 0.0,
                                "bessCharge_kW": 0.0,
                            },
                        }
                    ]
                },
            }
        if (service, action) == ("prosumer-shadow-twins", "record"):
            return {
                "status": "ok",
                "payload": {
                    "summary": {
                        "mode": payload["mode"],
                        "prosumer_twins": 1,
                        "buses_curtailed": 1,
                        "total_pv_curtailed_kwh": 2.5,
                        "total_ev_deferred_kwh": 0.0,
                        "total_pv_shared_kwh": 0.0,
                        "messages": 4,
                    }
                },
            }
        if action == "stop":
            return {"status": "ok", "payload": {"stopped": True}}
        raise AssertionError(f"Unexpected request {service}/{action}")


class FakeEngine:
    def __init__(self):
        self.pv_updates = {}

    def get_bus_voltages_pu(self):
        return {18: 1.10}

    def update_pv(self, bus_id, kw):
        self.pv_updates[bus_id] = kw

    def update_ev(self, bus_id, kw):
        raise AssertionError("EV should not be updated in this test")

    def update_bess(self, bus_id, kw):
        raise AssertionError("BESS should not be updated in this test")

    def solve(self):
        return True


def test_remote_coordinator_uses_external_control_services(sample_profiles):
    participant = FakeParticipant()
    engine = FakeEngine()

    coordinator = RemoteCoordinator(participant, sample_profiles, "dr_only", settings)
    assert coordinator.coordinate(0, engine, 0.25, "ts")
    summary = coordinator.summary()
    coordinator.close()

    services = [(service, action) for service, action, _payload in participant.calls]
    assert ("dr-controller", "configure") in services
    assert ("prosumer-shadow-twins", "start") in services
    assert ("prosumer-shadow-twins", "status") in services
    assert ("prosumer-shadow-twins", "record") in services
    assert engine.pv_updates[18] == sample_profiles["buses"][18]["timeseries"][0]["pv_kw"] - 10.0
    assert summary["total_pv_curtailed_kwh"] == 2.5
    # No twin_config given -> the start payload carries no config override.
    start = next(p for s, a, p in participant.calls if (s, a) == ("prosumer-shadow-twins", "start"))
    assert "config" not in start


def test_dr_curtailment_applies_on_volt_watt_reduced_base(sample_profiles):
    """With autonomous Volt-Watt active, DR curtails the REDUCED output.

    The bus sits at 1.10 pu (at/above the curve end), so the inverter has
    already dropped to the 20% floor; the commanded 10 kW curtailment applies
    to that floor value — re-applying the full scheduled PV would silently
    undo the standards response (the old double-booking bug).
    """
    participant = FakeParticipant()
    engine = FakeEngine()
    coordinator = RemoteCoordinator(
        participant, sample_profiles, "dr_only", settings,
        volt_watt=VoltWattCurve(),
    )
    assert coordinator.coordinate(0, engine, 0.25, "ts")
    coordinator.close()

    expected_pv = sample_profiles["buses"][18]["timeseries"][0]["pv_kw"]
    vw_base = expected_pv * VoltWattCurve().factor(1.10)   # 20% floor at 1.10 pu
    assert engine.pv_updates[18] == max(0.0, vw_base - 10.0)
    assert engine.pv_updates[18] < expected_pv - 10.0      # NOT the full base


def test_managed_envelopes_stamped_onto_statuses(sample_profiles):
    """Managed DOE: the coordinator publishes exportLimit_kW with each status."""
    import numpy as np

    participant = FakeParticipant()
    coordinator = RemoteCoordinator(
        participant, sample_profiles, "dr_only", settings,
        envelopes={18: np.array([7.5])},
    )
    coordinator.coordinate(0, FakeEngine(), 0.25, "ts")
    coordinator.close()

    control_req = next(
        p for s, a, p in participant.calls if (s, a) == ("dr-controller", "control")
    )
    readings = control_req["statuses"][0]["payload"]["readings"]
    assert readings["exportLimit_kW"] == 7.5


class _SupportReactiveParticipant(FakeParticipant):
    """Controller commands battery peak support plus a VAr injection."""

    def request(self, service, action, payload, timeout=60.0):
        if (service, action) == ("dr-controller", "control"):
            self.calls.append((service, action, payload))
            return {"status": "ok", "payload": {"controls": [
                {"bus_id": 18, "readings": {
                    "curtailment_kW": 0.0, "evCurtailment_kW": 0.0,
                    "bessCharge_kW": 0.0, "bessDischarge_kW": 6.0,
                    "pvReactive_kVAr": 4.4,
                }}]}}
        return super().request(service, action, payload, timeout)


class _SupportEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.bess_updates = {}
        self.kvar_updates = {}

    def get_bus_voltages_pu(self):
        return {18: 0.92}     # under-voltage: support + injection expected

    def update_bess(self, bus_id, kw):
        self.bess_updates[bus_id] = kw

    def update_pv_reactive(self, bus_id, kvar):
        self.kvar_updates[bus_id] = kvar


def test_support_and_reactive_channels_applied(sample_profiles):
    participant = _SupportReactiveParticipant()
    engine = _SupportEngine()
    coordinator = RemoteCoordinator(participant, sample_profiles, "dr_only", settings)
    assert coordinator.coordinate(0, engine, 0.25, "ts")
    coordinator.close()

    expected_bess = sample_profiles["buses"][18]["timeseries"][0].get("bess_power_kw", 0.0)
    # Discharge support adds on top of the scheduled battery power.
    assert engine.bess_updates[18] == expected_bess + 6.0
    assert engine.kvar_updates[18] == 4.4
    # The support outcome is recorded for the twins.
    record = next(p for s, a, p in participant.calls
                  if (s, a) == ("prosumer-shadow-twins", "record"))
    assert record["controls"]["18"]["support_kw"] == 6.0


def test_remote_coordinator_forwards_twin_config(sample_profiles):
    participant = FakeParticipant()
    RemoteCoordinator(
        participant, sample_profiles, "dr_only", settings,
        twin_config={"min_pv_kw": 5.0, "include_ev_only": False},
    )
    start = next(p for s, a, p in participant.calls if (s, a) == ("prosumer-shadow-twins", "start"))
    assert start["config"] == {"min_pv_kw": 5.0, "include_ev_only": False}


class _CustomSetpointParticipant:
    """Returns a control carrying a custom (non-built-in) setpoint for bus 18."""

    def request(self, service, action, payload, timeout=60.0):
        if (service, action) == ("dr-controller", "configure"):
            return {"status": "ok", "payload": {"mode": payload["mode"], "max_iterations": 1}}
        if (service, action) == ("prosumer-shadow-twins", "start"):
            return {"status": "ok", "payload": {"prosumer_twins": 1}}
        if (service, action) == ("prosumer-shadow-twins", "status"):
            return {"status": "ok", "payload": {"statuses": [
                {"bus_id": 18, "payload": {"mRID": "bus-018-der",
                                           "timestamp": payload["timestamp"],
                                           "readings": {"voltageMagnitude_pu": 1.0,
                                                        "heatpump_kw": 3.0}}}]}}
        if (service, action) == ("dr-controller", "control"):
            return {"status": "ok", "payload": {"controls": [
                {"bus_id": 18, "readings": {"curtailment_kW": 0.0, "evCurtailment_kW": 0.0,
                                            "bessCharge_kW": 0.0, "heatpump_shed_kw": 3.0}}]}}
        if (service, action) == ("prosumer-shadow-twins", "record"):
            return {"status": "ok", "payload": {"summary": {"mode": payload["mode"]}}}
        if action == "stop":
            return {"status": "ok", "payload": {"stopped": True}}
        raise AssertionError(f"Unexpected request {service}/{action}")


class _LoadRecordingEngine:
    def __init__(self):
        self.loads = {}

    def get_bus_voltages_pu(self):
        return {18: 1.0}

    def update_pv(self, *a):
        pass

    def update_ev(self, *a):
        pass

    def update_bess(self, *a):
        pass

    def update_load(self, bus_id, kw, kvar):
        self.loads[bus_id] = (kw, kvar)

    def solve(self):
        return True


def test_custom_setpoint_applied_as_load_reduction(sample_profiles):
    """A custom control-plugin setpoint sheds the bus building load generically."""
    engine = _LoadRecordingEngine()
    coordinator = RemoteCoordinator(
        _CustomSetpointParticipant(), sample_profiles, "dr_only", settings)
    coordinator.coordinate(0, engine, 0.25, "ts")
    # Bus 18 building load is 80 kW (no other_der in the CSV fixture); the 3 kW
    # custom shed reduces it to 77 kW, kvar unchanged.
    assert engine.loads[18] == (77.0, 48.0)
