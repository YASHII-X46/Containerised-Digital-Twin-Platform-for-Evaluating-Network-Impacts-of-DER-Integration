"""Wraps OpenDSSDirect.py to execute power flow solutions."""

import logging
import math
import os

import opendssdirect as dss

from dss_solver.network import SolverNetwork

logger = logging.getLogger(__name__)


class OpenDSSEngine:
    """OpenDSS power flow engine for any network-model-described feeder."""

    def __init__(self, dss_file: str, network: SolverNetwork):
        # OpenDSS's `Compile` changes the process working directory to the folder
        # containing the compiled file. Resolve to an absolute path and restore the
        # CWD afterwards so subsequent (relative) paths keep working across requests.
        self._dss_file = os.path.abspath(dss_file)
        self._network = network
        dss.Basic.Start(0)
        self._compile()

        circuit_name = dss.Circuit.Name()
        if not circuit_name:
            raise RuntimeError(f"Failed to load circuit from {dss_file}")

        self._bus_names = list(dss.Circuit.AllBusNames())
        self._bus_count = len(self._bus_names)
        logger.info("Loaded circuit '%s' with %d buses", circuit_name, self._bus_count)

    def load_pv_systems(self, pv_dss_file: str) -> None:
        """Load PV generator definitions from a .dss file."""
        dss.Text.Command(f"Redirect [{pv_dss_file}]")
        gen_count = dss.Generators.Count()
        logger.info("Loaded %d PV generators from %s", gen_count, pv_dss_file)

    def load_bess_systems(self, bess_dss_file: str) -> None:
        """Load BESS generator definitions from a .dss file."""
        dss.Text.Command(f"Redirect [{bess_dss_file}]")
        logger.info("Loaded BESS generators from %s", bess_dss_file)

    def load_ev_loads(self, ev_dss_file: str) -> None:
        """Load EV load definitions from a .dss file."""
        dss.Text.Command(f"Redirect [{ev_dss_file}]")
        logger.info("Loaded EV loads from %s", ev_dss_file)

    def update_load(self, bus_id: int, kw: float, kvar: float) -> None:
        """Update load at a bus for the current timestep."""
        dss.Text.Command(f"Edit Load.load_{bus_id:03d} kW={kw} kvar={kvar}")

    def update_pv(self, bus_id: int, kw: float) -> None:
        """Update PV generation at a bus for the current timestep."""
        enabled = "no" if kw == 0 else "yes"
        dss.Text.Command(f"Edit Generator.pv_{bus_id:03d} kW={kw} Enabled={enabled}")

    def update_pv_reactive(self, bus_id: int, kvar: float) -> None:
        """Set the PV inverter's reactive power (kvar) — positive injects (raises
        voltage), negative absorbs (lowers it). Used by autonomous Volt-VAr."""
        dss.Text.Command(f"Edit Generator.pv_{bus_id:03d} kvar={kvar}")

    def update_bess(self, bus_id: int, kw: float) -> None:
        """Update BESS power at a bus for the current timestep.

        Positive kW = discharging (injecting power into grid).
        Negative kW = charging (absorbing power from grid).
        """
        enabled = "no" if kw == 0 else "yes"
        dss.Text.Command(f"Edit Generator.bess_{bus_id:03d} kW={kw} Enabled={enabled}")

    def update_ev(self, bus_id: int, kw: float) -> None:
        """Update EV charging load at a bus for the current timestep."""
        enabled = "yes" if kw > 0 else "no"
        dss.Text.Command(f"Edit Load.ev_{bus_id:03d} kW={kw} kvar=0 Enabled={enabled}")

    def solve(self) -> bool:
        """Run a power flow solution. Returns True if converged."""
        dss.Solution.Solve()
        return dss.Solution.Converged()

    def get_bus_voltages_pu(self) -> dict[int, float]:
        """Returns bus_id -> average per-unit voltage magnitude across phases."""
        voltages = {}
        for bus_id in self._network.bus_ids:
            dss.Circuit.SetActiveBus(f"bus_{bus_id:03d}")
            vmag_angle = dss.Bus.puVmagAngle()
            if vmag_angle:
                phase_mags = vmag_angle[::2]
                voltages[bus_id] = sum(phase_mags) / len(phase_mags) if phase_mags else 1.0
            else:
                voltages[bus_id] = 1.0
        return voltages

    def get_branch_loadings_pct(self) -> dict[int, float]:
        """Returns branch_id -> loading percentage (lines and transformers).

        Lines use current vs the conductor's normal ampacity. Transformers use
        winding power vs the kVA rating — the two windings sit at different
        voltages, so a current-vs-ampacity ratio would compare the LV winding's
        amps against the HV rating and read wildly high.
        """
        loadings = {}
        for branch in self._network.branches:
            branch_id = int(branch["branch_id"])
            if self._network.is_transformer(branch):
                dss.Circuit.SetActiveElement(f"Transformer.xfmr_{branch_id}")
                loadings[branch_id] = self._transformer_loading_pct(branch)
                continue
            dss.Circuit.SetActiveElement(f"Line.branch_{branch_id}")
            currents = dss.CktElement.CurrentsMagAng()
            phase_mags = currents[::2] if currents else []
            max_current = max((abs(c) for c in phase_mags), default=0.0)
            normal_amps = dss.CktElement.NormalAmps()
            loadings[branch_id] = (max_current / normal_amps) * 100 if normal_amps > 0 else 0.0
        return loadings

    @staticmethod
    def _transformer_loading_pct(branch: dict) -> float:
        """Transformer loading: apparent power through winding 1 vs its kVA rating.

        Assumes the Transformer element is already the active circuit element.
        """
        rating_kva = float(branch.get("rating_kva") or 0.0)
        if rating_kva <= 0:
            return 0.0
        powers = dss.CktElement.Powers()  # [kW, kvar] per conductor, terminal by terminal
        nconds = dss.CktElement.NumConductors()
        if not powers or nconds <= 0:
            return 0.0
        term1 = powers[: 2 * nconds]                     # winding 1 (primary)
        p, q = sum(term1[0::2]), sum(term1[1::2])
        return (p * p + q * q) ** 0.5 / rating_kva * 100.0

    def get_max_vuf_pct(self) -> float:
        """Worst voltage-unbalance factor (%) across three-phase buses.

        VUF is the negative- to positive-sequence voltage ratio (the IEC
        definition; typical planning limit 2%). Buses with fewer than three
        connected phases are skipped. Near zero on balanced solves.
        """
        a = complex(-0.5, 3 ** 0.5 / 2)   # 1 /_ 120 deg
        worst = 0.0
        for bus_id in self._network.bus_ids:
            dss.Circuit.SetActiveBus(f"bus_{bus_id:03d}")
            vmag_angle = dss.Bus.puVmagAngle()
            if not vmag_angle or len(vmag_angle) < 6:
                continue   # fewer than three phases: VUF undefined
            phases = [
                vmag_angle[2 * k] * complex(
                    math.cos(math.radians(vmag_angle[2 * k + 1])),
                    math.sin(math.radians(vmag_angle[2 * k + 1])),
                )
                for k in range(3)
            ]
            va, vb, vc = phases
            v1 = (va + a * vb + a * a * vc) / 3.0
            v2 = (va + a * a * vb + a * vc) / 3.0
            if abs(v1) > 1e-9:
                worst = max(worst, abs(v2) / abs(v1) * 100.0)
        return worst

    def get_total_losses_kw(self) -> float:
        """Returns total circuit real power losses in kW."""
        return dss.Circuit.Losses()[0] / 1000.0

    def get_total_power_kw(self) -> float:
        """Returns total delivered power in kW (positive = delivered)."""
        return -dss.Circuit.TotalPower()[0]

    def reset(self) -> None:
        """Recompile the master DSS file to restore initial state."""
        self._compile()

    def _compile(self) -> None:
        """Compile the master file, restoring CWD (OpenDSS changes it on Compile)."""
        cwd = os.getcwd()
        try:
            dss.Text.Command(f"Compile [{self._dss_file}]")
        finally:
            os.chdir(cwd)
