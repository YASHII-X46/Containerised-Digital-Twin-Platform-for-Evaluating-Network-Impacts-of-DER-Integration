"""PSS SINCAL power-flow engine adapter (COM automation).

PSS SINCAL is licensed, Windows-only Siemens software driven through its COM
automation interface: a network lives in a SINCAL project database
(Access/SQL Server), and the ``Sincal.Simulation`` COM server runs load flow
(``LF``) against it. This adapter presents that workflow behind the same
engine interface the OpenDSS engine implements, so the sincal-solver service
can serve the identical solver bus contract.

The per-session project is created by SINCAL's own ``SinDBCreate.exe``, so no
project template has to be committed to this repository; where a site prefers
an existing project, ``SINCAL_TEMPLATE`` still clones one. Either way the model
is written in by ``sincal_schema``, the module the offline generator in
sample-networks uses, so one mapping serves both and cannot drift.

Results are read back out of the project's own SQLite tables (``LFNodeResult``,
``LFBranchResult``, ``LFPowDataResult``) rather than through COM result
navigation, because those table and column names are verified against PSS
SINCAL Platform 22.5 and are what the offline audit reads too.

Solve mode: ``"unbalanced"`` writes an unbalanced project (per-phase load
terminals, earthed transformer star points, and the unbalanced power flow
procedure with a four-conductor zero-sequence network) and reads the per-phase
result tables; anything else writes the symmetric project. The choice is made
once, when the project is written, because it is a property of the project's
calculation settings rather than of the ``Start`` call. See
SINCAL-SCHEMA-NOTES.md.
"""

import logging
import math
import os

from sincal_solver import sincal_schema

logger = logging.getLogger(__name__)

# SINCAL's own empty-project creator. Overridden by SINCAL_DBCREATE.
DEFAULT_DBCREATE = (
    r"C:\Program Files\Siemens\PSS SINCAL Platform 22.5\Bin\SinDBCreate.exe")

# Simulation.StatusID after a successful run. 1102 is "finished with errors".
STATUS_OK = 1101


class SincalUnavailable(RuntimeError):
    """PSS SINCAL (or pywin32) is not available where this adapter runs."""


def progid_candidates(configured: str = "") -> list[str]:
    """COM ProgIDs to try, most specific first.

    SINCAL registers a version-suffixed ProgID (``Sincal.Simulation.28`` for
    the 22.5 platform) and, on a desktop install, usually the unsuffixed
    ``Sincal.Simulation`` as well. A container image built from the installer
    may register only the suffixed one, so probe both rather than assuming.
    """
    out = [configured] if configured else []
    out += ["Sincal.Simulation.28", "Sincal.Simulation"]
    seen, ordered = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _dispatch(progid: str = ""):
    """Start the SINCAL COM server on the calling thread.

    COM is apartment-bound, so the thread that dispatches must also make the
    subsequent calls. The service therefore probes once from its main thread at
    start-up and caches the answer, and never dispatches from a bus handler
    thread. See the threading note in the module docstring.
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise SincalUnavailable(
            "pywin32 is not installed. The SINCAL adapter drives PSS SINCAL "
            "through COM and must run on Windows with pywin32 available."
        ) from exc
    pythoncom.CoInitialize()
    tried = []
    for candidate in progid_candidates(progid):
        try:
            server = win32com.client.Dispatch(candidate)
            logger.info("PSS SINCAL COM server '%s' started", candidate)
            return server
        except Exception as exc:  # noqa: BLE001 - COM errors are environmental
            tried.append(f"{candidate}: {exc}")
    raise SincalUnavailable(
        "Could not start a PSS SINCAL COM server. A licensed PSS SINCAL "
        "installation must be present where this adapter runs (the adapter "
        "cannot ship SINCAL: it is proprietary, Windows-only, licensed "
        "software). Tried " + "; ".join(tried)
    )


class SincalEngine:
    """PSS SINCAL load-flow engine for a network-model-described feeder."""

    def __init__(self, workdir: str, network, solve_mode: str,
                 progid: str = "Sincal.Simulation", template: str = "",
                 dbcreate: str = DEFAULT_DBCREATE):
        self._network = network
        self._solve_mode = solve_mode
        self._workdir = workdir
        self._template = template
        self._dbcreate = dbcreate or DEFAULT_DBCREATE
        self._unbalanced = str(solve_mode or "").strip().lower() == "unbalanced"
        os.makedirs(workdir, exist_ok=True)
        self._db_path = os.path.join(workdir, f"{network.id}.sin")
        self._sqlite_path = ""
        self._sim = _dispatch(progid)
        self._export_network()
        self._bind_database()
        # Pending element states applied at the next solve (SINCAL updates go
        # through the project database rather than an in-memory circuit).
        self._element_state: dict[tuple[str, int], dict] = {}

    # ---- model construction ------------------------------------------------

    def _export_network(self) -> None:
        """Create a fresh SINCAL project and write the network model into it.

        Two ways to get the empty project, in order:

          1. ``SINCAL_TEMPLATE`` — clone an existing ``.sin`` and its
             ``<name>_files`` folder. Use this when a site has a project with
             house settings already applied.
          2. ``SinDBCreate.exe`` — SINCAL's own project creator, run as
             ``/DBSYS:SQLITE /TYPE:E``. This needs no committed artefact and is
             the default.

        The rows themselves come from ``sincal_schema.export_network``, which
        filters every write against the columns the installed release actually
        has.
        """
        import shutil
        import sqlite3
        import subprocess

        stem = self._network.id
        # SinDBCreate rejects a path that mixes separators (exit 14), and the
        # workdir arrives from configuration, so normalise before using it.
        workdir = os.path.normpath(os.path.abspath(self._workdir))
        dst_sin = os.path.join(workdir, f"{stem}.sin")
        dst_files = os.path.join(workdir, f"{stem}_files")
        db_path = os.path.join(dst_files, "database.db")
        if os.path.isdir(dst_files):
            shutil.rmtree(dst_files)
        if os.path.exists(dst_sin):
            os.remove(dst_sin)
        os.makedirs(dst_files, exist_ok=True)

        if self._template and os.path.isfile(self._template):
            src_stem = os.path.splitext(os.path.basename(self._template))[0]
            src_db = os.path.join(os.path.dirname(self._template),
                                  f"{src_stem}_files", "database.db")
            if not os.path.isfile(src_db):
                raise SincalUnavailable(
                    f"SINCAL_TEMPLATE '{self._template}' has no "
                    f"'{src_stem}_files/database.db' beside it; a .sin project "
                    "is the pair, not the file alone."
                )
            shutil.copyfile(self._template, dst_sin)
            shutil.copyfile(src_db, db_path)
        elif os.path.isfile(self._dbcreate):
            done = subprocess.run(
                [self._dbcreate, "/DBSYS:SQLITE", f"/FILE:{db_path}",
                 "/TYPE:E", "/LANG:ENG", f"/SIN:{dst_sin}"],
                capture_output=True, text=True)
            if done.returncode != 0 or not os.path.isfile(db_path):
                raise SincalUnavailable(
                    "SinDBCreate failed (exit %s) creating %s. %s"
                    % (done.returncode, dst_sin,
                       (done.stderr or done.stdout or "").strip()))
        else:
            raise SincalUnavailable(
                "No way to create a SINCAL project: SinDBCreate.exe was not "
                f"found at '{self._dbcreate}' and SINCAL_TEMPLATE is not set. "
                "Point SINCAL_DBCREATE at the installation's Bin/SinDBCreate.exe."
            )

        conn = sqlite3.connect(db_path)
        try:
            counts = sincal_schema.export_network(
                conn, self._network.to_dict(), phases=self._unbalanced)
        finally:
            conn.close()
        logger.info("Wrote SINCAL project %s: %s", dst_sin,
                    ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        self._db_path = dst_sin
        self._sqlite_path = db_path

    def _bind_database(self) -> None:
        # Sincal.Simulation binds to the project database, then runs batches.
        self._sim.Database(self._db_path)
        self._sim.Language("US")
        self._sim.BatchMode(0)

    # ---- engine interface (the solver bus contract's expectations) ---------

    # Pending states are kept in the contract's own units and sign convention
    # (kW/kvar, generation positive). ``sincal_schema.write_element_states``
    # owns the conversion to SINCAL's megawatts and the sign that turns
    # generation into negative load, so the rule lives in one place.
    def update_load(self, bus_id: int, kw: float, kvar: float) -> None:
        self._element_state[("load", bus_id)] = {"kw": kw, "kvar": kvar}

    def update_pv(self, bus_id: int, kw: float) -> None:
        self._element_state[("pv", bus_id)] = {"kw": kw}

    def update_pv_reactive(self, bus_id: int, kvar: float) -> None:
        self._element_state[("pv_q", bus_id)] = {"kvar": kvar}

    def update_bess(self, bus_id: int, kw: float) -> None:
        self._element_state[("bess", bus_id)] = {"kw": kw}

    def update_ev(self, bus_id: int, kw: float) -> None:
        self._element_state[("ev", bus_id)] = {"kw": kw}

    def solve(self) -> bool:
        """Flush pending element states to the DB and run a load flow."""
        self._write_element_states()

        self._sim.Start("LF")
        status = int(self._sim.StatusID)
        if status != STATUS_OK:
            logger.warning("SINCAL load flow finished with status %s "
                           "(%s holds the message log)", status,
                           os.path.join(os.path.dirname(self._sqlite_path), "LOG"))
        return status == STATUS_OK

    def get_bus_voltages_pu(self) -> dict[int, float]:
        with self._results() as conn:
            volts = sincal_schema.read_node_voltages(conn)
        # A bus with no result row keeps 1.0 so a partial result cannot be
        # mistaken for a deep voltage sag.
        return {b: volts.get(b, 1.0) for b in self._network.bus_ids}

    def get_branch_loadings_pct(self) -> dict[int, float]:
        with self._results() as conn:
            return sincal_schema.read_branch_loadings(
                conn, [int(b["branch_id"]) for b in self._network.branches])

    def get_total_losses_kw(self) -> float:
        with self._results() as conn:
            return sincal_schema.read_power_summary(conn)["losses_kw"]

    def get_total_power_kw(self) -> float:
        with self._results() as conn:
            return sincal_schema.read_power_summary(conn)["total_kw"]

    def get_max_vuf_pct(self) -> float:
        """Worst voltage unbalance factor across the network, in percent.

        Zero on a balanced solve, which has no unbalance by construction, and
        the IEC negative-over-positive-sequence ratio on an unbalanced one.
        """
        with self._results() as conn:
            return sincal_schema.read_max_vuf_pct(conn)

    def get_phase_voltages_pu(self) -> dict[int, list[float]]:
        """bus_id -> the three phase voltages per unit, on an unbalanced solve.

        Empty after a balanced solve: a symmetric result has no per-phase
        voltages, and inventing three equal ones would hide that.
        """
        with self._results() as conn:
            return sincal_schema.read_phase_voltages(conn)

    def reset(self) -> None:
        self._element_state.clear()

    # ---- internals -----------------------------------------------------------

    def _results(self):
        """A read connection to the project's SQLite database."""
        import contextlib
        import sqlite3

        @contextlib.contextmanager
        def _open():
            conn = sqlite3.connect(self._sqlite_path)
            try:
                yield conn
            finally:
                conn.close()

        return _open()

    def _write_element_states(self) -> None:
        """Push pending per-element P/Q values into the project database.

        The pending map is keyed by (op, bus); ``sincal_schema`` folds the
        several ops that land on one bus into a single net P and Q, with PV and
        battery discharge as negative load, exactly as the OpenDSS adapter
        models them.
        """
        if not self._element_state:
            return
        import sqlite3

        updates = [dict(state, op=op, bus_id=bus)
                   for (op, bus), state in self._element_state.items()]
        conn = sqlite3.connect(self._sqlite_path)
        try:
            sincal_schema.write_element_states(conn, updates)
        finally:
            conn.close()
        self._element_state.clear()


def check_environment(progid: str = "") -> dict:
    """Probe the local SINCAL environment (used by the health command).

    Reports which ProgID answered, so a health payload distinguishes a desktop
    install from a container image that registers only the versioned ProgID.
    """
    try:
        _dispatch(progid)
        return {
            "sincal_available": True,
            "detail": "PSS SINCAL COM server reachable",
            "progid_tried": progid_candidates(progid),
        }
    except SincalUnavailable as exc:
        return {"sincal_available": False, "detail": str(exc)}


def nan_safe(value: float) -> float:
    return 0.0 if math.isnan(value) else value
