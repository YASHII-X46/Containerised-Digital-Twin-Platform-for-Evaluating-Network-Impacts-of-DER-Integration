"""Contract tests for the SINCAL solver adapter (mocked engine, loopback bus)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sincal_solver.bus import BusParticipant, LoopbackTransport, envelope
from sincal_solver.config import Settings
from sincal_solver.engine import SincalUnavailable
from sincal_solver.service import SincalSolverService

NETWORK = {
    "id": "t2", "name": "two-bus", "base_voltage_kv": 11.0, "source_bus": 1,
    "buses": [
        {"bus_id": 1, "base_load_kw": 0.0, "base_load_kvar": 0.0},
        {"bus_id": 2, "base_load_kw": 100.0, "base_load_kvar": 40.0},
    ],
    "branches": [
        {"branch_id": 1, "from_bus": 1, "to_bus": 2,
         "r_ohm": 0.3, "x_ohm": 0.15, "rating_kva": 500},
    ],
}


class FakeEngine:
    """Mock SINCAL engine: records ops, returns canned electrical state."""

    def __init__(self, workdir, network, solve_mode, progid, template="",
                 dbcreate=""):
        self.network = network
        self.ops = []
        self.solves = 0

    def update_load(self, bus_id, kw, kvar):
        self.ops.append(("load", bus_id, kw, kvar))

    def update_pv(self, bus_id, kw):
        self.ops.append(("pv", bus_id, kw))

    def update_pv_reactive(self, bus_id, kvar):
        self.ops.append(("pv_q", bus_id, kvar))

    def update_bess(self, bus_id, kw):
        self.ops.append(("bess", bus_id, kw))

    def update_ev(self, bus_id, kw):
        self.ops.append(("ev", bus_id, kw))

    def solve(self):
        self.solves += 1
        return True

    def get_bus_voltages_pu(self):
        return {b: 1.0 for b in self.network.bus_ids}

    def get_branch_loadings_pct(self):
        return {1: 42.0}

    def get_total_losses_kw(self):
        return 1.5

    def get_total_power_kw(self):
        return 101.5

    def get_max_vuf_pct(self):
        return 0.0

    def reset(self):
        self.ops.clear()


def _service_on_bus(engine_factory=FakeEngine):
    transport = LoopbackTransport()
    participant = BusParticipant(transport, service="sincal-solver")
    # Stub the environment probe: tests must not start a real COM server,
    # which is slow and would make the suite depend on a licensed install.
    service = SincalSolverService(
        Settings(), engine_factory=engine_factory,
        env_probe=lambda _p: {"sincal_available": False, "detail": "stubbed in tests"},
    )
    service.register(participant)
    participant.start()
    return transport, service


def _command(transport, action, payload):
    """Publish a command and capture the correlated reply event."""
    replies = []
    transport.subscribe(f"openfmb/event/sincal-solver/{action}",
                        lambda _t, m: replies.append(m))
    transport.publish(f"openfmb/command/sincal-solver/{action}",
                      envelope(payload, "corr-1"))
    assert replies, f"no reply event for {action}"
    return replies[-1]


def test_full_contract_round_trip():
    transport, service = _service_on_bus()

    built = _command(transport, "build", {
        "session_id": "s1", "network": NETWORK, "solve_mode": "balanced",
        "elements": {"pv": [{"bus_id": 2, "pv_capacity_kw": 5.0}]},
    })
    assert built["status"] == "ok"
    assert built["payload"]["buses"] == 2
    assert built["payload"]["elements"] == {"pv": 1}

    solved = _command(transport, "solve", {
        "session_id": "s1",
        "updates": [
            {"op": "load", "bus_id": 2, "kw": 80.0, "kvar": 30.0},
            {"op": "pv", "bus_id": 2, "kw": 4.0},
        ],
    })
    assert solved["status"] == "ok" and solved["payload"]["converged"] is True
    assert service._engine.ops == [("load", 2, 80.0, 30.0), ("pv", 2, 4.0)]

    read = _command(transport, "read", {"session_id": "s1"})
    assert read["payload"]["voltages"] == {"1": 1.0, "2": 1.0}
    assert read["payload"]["loadings"] == {"1": 42.0}
    assert read["payload"]["losses_kw"] == 1.5

    closed = _command(transport, "teardown", {"session_id": "s1"})
    assert closed["payload"]["existed"] is True


def test_unknown_session_is_error_event():
    transport, _ = _service_on_bus()
    reply = _command(transport, "solve", {"session_id": "nope", "updates": []})
    assert reply["status"] == "error"
    assert "No active solver session" in reply["payload"]["error"]


def test_unknown_op_is_error_event():
    transport, _ = _service_on_bus()
    _command(transport, "build", {"session_id": "s1", "network": NETWORK})
    reply = _command(transport, "solve", {
        "session_id": "s1", "updates": [{"op": "flux_capacitor", "bus_id": 2}],
    })
    assert reply["status"] == "error"
    assert "Unknown update op" in reply["payload"]["error"]


def test_missing_sincal_surfaces_cleanly_on_build():
    def unavailable_factory(workdir, network, solve_mode, progid, template="",
                            dbcreate=""):
        raise SincalUnavailable("pywin32 is not installed")

    transport, _ = _service_on_bus(engine_factory=unavailable_factory)
    reply = _command(transport, "build", {"session_id": "s1", "network": NETWORK})
    assert reply["status"] == "error"
    assert "pywin32" in reply["payload"]["error"]


def test_health_reports_probe_without_sincal():
    transport, _ = _service_on_bus()
    reply = _command(transport, "health", {})
    assert reply["status"] == "ok"
    assert reply["payload"]["solver"] == "sincal"
    assert "sincal_available" in reply["payload"]
