"""Tests for the OpenFMB command/event bus participant (in-process transport)."""

from app.bus import (
    BusParticipant,
    LoopbackTransport,
    NatsTransport,
    make_transport,
    topic_matches,
)
from app.bus.transport import _filter_to_subject, _topic_to_subject
from app.config import settings
from tests.ieee33_data import IEEE33_NETWORK_BUSES as NB


def test_nats_subject_translation():
    assert _topic_to_subject("openfmb/command/load-engine/generate") == \
        "openfmb.command.load-engine.generate"
    assert _filter_to_subject("openfmb/command/load-engine/+") == \
        "openfmb.command.load-engine.*"
    assert _filter_to_subject("openfmb/event/#") == "openfmb.event.>"


def test_make_transport_selects_kind():
    # Constructors don't open connections, so this needs no broker.
    assert isinstance(make_transport("loopback", settings, "x"), LoopbackTransport)
    assert isinstance(make_transport("nats", settings, "x"), NatsTransport)


def test_topic_matches_wildcards():
    assert topic_matches("openfmb/command/load-engine/+", "openfmb/command/load-engine/generate")
    assert topic_matches("openfmb/event/#", "openfmb/event/load-engine/generate")
    assert not topic_matches("openfmb/command/sim-engine/+", "openfmb/command/load-engine/generate")
    assert not topic_matches("a/b/+", "a/b/c/d")


def test_command_event_roundtrip_over_loopback():
    t = LoopbackTransport()
    svc = BusParticipant(t, "demo")
    svc.on_command("echo", lambda payload: {"echoed": payload["x"] * 2})
    svc.start()

    client = BusParticipant(t, "client")
    evt = client.request("demo", "echo", {"x": 21})
    assert evt["status"] == "ok"
    assert evt["payload"]["echoed"] == 42
    assert evt["correlationId"]  # correlation propagated


def test_handler_error_becomes_error_event():
    t = LoopbackTransport()
    svc = BusParticipant(t, "demo")
    svc.on_command("boom", lambda payload: (_ for _ in ()).throw(ValueError("nope")))
    svc.start()
    evt = BusParticipant(t, "client").request("demo", "boom", {})
    assert evt["status"] == "error"
    assert "nope" in evt["payload"]["error"]


def test_load_engine_generate_over_bus():
    from app.main import _bus_generate

    t = LoopbackTransport()
    engine = BusParticipant(t, "load-engine")
    engine.on_command("generate", _bus_generate)
    engine.start()

    client = BusParticipant(t, "orchestrator")
    evt = client.request(
        "load-engine", "generate",
        {"seed": 42, "network_buses": NB, "export_csv": False},
    )
    assert evt["status"] == "ok"
    assert evt["payload"]["total_buses"] == 33
    assert evt["payload"]["timesteps"] == 96


def test_generate_event_carries_profiles_payload():
    """The generate event includes the full profiles, so no shared CSV is needed."""
    from app.main import _bus_generate

    t = LoopbackTransport()
    engine = BusParticipant(t, "load-engine")
    engine.on_command("generate", _bus_generate)
    engine.start()

    evt = BusParticipant(t, "orchestrator").request(
        "load-engine", "generate",
        {"seed": 42, "network_buses": NB, "export_csv": False},
    )
    assert evt["status"] == "ok"
    profiles = evt["payload"]["profiles"]
    assert set(profiles) == {"metadata", "buses"}
    assert profiles["metadata"]["timesteps"] == 96
    # A representative bus carries its full time series in the wire format.
    any_bus = next(iter(profiles["buses"].values()))
    assert {"customer_class", "base_load_kw", "timeseries"} <= set(any_bus)
    assert len(any_bus["timeseries"]) == 96
    assert {"pv_kw", "net_load_kw", "ev_charge_kw"} <= set(any_bus["timeseries"][0])


def test_load_engine_generate_bad_request_errors_over_bus():
    from app.main import _bus_generate

    t = LoopbackTransport()
    engine = BusParticipant(t, "load-engine")
    engine.on_command("generate", _bus_generate)
    engine.start()

    evt = BusParticipant(t, "client").request(
        "load-engine", "generate",
        {"der_penetration_percent": 600, "export_csv": False},
    )
    assert evt["status"] == "error"
