"""Core QSTS (Quasi-Static Time Series) simulation loop."""

import logging
import time

from app.config import Settings
from app.network.model import NetworkModel
from app.simulation.results import (
    SimulationResult,
    TimestepResult,
    check_violations,
    converged_or_all,
)
from app.control.volt_var import VoltVarCurve, VoltWattCurve, apply_inverter_control
from app.simulation.der_elements import installed_elements

logger = logging.getLogger(__name__)


class QSTSSimulation:
    """Orchestrates a full QSTS power flow simulation over Load Engine profiles."""

    def __init__(
        self,
        engine,  # any solver backend implementing the engine interface
        profiles: dict,
        config: Settings,
        network: NetworkModel,
        volt_var: VoltVarCurve | None = None,
        volt_watt: VoltWattCurve | None = None,
        envelopes: dict | None = None,
        doe_enforce: bool = True,
    ):
        self._engine = engine
        self._profiles = profiles
        self._config = config
        self._network = network
        self._metadata = profiles["metadata"]
        self._results: list[TimestepResult] = []

        # DER elements to drive each timestep (registry-ordered), with the bus
        # ids each one applies to — the same selection main.py used to install
        # them, so a new DER type flows through with no edits to this loop.
        self._elements = installed_elements()
        self._element_buses = {
            element.name: [int(b["bus_id"]) for b in element.buses(profiles)]
            for element in self._elements
        }

        # Autonomous smart-inverter responses (AS/NZS 4777.2). When enabled, each
        # PV inverter sets its reactive power (Volt-VAr) and/or reduces its real
        # power (Volt-Watt) from its local voltage every timestep.
        self._volt_var = volt_var
        self._volt_watt = volt_watt
        self._pv_ratings = (
            {
                bus_id: float(profiles["buses"][bus_id].get("pv_capacity_kw", 0.0))
                for bus_id in self._element_buses.get("pv", [])
            }
            if (volt_var is not None or volt_watt is not None)
            else {}
        )

        # Dynamic operating envelopes: per-site export limits per interval.
        # When enforcing (autonomous compliance), the loop caps each site's net
        # export — reducing battery discharge first, then curtailing PV. When
        # not enforcing (managed mode: the DR loop applies the limits), only
        # the published-envelope series is recorded here.
        self._envelopes = envelopes or {}
        self._doe_enforce = doe_enforce
        self._doe = {
            "curtailed_kwh": 0.0, "envelope_kwh": 0.0, "export_kwh": 0.0,
            "envelope_total": [], "export_total": [],
        }

    def run(self, coordinator=None) -> SimulationResult:
        """Execute the full QSTS simulation.

        Uses decomposed DER elements for accurate power flow: the building load
        sits on Load elements, and every registered ``DERElement`` (PV/BESS/EV
        and any added device) drives its own OpenDSS element each timestep.

        Args:
            coordinator: Optional DR coordination session. When set, each timestep
                runs the bus-mediated twin<->controller exchange and is re-solved
                to a fixed point (PV curtailment, EV deferral, battery charging).

        Returns:
            SimulationResult with all timestep data.
        """
        start_time = time.time()
        num_timesteps = self._metadata["timesteps"]
        step_hours = float(self._metadata.get("resolution_minutes", 15)) / 60.0
        timestep_results = []

        for t in range(num_timesteps):
            # Update building loads (the source/slack bus carries no Load element).
            # `other_der_kw` is the signed net contribution of any DER plugins
            # beyond the four physically-modelled types (PV/BESS/EV); folding it
            # into the building load lets new DER types affect the power flow with
            # no Sim Engine changes. Absent (legacy payloads) -> 0.
            for bus_id, bus_data in self._profiles["buses"].items():
                if bus_id == self._network.source_bus:
                    continue
                ts_data = bus_data["timeseries"][t]
                load_kw = ts_data["load_kw"] + ts_data.get("other_der_kw", 0.0)
                self._engine.update_load(bus_id, load_kw, ts_data["load_kvar"])

            # Drive every installed DER element from its own profile series
            # (PV generators, BESS generators, EV loads, ...) — registry-driven.
            for element in self._elements:
                for bus_id in self._element_buses[element.name]:
                    bus_data = self._profiles["buses"][bus_id]
                    element.update(self._engine, bus_id, element.value_at(bus_data, t))

            # Dynamic operating envelopes: cap each site's export at its
            # published limit (autonomous compliance) before the solve.
            capped_pv = self._apply_envelopes(t, step_hours) if self._envelopes else {}

            timestamp = self._profiles["buses"][
                next(iter(self._profiles["buses"]))
            ]["timeseries"][t]["timestamp"]

            converged = self._engine.solve()

            # Autonomous smart-inverter responses: each PV inverter adjusts its
            # reactive power (Volt-VAr) and/or reduces its real power (Volt-Watt)
            # from local voltage and the network re-solves. Runs before any DR
            # coordination (the commanded real-power layer).
            if self._pv_ratings:
                # The inverter's baseline is the envelope-capped schedule where
                # a cap applied; otherwise the scheduled PV.
                expected_pv = {
                    bus_id: capped_pv.get(bus_id, float(
                        self._profiles["buses"][bus_id]["timeseries"][t].get("pv_kw", 0.0)
                    ))
                    for bus_id in self._pv_ratings
                }
                converged = apply_inverter_control(
                    self._engine, self._pv_ratings, expected_pv,
                    self._volt_var, self._volt_watt,
                )

            # Closed-loop DR over the message bus: prosumer twins publish status,
            # the controller answers with setpoints, and we re-solve to a fixed
            # point (PV curtailment, EV deferral, battery charging) before recording.
            if coordinator is not None:
                converged = coordinator.coordinate(
                    t, self._engine, step_hours, timestamp,
                    pv_base=capped_pv or None,
                )

            if not converged:
                logger.warning("Power flow did not converge at timestep %d", t)

            voltages = self._engine.get_bus_voltages_pu()
            loadings = self._engine.get_branch_loadings_pct()
            losses = self._engine.get_total_losses_kw()
            power = self._engine.get_total_power_kw()
            max_vuf = self._engine.get_max_vuf_pct()

            v_violations, t_violations = check_violations(
                voltages,
                loadings,
                self._config.VOLTAGE_LOWER_PU,
                self._config.VOLTAGE_UPPER_PU,
                self._config.THERMAL_LIMIT_PCT,
            )

            timestep_results.append(
                TimestepResult(
                    timestep=t,
                    timestamp=timestamp,
                    converged=converged,
                    bus_voltages_pu=voltages,
                    branch_loadings_pct=loadings,
                    total_losses_kw=round(losses, 3),
                    total_power_kw=round(power, 3),
                    voltage_violations=v_violations,
                    thermal_violations=t_violations,
                    max_vuf_pct=round(max_vuf, 4),
                )
            )

        self._maybe_log_controller(coordinator)
        elapsed = time.time() - start_time

        # Summary statistics are computed only over converged timesteps — a
        # non-converged solve leaves OpenDSS holding a non-physical iterate whose
        # voltages/loadings would otherwise corrupt the headline min/max and
        # violation totals. The per-timestep records keep every step (flagged by
        # `converged`); the non-convergence count is reported to the caller.
        valid_results = converged_or_all(timestep_results)
        num_converged = sum(1 for tr in timestep_results if tr.converged)
        if num_converged < num_timesteps:
            logger.warning(
                "%d/%d timesteps did not converge; summary statistics computed "
                "over the %d converged timestep(s) only",
                num_timesteps - num_converged,
                num_timesteps,
                num_converged,
            )

        all_voltages = [v for tr in valid_results for v in tr.bus_voltages_pu.values()]
        all_loadings = [ld for tr in valid_results for ld in tr.branch_loadings_pct.values()]
        total_v_violations = sum(len(tr.voltage_violations) for tr in valid_results)
        total_t_violations = sum(len(tr.thermal_violations) for tr in valid_results)

        return SimulationResult(
            scenario_name=self._metadata["scenario_name"],
            seed=self._metadata["seed"],
            der_penetration_percent=self._metadata["der_penetration_percent"],
            timesteps=timestep_results,
            total_voltage_violations=total_v_violations,
            total_thermal_violations=total_t_violations,
            min_voltage_pu=round(min(all_voltages), 6),
            max_voltage_pu=round(max(all_voltages), 6),
            max_loading_pct=round(max(all_loadings), 3),
            simulation_time_seconds=round(elapsed, 3),
            doe_active=bool(self._envelopes),
            doe_curtailed_kwh=round(self._doe["curtailed_kwh"], 3),
            doe_envelope_kwh=round(self._doe["envelope_kwh"], 3),
            doe_export_kwh=round(self._doe["export_kwh"], 3),
            doe_envelope_total=self._doe["envelope_total"],
            doe_export_total=self._doe["export_total"],
        )

    def _apply_envelopes(self, t: int, step_hours: float) -> dict[int, float]:
        """Cap each envelope site's net export at its published limit.

        Site export = PV + battery discharge − (building load + EV). The cap is
        met by reducing battery discharge first, then curtailing PV — the order
        a site controller uses. Returns each capped site's final PV value so the
        inverter-control and DR layers baseline on it. In managed mode
        (``doe_enforce=False``) only the published-envelope series is recorded
        here; the DR loop applies the limits and the achieved-export series
        shows the *scheduled* export for comparison.
        """
        capped: dict[int, float] = {}
        envelope_kw = 0.0
        export_kw = 0.0
        for bus_id, series in self._envelopes.items():
            limit = float(series[t])
            ts = self._profiles["buses"][bus_id]["timeseries"][t]
            load = (
                float(ts.get("load_kw", 0.0))
                + float(ts.get("other_der_kw", 0.0))
                + float(ts.get("ev_charge_kw", 0.0))
            )
            pv = float(ts.get("pv_kw", 0.0))
            bess = float(ts.get("bess_power_kw", 0.0))
            export = pv + max(bess, 0.0) - load
            envelope_kw += limit
            if not self._doe_enforce:
                export_kw += max(0.0, export)
                continue
            excess = export - limit
            if excess > 1e-9:
                # Battery discharge yields first, PV curtails the remainder
                # (the remainder always fits within PV — see site export above).
                discharge_cut = min(max(bess, 0.0), excess)
                if discharge_cut > 0.0:
                    self._engine.update_bess(bus_id, bess - discharge_cut)
                pv_final = max(0.0, pv - (excess - discharge_cut))
                self._engine.update_pv(bus_id, pv_final)
                capped[bus_id] = pv_final
                self._doe["curtailed_kwh"] += excess * step_hours
                export_kw += limit
            else:
                export_kw += max(0.0, export)
        self._doe["envelope_total"].append(round(envelope_kw, 3))
        self._doe["export_total"].append(round(export_kw, 3))
        if self._doe_enforce:
            self._doe["envelope_kwh"] += envelope_kw * step_hours
            self._doe["export_kwh"] += export_kw * step_hours
        return capped

    @staticmethod
    def _maybe_log_controller(coordinator) -> None:
        if coordinator is not None:
            s = coordinator.summary()
            logger.info(
                "DR (%s): %d prosumers, %.2f kWh PV curtailed, %.2f kWh EV "
                "deferred, %.2f kWh shared to storage, %d bus messages",
                s["mode"], s["prosumer_twins"], s["total_pv_curtailed_kwh"],
                s["total_ev_deferred_kwh"], s["total_pv_shared_kwh"], s["messages"],
            )
