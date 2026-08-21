"""OpenDSS solver service — the power-flow half of the solver bus contract.

The Simulation Engine (or any orchestrator) drives this service over OpenFMB
command/event messages; the contract is deliberately solver-agnostic so an
alternative engine (e.g. a PSS SINCAL adapter) can implement the same actions:

  build    {session_id, network, solve_mode, elements: {type: [bus dicts]}}
           -> compile a fresh circuit for the session
  solve    {session_id, updates: [{op, bus_id, kw?, kvar?}]}
           -> apply the batched element updates, run one power flow
           -> {converged}
  read     {session_id}
           -> full electrical state of the last solve: per-bus voltages (pu),
              per-branch loadings (%), losses kW, source power kW, worst VUF %
  reset    {session_id}   -> recompile the session's circuit (initial state)
  teardown {session_id}   -> drop the session
  health   {}             -> service status

The service is pure power flow: no control logic (inverter responses,
envelopes, DR) lives here — orchestrators iterate solve/read to their own
fixed points. OpenDSS is a process-wide singleton, so one session is active
at a time; a new build replaces the previous session (logged). Each build
writes its OpenDSS files into a per-session working directory.
"""

import logging
import os
import shutil
import threading

from dss_solver.config import settings as default_settings
from dss_solver.dss_model import generate_master_dss, normalize_solve_mode
from dss_solver.elements import element_names, get_element, install_elements
from dss_solver.engine import OpenDSSEngine
from dss_solver.network import SolverNetwork

logger = logging.getLogger(__name__)

VERSION = "5.0.0"
SOLVER_NAME = "opendss"


class SessionError(ValueError):
    """The command names a session this solver is not currently holding."""


class OpenDSSSolverService:
    """Command handlers for the OpenDSS solver container."""

    def __init__(self, settings=default_settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._engine: OpenDSSEngine | None = None
        self._workdir: str | None = None

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
            "service": "opendss-solver",
            "solver": SOLVER_NAME,
            "version": VERSION,
            "elements": element_names(),
            "active_session": self._session_id,
        }

    def build(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("build requires a session_id")
        network_dict = payload.get("network")
        if not isinstance(network_dict, dict):
            raise ValueError("build requires the network model dict")
        solve_mode = normalize_solve_mode(payload.get("solve_mode"))

        with self._lock:
            if self._session_id is not None and self._session_id != session_id:
                logger.info(
                    "Replacing active session %s with %s (OpenDSS holds one "
                    "circuit at a time)", self._session_id, session_id,
                )
            self._drop_session()

            network = SolverNetwork(network_dict)
            workdir = os.path.join(self.settings.DSS_DIR, session_id[:8])
            master = generate_master_dss(network, workdir, solve_mode)
            engine = OpenDSSEngine(master, network)
            counts = install_elements(
                engine, payload.get("elements") or {}, network, workdir, solve_mode
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
            "elements": counts,
        }

    def solve(self, payload: dict) -> dict:
        engine = self._engine_for(payload)
        for op in payload.get("updates") or []:
            element = get_element(str(op.get("op", "")))
            if element is None:
                raise ValueError(
                    f"Unknown update op '{op.get('op')}'. "
                    f"Registered element types: {element_names()}."
                )
            element.apply(engine, op)
        return {"converged": bool(engine.solve())}

    def read(self, payload: dict) -> dict:
        engine = self._engine_for(payload)
        # One message carries the full electrical state: voltages and loadings
        # are keyed by id (JSON stringifies the keys; the client coerces back).
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

    def _engine_for(self, payload: dict) -> OpenDSSEngine:
        session_id = str(payload.get("session_id") or "")
        if self._engine is None or session_id != self._session_id:
            raise SessionError(
                f"No active solver session '{session_id}' "
                f"(active: {self._session_id}). Send build first — a newer "
                f"build may have replaced this session."
            )
        return self._engine

    def _drop_session(self) -> None:
        self._engine = None
        self._session_id = None
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
