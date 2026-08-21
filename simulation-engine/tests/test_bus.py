"""Tests for the sim-engine OpenFMB bus participant (in-process transport)."""

from app.bus import BusParticipant, LoopbackTransport, topic_matches
from app.main import _bus_simulate, app, build_registry


def test_topic_matches_wildcards():
    assert topic_matches("openfmb/command/sim-engine/+", "openfmb/command/sim-engine/simulate")
    assert topic_matches("openfmb/event/#", "openfmb/event/sim-engine/simulate")
    assert not topic_matches("openfmb/command/load-engine/+", "openfmb/command/sim-engine/simulate")


def test_command_event_roundtrip():
    t = LoopbackTransport()
    svc = BusParticipant(t, "demo")
    svc.on_command("echo", lambda p: {"n": p["n"] + 1})
    svc.start()
    evt = BusParticipant(t, "client").request("demo", "echo", {"n": 41})
    assert evt["status"] == "ok" and evt["payload"]["n"] == 42


def _wire_sim_engine(solver_bus):
    """Register the simulate handler on the shared loopback bus.

    ``solver_bus`` (conftest) is the sim-engine participant on a transport
    that already carries the real opendss-solver service — the same topology
    as the container stack, in-process.
    """
    app.state.registry = build_registry()
    app.state.bus = solver_bus
    solver_bus.on_command("simulate", _bus_simulate)
    return solver_bus._t


def test_simulate_over_bus(sample_profiles, solver_bus):
    t = _wire_sim_engine(solver_bus)

    client = BusParticipant(t, "orchestrator")
    evt = client.request("sim-engine", "simulate", {
        "scenario_name": "test", "profiles": sample_profiles, "network_id": "ieee33",
        "coordination_mode": "uncoordinated",
    })
    assert evt["status"] == "ok"
    assert evt["payload"]["total_timesteps"] == 2
    assert "kpis" in evt["payload"] and "max_voltage_pu" in evt["payload"]["kpis"]


def test_simulate_over_bus_with_inline_profiles(sample_profiles, solver_bus):
    """Profiles delivered inline over the bus — no shared file path."""
    import json

    # Round-trip through JSON to mimic transport (stringifies bus ids); the sim
    # must coerce them back.
    profiles = json.loads(json.dumps(sample_profiles))
    t = _wire_sim_engine(solver_bus)

    evt = BusParticipant(t, "orchestrator").request("sim-engine", "simulate", {
        "scenario_name": "inline", "network_id": "ieee33", "profiles": profiles,
        "coordination_mode": "uncoordinated",
    })
    assert evt["status"] == "ok"
    assert evt["payload"]["total_timesteps"] == 2
    series = evt["payload"]["result_series"]
    assert series["bus_ids"] and len(series["v_max"]) == 2
    assert len(series["v_by_bus"][str(series["bus_ids"][0])]) == 2


def test_simulate_over_bus_missing_profiles_errors(solver_bus):
    t = _wire_sim_engine(solver_bus)

    # No `profiles` -> the request is invalid; surfaced as an error event.
    evt = BusParticipant(t, "client").request("sim-engine", "simulate", {
        "network_id": "ieee33",
    })
    assert evt["status"] == "error"
