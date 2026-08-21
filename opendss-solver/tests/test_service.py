"""Contract tests: the solver service over the loopback bus with real OpenDSS."""

import json
import os

from dss_solver.bus import BusParticipant, LoopbackTransport, envelope
from dss_solver.config import Settings
from dss_solver.service import OpenDSSSolverService

TESTS_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _ieee33_dict() -> dict:
    with open(os.path.join(TESTS_DATA_DIR, "ieee33.json"), encoding="utf-8") as f:
        return json.load(f)


def _service_on_bus(tmp_path):
    transport = LoopbackTransport()
    participant = BusParticipant(transport, service="opendss-solver")
    service = OpenDSSSolverService(Settings(DSS_DIR=str(tmp_path)))
    service.register(participant)
    participant.start()
    return transport


def _command(transport, action, payload):
    replies = []
    transport.subscribe(f"openfmb/event/opendss-solver/{action}",
                        lambda _t, m: replies.append(m))
    transport.publish(f"openfmb/command/opendss-solver/{action}",
                      envelope(payload, f"corr-{action}-{len(replies)}"))
    assert replies, f"no reply event for {action}"
    return replies[-1]


def test_contract_build_solve_read_teardown(tmp_path):
    transport = _service_on_bus(tmp_path)

    built = _command(transport, "build", {
        "session_id": "s1", "network": _ieee33_dict(), "solve_mode": "balanced",
        "elements": {"pv": [{"bus_id": 18, "pv_capacity_kw": 50.0}]},
    })
    assert built["status"] == "ok", built
    assert built["payload"]["buses"] == 33
    assert built["payload"]["elements"]["pv"] == 1

    solved = _command(transport, "solve", {
        "session_id": "s1",
        "updates": [
            {"op": "load", "bus_id": 2, "kw": 100.0, "kvar": 60.0},
            {"op": "pv", "bus_id": 18, "kw": 30.0},
        ],
    })
    assert solved["status"] == "ok" and solved["payload"]["converged"] is True

    read = _command(transport, "read", {"session_id": "s1"})
    voltages = read["payload"]["voltages"]
    assert len(voltages) == 33
    assert abs(float(voltages["1"]) - 1.0) < 0.02
    assert len(read["payload"]["loadings"]) == 32
    assert read["payload"]["losses_kw"] >= 0.0

    reset = _command(transport, "reset", {"session_id": "s1"})
    assert reset["status"] == "ok"

    closed = _command(transport, "teardown", {"session_id": "s1"})
    assert closed["payload"]["existed"] is True


def test_stale_session_is_error(tmp_path):
    transport = _service_on_bus(tmp_path)
    _command(transport, "build", {"session_id": "a", "network": _ieee33_dict()})
    # A newer build replaces the session; the old id is refused thereafter.
    _command(transport, "build", {"session_id": "b", "network": _ieee33_dict()})
    reply = _command(transport, "solve", {"session_id": "a", "updates": []})
    assert reply["status"] == "error"
    assert "No active solver session" in reply["payload"]["error"]


def test_unknown_element_type_skipped_with_count(tmp_path):
    transport = _service_on_bus(tmp_path)
    built = _command(transport, "build", {
        "session_id": "s1", "network": _ieee33_dict(),
        "elements": {"hydrogen_turbine": [{"bus_id": 5}]},
    })
    # Unknown types are logged and skipped, never a silent drop or a crash.
    assert built["status"] == "ok"
    assert "hydrogen_turbine" not in built["payload"]["elements"]
