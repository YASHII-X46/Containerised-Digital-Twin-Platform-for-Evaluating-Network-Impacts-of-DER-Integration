"""Tests for the standalone prosumer shadow-twins service."""

from app.bus import BusParticipant, LoopbackTransport
from app.config import TwinConfig, settings
from app.main import ProsumerShadowTwinsService
from app.shadow_twin import ProsumerShadowTwin, build_shadow_twins


def sample_profiles():
    row = {
        "pv_kw": 30.0,
        "ev_charge_kw": 7.0,
        "bess_soc": 0.5,
        "bess_power_kw": 0.0,
    }
    return {
        "metadata": {"timesteps": 1, "resolution_minutes": 15},
        "buses": {
            18: {
                "pv_capacity_kw": 50.0,
                "bess_capacity_kwh": 13.5,
                "ev_charge_rate_kw": 0.0,
                "timeseries": [dict(row)],
            },
            22: {
                "pv_capacity_kw": 50.0,
                "bess_capacity_kwh": 0.0,
                "ev_charge_rate_kw": 0.0,
                "timeseries": [dict(row)],
            },
            25: {
                "pv_capacity_kw": 0.0,
                "bess_capacity_kwh": 0.0,
                "ev_charge_rate_kw": 7.0,
                "timeseries": [dict(row)],
            },
        },
    }


def test_build_includes_pv_bess_and_ev_buses():
    twins = build_shadow_twins(sample_profiles())
    assert set(twins) == {18, 22, 25}


def test_pv_threshold_excludes_small_buses():
    # Buses 18 and 22 have 50 kW PV; require more than that to qualify on PV.
    config = TwinConfig(min_pv_kw=60.0)
    twins = build_shadow_twins(sample_profiles(), config)
    # 18 still qualifies via its 13.5 kWh BESS; 22 (PV-only) drops out;
    # 25 stays as an EV-only twin (include_ev_only defaults True).
    assert set(twins) == {18, 25}


def test_include_ev_only_false_drops_ev_only_buses():
    config = TwinConfig(include_ev_only=False)
    twins = build_shadow_twins(sample_profiles(), config)
    assert set(twins) == {18, 22}  # EV-only bus 25 excluded


def test_twin_config_merged_applies_overrides():
    base = TwinConfig.from_settings(settings)
    merged = base.merged({"min_pv_kw": 10.0, "include_ev_only": False, "unknown": 1})
    assert merged.min_pv_kw == 10.0
    assert merged.include_ev_only is False
    assert merged.min_bess_kwh == base.min_bess_kwh  # untouched


def test_record_accumulates_bess_support():
    twin = ProsumerShadowTwin(
        18, pv_capacity_kw=50.0, bess_capacity_kwh=13.5, ev_charge_rate_kw=0.0,
        timeseries=[{"pv_kw": 0.0, "ev_charge_kw": 0.0, "bess_soc": 0.6}],
    )
    twin.record(0, 0.94, 0.25, support_kw=12.0)
    twin.record(0, 0.95, 0.25, support_kw=8.0)
    assert twin.summary()["bess_support_kwh"] == 5.0   # (12 + 8) x 0.25 h


def test_status_message_carries_expected_state():
    twin = ProsumerShadowTwin(
        18,
        pv_capacity_kw=50.0,
        bess_capacity_kwh=13.5,
        ev_charge_rate_kw=7.0,
        timeseries=[{"pv_kw": 30.0, "ev_charge_kw": 7.0, "bess_soc": 0.5}],
    )
    topic, payload = twin.status_message(0, 1.06, 0.25, "ts")
    assert topic == "openfmb/DERStatusProfile/bus-018-der"
    assert payload["readings"]["pvOutput_kW"] == 30.0
    assert payload["readings"]["voltageMagnitude_pu"] == 1.06


def test_status_message_forwards_extra_der_readings():
    """Extra DER series (e.g. a heat-pump plugin) reach the controller by name."""
    twin = ProsumerShadowTwin(
        18, pv_capacity_kw=50.0, bess_capacity_kwh=13.5, ev_charge_rate_kw=0.0,
        timeseries=[{
            "pv_kw": 30.0, "ev_charge_kw": 0.0, "bess_soc": 0.5,
            "load_kw": 80.0, "other_der_kw": 2.0, "heatpump_kw": 2.0,
        }],
    )
    _topic, payload = twin.status_message(0, 1.02, 0.25, "ts")
    readings = payload["readings"]
    assert readings["heatpump_kw"] == 2.0   # extra series forwarded verbatim
    assert "load_kw" not in readings        # built-in/bookkeeping keys not leaked
    # Site demand (load + extra-DER) backs export-limit (envelope) control.
    assert readings["loadDemand_kW"] == 82.0
    assert "other_der_kw" not in readings


def test_record_tracks_curtailed_deferred_shared():
    twin = ProsumerShadowTwin(18, 50.0, 13.5, 7.0, [{"bess_soc": 0.5}])
    twin.record(0, 1.06, 0.25, curtailed_pv_kw=4.0, deferred_ev_kw=2.0,
                shared_kw=1.0, other_shed_kw=8.0)
    summary = twin.summary()
    assert abs(summary["curtailed_pv_kwh"] - 1.0) < 1e-9
    assert abs(summary["deferred_ev_kwh"] - 0.5) < 1e-9
    assert abs(summary["shared_pv_kwh"] - 0.25) < 1e-9
    assert abs(summary["other_shed_kwh"] - 2.0) < 1e-9


def test_shadow_twins_service_command_roundtrip():
    transport = LoopbackTransport()
    service_participant = BusParticipant(transport, "prosumer-shadow-twins")
    ProsumerShadowTwinsService(settings).register(service_participant)
    service_participant.start()

    client = BusParticipant(transport, "client")
    start = client.request("prosumer-shadow-twins", "start", {
        "session_id": "s1",
        "profiles": sample_profiles(),
        "mode": "dr_only",
    })
    assert start["status"] == "ok"
    assert start["payload"]["prosumer_twins"] == 3

    status = client.request("prosumer-shadow-twins", "status", {
        "session_id": "s1",
        "timestep": 0,
        "voltages": {18: 1.10, 22: 1.0, 25: 1.0},
        "step_hours": 0.25,
        "timestamp": "ts",
    })
    assert len(status["payload"]["statuses"]) == 3
    assert status["payload"]["statuses"][0]["topic"].startswith("openfmb/DERStatusProfile/")

    record = client.request("prosumer-shadow-twins", "record", {
        "session_id": "s1",
        "mode": "dr_only",
        "timestep": 0,
        "final_voltages": {18: 1.04, 22: 1.0, 25: 1.0},
        "step_hours": 0.25,
        "controls": {
            "18": {
                "curtailed_pv_kw": 10.0,
                "deferred_ev_kw": 0.0,
                "shared_kw": 0.0,
                "other_shed_kw": 4.0,
            }
        },
    })
    summary = record["payload"]["summary"]
    assert summary["mode"] == "dr_only"
    assert summary["buses_curtailed"] == 1
    assert summary["total_pv_curtailed_kwh"] == 2.5
    assert summary["buses_shed"] == 1
    assert summary["total_other_shed_kwh"] == 1.0  # 4 kW × 0.25 h


def test_start_honours_per_session_config_override():
    transport = LoopbackTransport()
    service_participant = BusParticipant(transport, "prosumer-shadow-twins")
    ProsumerShadowTwinsService(settings).register(service_participant)
    service_participant.start()

    client = BusParticipant(transport, "client")
    start = client.request("prosumer-shadow-twins", "start", {
        "session_id": "s2",
        "profiles": sample_profiles(),
        "mode": "dr_only",
        "config": {"include_ev_only": False},
    })
    assert start["status"] == "ok"
    # EV-only bus 25 is excluded by the per-session override.
    assert start["payload"]["prosumer_twins"] == 2
    assert start["payload"]["bus_ids"] == [18, 22]
    assert start["payload"]["config"]["include_ev_only"] is False


def test_config_command_returns_defaults():
    transport = LoopbackTransport()
    service_participant = BusParticipant(transport, "prosumer-shadow-twins")
    ProsumerShadowTwinsService(settings).register(service_participant)
    service_participant.start()

    client = BusParticipant(transport, "client")
    resp = client.request("prosumer-shadow-twins", "config", {})
    assert resp["status"] == "ok"
    cfg = resp["payload"]["config"]
    assert "min_pv_kw" in cfg and "include_ev_only" in cfg
