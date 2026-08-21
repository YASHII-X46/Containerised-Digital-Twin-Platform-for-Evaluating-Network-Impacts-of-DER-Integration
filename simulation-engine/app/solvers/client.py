"""Remote power-flow engine — the solver bus contract, presented as the
engine interface the QSTS loop, envelope computation, inverter control, and
DR coordination were written against.

The client buffers element updates locally and flushes them with the next
``solve()`` (one bus message per solve, not per element), and fetches the full
electrical state in one ``read`` request that is cached until the next solve.
A QSTS fixed point (Volt-VAr/Volt-Watt, DR re-solves) therefore costs two bus
round trips per iteration regardless of network size.

Unlike ``BusParticipant.request()`` (one throwaway subscription per call),
this client holds a single event subscription for the whole session and
dispatches replies by correlationId — thousands of solver calls per run leave
no subscription trail.
"""

import logging
import threading
import uuid

logger = logging.getLogger(__name__)


class SolverError(RuntimeError):
    """The solver service reported an error for a command."""


class SolverTimeout(SolverError):
    """No reply from the solver service within the timeout."""


class RemoteSolverEngine:
    """Engine-interface adapter over a remote solver bus service."""

    def __init__(self, participant, service: str, network: dict,
                 element_buses: dict[str, list[dict]], solve_mode: str,
                 timeout: float = 120.0):
        self._p = participant
        self._service = service
        self._timeout = timeout
        self._session = str(uuid.uuid4())
        self._pending: list[dict] = []
        self._read_cache: dict | None = None
        self._waiters: dict[str, dict] = {}
        self._closed = False

        # One subscription serves every reply for this session.
        self._p._t.subscribe(
            f"{self._p.prefix}/event/{service}/+", self._on_event
        )
        self.build_info = self._call("build", {
            "network": network,
            "solve_mode": solve_mode,
            "elements": element_buses,
        })

    # ---- update buffer (flushed with the next solve) -----------------------

    def update_load(self, bus_id: int, kw: float, kvar: float) -> None:
        self._pending.append({"op": "load", "bus_id": bus_id, "kw": kw, "kvar": kvar})

    def update_pv(self, bus_id: int, kw: float) -> None:
        self._pending.append({"op": "pv", "bus_id": bus_id, "kw": kw})

    def update_pv_reactive(self, bus_id: int, kvar: float) -> None:
        self._pending.append({"op": "pv_q", "bus_id": bus_id, "kvar": kvar})

    def update_bess(self, bus_id: int, kw: float) -> None:
        self._pending.append({"op": "bess", "bus_id": bus_id, "kw": kw})

    def update_ev(self, bus_id: int, kw: float) -> None:
        self._pending.append({"op": "ev", "bus_id": bus_id, "kw": kw})

    def update_element(self, op: str, bus_id: int, **values) -> None:
        """Generic update for a custom solver-side DER element type."""
        self._pending.append({"op": op, "bus_id": bus_id, **values})

    # ---- solve / read -------------------------------------------------------

    def solve(self) -> bool:
        updates, self._pending = self._pending, []
        self._read_cache = None
        result = self._call("solve", {"updates": updates})
        return bool(result.get("converged"))

    def _read(self) -> dict:
        if self._read_cache is None:
            self._read_cache = self._call("read", {})
        return self._read_cache

    def get_bus_voltages_pu(self) -> dict[int, float]:
        return {int(b): float(v) for b, v in self._read()["voltages"].items()}

    def get_branch_loadings_pct(self) -> dict[int, float]:
        return {int(b): float(v) for b, v in self._read()["loadings"].items()}

    def get_total_losses_kw(self) -> float:
        return float(self._read()["losses_kw"])

    def get_total_power_kw(self) -> float:
        return float(self._read()["power_kw"])

    def get_max_vuf_pct(self) -> float:
        return float(self._read()["max_vuf_pct"])

    # ---- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        self._pending.clear()
        self._read_cache = None
        self._call("reset", {})

    def close(self) -> None:
        """Release the solver session (best-effort; safe to call twice)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._call("teardown", {}, timeout=10.0)
        except SolverError as exc:
            logger.warning("Solver teardown failed (ignored): %s", exc)

    # ---- bus plumbing --------------------------------------------------------

    def _on_event(self, _topic: str, msg: dict) -> None:
        waiter = self._waiters.pop(msg.get("correlationId", ""), None)
        if waiter is not None:
            waiter["msg"] = msg
            waiter["done"].set()

    def _call(self, action: str, payload: dict, timeout: float | None = None) -> dict:
        corr = str(uuid.uuid4())
        waiter = {"done": threading.Event(), "msg": None}
        self._waiters[corr] = waiter
        from app.bus import envelope
        self._p._t.publish(
            f"{self._p.prefix}/command/{self._service}/{action}",
            envelope({"session_id": self._session, **payload}, corr),
        )
        if not waiter["done"].wait(timeout or self._timeout):
            self._waiters.pop(corr, None)
            raise SolverTimeout(
                f"No reply from solver service '{self._service}' for "
                f"'{action}' within {timeout or self._timeout:.0f}s — is the "
                f"solver container running and on the bus?"
            )
        msg = waiter["msg"]
        if msg.get("status") != "ok":
            detail = (msg.get("payload") or {}).get("error") or msg.get("payload")
            raise SolverError(f"{self._service}/{action}: {detail}")
        return msg.get("payload") or {}
