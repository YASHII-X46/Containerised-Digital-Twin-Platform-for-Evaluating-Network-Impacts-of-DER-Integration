"""PSS SINCAL solver service — the same solver bus contract as opendss-solver.

The Simulation Engine selects a solver by name; ``solver: "sincal"`` routes
build/solve/read/reset/teardown commands here instead of the OpenDSS
container. The handlers are contract-identical; only the engine behind them
differs. When PSS SINCAL is not available on this machine, ``build`` fails
with a clear error event (and ``health`` reports the probe result) rather
than pretending to solve.
"""

import logging
import os
import shutil
import threading

from sincal_solver.config import settings as default_settings
from sincal_solver.engine import SincalEngine, check_environment
from sincal_solver.network import SolverNetwork

logger = logging.getLogger(__name__)

VERSION = "5.0.0"
SOLVER_NAME = "sincal"

# Per-step update ops -> engine calls (the solver bus contract's op names).
_OPS = {
    "load": lambda e, op: e.update_load(
        int(op["bus_id"]), float(op.get("kw", 0.0)), float(op.get("kvar", 0.0))),
    "pv": lambda e, op: e.update_pv(int(op["bus_id"]), float(op.get("kw", 0.0))),
    "pv_q": lambda e, op: e.update_pv_reactive(
        int(op["bus_id"]), float(op.get("kvar", 0.0))),
    "bess": lambda e, op: e.update_bess(int(op["bus_id"]), float(op.get("kw", 0.0))),
    "ev": lambda e, op: e.update_ev(int(op["bus_id"]), float(op.get("kw", 0.0))),
}


class SessionError(ValueError):
    """The command names a session this solver is not currently holding."""


class SincalSolverService:
    """Command handlers for the PSS SINCAL solver adapter."""

    def __init__(self, settings=default_settings, engine_factory=SincalEngine,
                 env_probe=check_environment):
        self.settings = settings
        self._engine_factory = engine_factory  # injectable for tests
        # Injectable so tests never start a real COM server.
        self._env_probe_fn = env_probe
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._engine = None
        self._workdir: str | None = None
        # Starting the SINCAL COM server costs seconds and is apartment-bound,
        # so probe once and reuse the answer. main() calls health() on the main
        # thread at startup, which primes this before any bus command lands.
        self._env_probe: dict | None = None

    def _environment(self) -> dict:
        if self._env_probe is None:
            self._env_probe = self._env_probe_fn(self.settings.SINCAL_PROGID)
        return self._env_probe

    def register(self, participant) -> None:
        participant.on_command("health", self.health)
        participant.on_command("build", self.build)
        participant.on_command("solve", self.solve)
        participant.on_command("read", self.read)
        participant.on_command("reset", self.reset)
        participant.on_command("teardown", self.teardown)

    # ---- handlers ---------------------------------------------------------

    def health(self, payload: dict) -> dict:
        return {
            "status": "ok",
            "service": "sincal-solver",
            "solver": SOLVER_NAME,
            "version": VERSION,
            "active_session": self._session_id,
            **self._environment(),
        }

    def build(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("build requires a session_id")
        network_dict = payload.get("network")
        if not isinstance(network_dict, dict):
            raise ValueError("build requires the network model dict")
        solve_mode = str(payload.get("solve_mode") or "balanced").lower()

        with self._lock:
            if self._session_id is not None and self._session_id != session_id:
                logger.info("Replacing active session %s with %s",
                            self._session_id, session_id)
            self._drop_session()

            network = SolverNetwork(network_dict)
            workdir = os.path.join(self.settings.SINCAL_WORK_DIR, session_id[:8])
            engine = self._engine_factory(
                workdir, network, solve_mode, self.settings.SINCAL_PROGID,
                self.settings.SINCAL_TEMPLATE, self.settings.SINCAL_DBCREATE,
            )

            self._session_id = session_id
            self._engine = engine
            self._workdir = workdir

        return {
            "status": "built",
            "session_id": session_id,
            "solver": SOLVER_NAME,
            "network_id": network.id,
            "solve_mode": solve_mode,
            "buses": len(network.bus_ids),
            # Element bus lists are recorded in the exported model; counts
            # echo what the orchestrator sent.
            "elements": {k: len(v) for k, v in (payload.get("elements") or {}).items()},
        }

    def solve(self, payload: dict) -> dict:
        engine = self._engine_for(payload)
        for op in payload.get("updates") or []:
            apply = _OPS.get(str(op.get("op", "")))
            if apply is None:
                raise ValueError(
                    f"Unknown update op '{op.get('op')}'. "
                    f"Supported: {sorted(_OPS)}."
                )
            apply(engine, op)
        return {"converged": bool(engine.solve())}

    def read(self, payload: dict) -> dict:
        engine = self._engine_for(payload)
        return {
            "voltages": {str(b): v for b, v in engine.get_bus_voltages_pu().items()},
            "loadings": {str(b): v for b, v in engine.get_branch_loadings_pct().items()},
            "losses_kw": engine.get_total_losses_kw(),
            "power_kw": engine.get_total_power_kw(),
            "max_vuf_pct": engine.get_max_vuf_pct(),
        }

    def reset(self, payload: dict) -> dict:
        self._engine_for(payload).reset()
        return {"status": "reset", "session_id": self._session_id}

    def teardown(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "")
        with self._lock:
            existed = session_id == self._session_id
            if existed:
                self._drop_session()
        return {"status": "closed", "session_id": session_id, "existed": existed}

    # ---- internals ---------------------------------------------------------

    def _engine_for(self, payload: dict):
        session_id = str(payload.get("session_id") or "")
        if self._engine is None or session_id != self._session_id:
            raise SessionError(
                f"No active solver session '{session_id}' "
                f"(active: {self._session_id}). Send build first."
            )
        return self._engine

    def _drop_session(self) -> None:
        self._engine = None
        self._session_id = None
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
