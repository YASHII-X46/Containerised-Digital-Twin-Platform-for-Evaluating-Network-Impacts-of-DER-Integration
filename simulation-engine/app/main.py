"""Simulation Engine v5.0 — solver-agnostic QSTS power flow (FastAPI).

The engine solves any distribution network described by a NetworkModel. It ships
no networks: every model is user-uploaded through the UI (or dropped into
NETWORKS_DIR).
"""

import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.models.schemas import (
    BranchLoadingResult,
    BusVoltageResult,
    NetworkImport,
    NetworkUpload,
    SimulationRequest,
    SimulationResponse,
)
from app.control.remote_coordinator import RemoteCoordinator
from app.control.strategy_catalog import available_strategies, is_registered_strategy
from app.metrics import KpiContext, compute_kpis, kpi_names
from app.network.model import NetworkRegistry, NetworkValidationError
from app.simulation.results import converged_or_all
from app.simulation.der_elements import installed_elements
from app.simulation.qsts import QSTSSimulation
from app.solvers import (
    RemoteSolverEngine,
    SolverTimeout,
    available_solvers,
    resolve_solver_service,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERSION = "5.0.0"


def build_registry() -> NetworkRegistry:
    # The engine ships no built-in or example networks: every network is parsed
    # from a user-provided model uploaded through the UI (or dropped into
    # NETWORKS_DIR). The registry starts empty until the user provides one.
    return NetworkRegistry(None, settings.NETWORKS_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = build_registry()
    app.state.registry = registry

    # OpenFMB bus participant: lets the engine be driven over the bus
    # (command openfmb/command/sim-engine/simulate -> event .../event/...).
    app.state.bus = None
    try:
        if not settings.BUS_ENABLED:
            raise RuntimeError("bus disabled")
        from app.bus import BusParticipant, make_transport
        participant = BusParticipant(
            make_transport(settings.BUS_TRANSPORT, settings, "sim-engine-bus"),
            service="sim-engine", prefix=settings.BUS_PREFIX,
        )
        # Every metadata/network operation the UI needs is exposed as an OpenFMB
        # command so the control panel reaches this engine over NATS only — no
        # inter-container HTTP. The matching FastAPI routes below stay for direct
        # host-side debugging.
        participant.on_command("simulate", _bus_simulate)
        participant.on_command("health", lambda _p: _health_payload())
        participant.on_command("config", lambda _p: _config_payload())
        participant.on_command("list-networks", lambda _p: _list_networks_payload())
        participant.on_command("get-network", _bus_get_network)
        participant.on_command("save-network", _bus_save_network)
        participant.on_command("import-network", _bus_import_network)
        participant.on_command("import-formats", lambda _p: _import_formats_payload())
        participant.on_command("delete-network", _bus_delete_network)
        participant.on_command("strategies", lambda _p: _strategies_payload())
        participant.on_command("kpis", lambda _p: _kpis_payload())
        participant.on_command("der-elements", lambda _p: _der_elements_payload())
        participant.on_command("tariffs", lambda _p: _tariffs_payload())
        participant.on_command("doe-allocations", lambda _p: _doe_allocations_payload())
        participant.on_command("solvers", lambda _p: _solvers_payload())
        participant.start()
        app.state.bus = participant
    except Exception as exc:
        logger.warning("Bus participant unavailable on startup: %s", exc)

    yield

    if getattr(app.state, "bus", None) is not None:
        app.state.bus.stop()


app = FastAPI(title="Simulation Engine", version=VERSION, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Metadata / network payload builders — shared by the FastAPI routes (host
# debugging) and the OpenFMB bus command handlers (the path the UI uses).
# ---------------------------------------------------------------------------


def _health_payload() -> dict:
    return {"status": "ok", "service": "simulation-engine", "version": VERSION}


def _config_payload() -> dict:
    return {
        "bus_prefix": settings.BUS_PREFIX,
        "nats_url": settings.NATS_URL,
        "profiles_dir": settings.PROFILES_DIR,
        "networks_dir": settings.NETWORKS_DIR,
        "default_network": settings.DEFAULT_NETWORK,
        "voltage_lower_pu": settings.VOLTAGE_LOWER_PU,
        "voltage_upper_pu": settings.VOLTAGE_UPPER_PU,
        "thermal_limit_pct": settings.THERMAL_LIMIT_PCT,
    }


def _list_networks_payload() -> dict:
    # The default is resolved against what's actually registered (no assumption
    # that the configured DEFAULT_NETWORK, or any specific network, exists).
    return {
        "default": app.state.registry.resolve_default_id(settings.DEFAULT_NETWORK),
        "networks": app.state.registry.list_networks(),
    }


def _kpis_payload() -> dict:
    return {"kpis": kpi_names()}


def _strategies_payload() -> dict:
    return {
        "default": "uncoordinated",
        "strategies": [{"name": "uncoordinated", "description": "No control (baseline)"}]
        + available_strategies(),
    }


def _import_formats_payload() -> dict:
    from app.network.importers import available_formats
    return {"formats": available_formats()}


def _der_elements_payload() -> dict:
    from app.simulation.der_elements import element_names
    return {"der_elements": element_names()}


def _tariffs_payload() -> dict:
    from app.metrics.tariffs import available_tariffs
    return {"default": "tou_residential", "tariffs": available_tariffs()}


def _doe_allocations_payload() -> dict:
    from app.control.envelopes import available_allocations
    return {"default": "equal", "allocations": available_allocations()}


def _solvers_payload() -> dict:
    return {"default": "opendss", "solvers": available_solvers()}


def _save_network(model_dict: dict) -> dict:
    model = app.state.registry.save(model_dict)
    return {
        "status": "saved",
        "id": model.id,
        "buses": model.num_buses,
        "branches": model.num_branches,
    }


def _import_network(body: NetworkImport) -> dict:
    from app.network.importers import parse_network
    model_dict = parse_network(
        body.content, fmt=body.format, filename=body.filename, network_id=body.id
    )
    model_dict["id"] = body.id
    model = app.state.registry.save(model_dict)
    return {
        "status": "imported", "id": model.id,
        "format": body.format or "auto",
        "buses": model.num_buses, "branches": model.num_branches,
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return _health_payload()


# ---------------------------------------------------------------------------
# Network model registry (plug-and-play)
# ---------------------------------------------------------------------------


@app.get("/networks")
async def list_networks():
    """List all available network models (built-in + user-supplied)."""
    return _list_networks_payload()


@app.get("/networks/{network_id}")
async def get_network(network_id: str):
    """Full topology of one network model (buses, branches, base loads)."""
    try:
        return app.state.registry.get(network_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/networks", status_code=201)
async def upload_network(body: NetworkUpload):
    """Validate and persist a user network model.

    The model becomes immediately selectable for bus simulations and in the UI.
    """
    try:
        return _save_network(body.model_dump())
    except NetworkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/networks/import", status_code=201)
async def import_network(body: NetworkImport):
    """Import a PSS/E RAW/RAWX or CIM/CGMES network and register it.

    The external model is mapped to the internal NetworkModel (buses + branches),
    validated, and persisted — immediately selectable for bus simulations and the UI.
    """
    from app.network.importers import NetworkImportError
    try:
        return _import_network(body)
    except (NetworkImportError, NetworkValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/networks/{network_id}")
async def delete_network(network_id: str):
    """Delete a user-supplied network (built-ins are protected)."""
    try:
        app.state.registry.delete(network_id)
    except NetworkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted", "id": network_id}


@app.get("/tariffs")
async def tariffs():
    """Registered tariff structures for the cost KPIs."""
    return _tariffs_payload()


@app.get("/doe-allocations")
async def doe_allocations():
    """Registered dynamic-operating-envelope allocation policies."""
    return _doe_allocations_payload()


@app.get("/solvers")
async def get_solvers():
    """Registered power-flow solver backends (each a standalone container)."""
    return _solvers_payload()


@app.get("/kpis")
async def get_kpis():
    """List the registered impact KPIs computed for every simulation."""
    return _kpis_payload()


@app.get("/strategies")
async def get_strategies():
    """List the registered DR coordination strategies (plus 'uncoordinated')."""
    return _strategies_payload()


@app.get("/import-formats")
async def get_import_formats():
    """List the registered network-import formats (name, description, extensions)."""
    return _import_formats_payload()


@app.get("/der-elements")
async def get_der_elements():
    """List the registered OpenDSS DER-element types (in install order)."""
    return _der_elements_payload()


@app.get("/config")
async def get_config():
    """Returns non-sensitive configuration settings."""
    return _config_payload()


# ---------------------------------------------------------------------------
# QSTS simulation
# ---------------------------------------------------------------------------


def _normalize_profiles(profiles: dict) -> dict:
    """Coerce an inline profiles payload into the structure the simulation
    consumes — JSON stringifies bus ids over the bus, so coerce them back to
    int keys."""
    buses = {int(bid): data for bid, data in profiles.get("buses", {}).items()}
    metadata = dict(profiles.get("metadata", {}))
    metadata.setdefault("timesteps", len(next(iter(buses.values()))["timeseries"]) if buses else 0)
    metadata.setdefault("resolution_minutes", 15)
    metadata.setdefault("total_buses", len(buses))
    return {"metadata": metadata, "buses": buses}


def run_simulation(request: SimulationRequest) -> SimulationResponse:
    """Core QSTS run used by the bus command handler and tests.

    Profiles arrive inline (`request.profiles`) over the message bus. Raises
    HTTPException on bad input; the bus handler converts it to an error event.
    """
    try:
        net = app.state.registry.get(request.network_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    profiles = _normalize_profiles(request.profiles)

    # The profile's buses must exist in the selected network.
    network_buses = set(net.bus_ids)
    unknown = sorted(b for b in profiles["buses"] if b not in network_buses)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Profile payload references buses {unknown} that do not exist in "
                f"network '{net.id}' ({net.num_buses} buses). Regenerate the "
                f"profiles for this network or pick the matching network_id."
            ),
        )

    solve_mode = request.solve_mode

    # Resolve the named solver backend (registry-validated) and open a solver
    # session in its container over the bus: the solver builds the circuit from
    # the network model plus the per-element bus lists. Placement stays
    # registry-driven — a new DER type is a DERElement here plus its builder in
    # the solver service; no edits to this handler or the QSTS loop.
    try:
        solver_service = resolve_solver_service(request.solver)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus_participant = getattr(app.state, "bus", None)
    if bus_participant is None:
        raise HTTPException(
            status_code=503,
            detail="Simulation requires the bus participant: the power-flow "
            "solver runs in its own container and is reached over NATS.",
        )

    element_buses = {e.name: e.buses(profiles) for e in installed_elements()}
    der_bus_counts = {
        e.summary_key: len(element_buses[e.name]) for e in installed_elements()
    }
    try:
        engine = RemoteSolverEngine(
            bus_participant, solver_service, net.to_dict(), element_buses,
            solve_mode,
        )
    except SolverTimeout as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    from app.control.volt_var import VoltVarCurve, VoltWattCurve

    # Everything from here to the end of the run holds an open solver session —
    # release it on every exit path (validation 400s included).
    coordinator = None
    try:
        # Dynamic operating envelopes: compute per-site export limits up front
        # (static fixed limit, or dynamic limits from network headroom).
        doe = request.doe
        doe_active = doe is not None and doe.mode != "off"
        envelopes: dict = {}
        if doe_active:
            from app.control.envelopes import available_allocations
            allocation_names = {a["name"] for a in available_allocations()}
            if doe.mode == "dynamic" and doe.allocation not in allocation_names:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown envelope allocation '{doe.allocation}'. "
                    f"Available: {sorted(allocation_names)}.",
                )
            if doe.managed and request.coordination_mode == "uncoordinated":
                raise HTTPException(
                    status_code=400,
                    detail="doe.managed enforcement runs through the DR loop — "
                    "pick a coordination_mode, or set managed to false for "
                    "autonomous inverter compliance.",
                )
            from app.control.envelopes import compute_envelopes
            envelopes = compute_envelopes(
                engine, net, profiles,
                mode=doe.mode, allocation=doe.allocation, method=doe.method,
                fixed_export_kw=doe.fixed_export_kw,
                v_limit=settings.VOLTAGE_UPPER_PU,
                thermal_limit=settings.THERMAL_LIMIT_PCT,
            )

        sim = QSTSSimulation(
            engine, profiles, settings, net,
            volt_var=VoltVarCurve() if request.volt_var else None,
            volt_watt=VoltWattCurve() if request.volt_watt else None,
            envelopes=envelopes,
            doe_enforce=not (doe_active and doe.managed),
        )

        # Closed-loop DR coordination: the Simulation Engine owns the solve,
        # while the prosumer shadow twins and DR controller run as separate NATS
        # participants. The control strategy is looked up by name before the run.
        if request.coordination_mode != "uncoordinated":
            if not is_registered_strategy(request.coordination_mode):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown coordination strategy '{request.coordination_mode}'. "
                        f"Available: {[s['name'] for s in available_strategies()]} "
                        f"or 'uncoordinated'."
                    ),
                )
            coordinator = RemoteCoordinator(
                bus_participant,
                profiles,
                request.coordination_mode,
                settings,
                settings.BUS_PREFIX,
                twin_config=request.twin_config,
                # DR curtailment applies on top of the autonomous Volt-Watt output
                # (never undoing it), so the combination is well defined.
                volt_watt=VoltWattCurve() if request.volt_watt else None,
                # Managed-mode envelopes: the coordinator publishes each site's
                # exportLimit_kW with its status so the DR controller enforces it.
                envelopes=envelopes if (doe_active and doe.managed) else None,
            )

        result = sim.run(coordinator=coordinator)
        dr_summary = coordinator.summary() if coordinator is not None else {}
    finally:
        if coordinator is not None:
            coordinator.close()
        engine.close()  # release the solver container's session

    bus_voltage_summary = _build_bus_voltage_summary(result)
    branch_loading_summary = _build_branch_loading_summary(result)

    # Energy = power x timestep duration; the resolution comes from the
    # profile metadata. Non-converged timesteps are excluded — their losses are
    # not physically meaningful.
    resolution_minutes = int(profiles["metadata"].get("resolution_minutes", 15))
    step_hours = resolution_minutes / 60.0
    total_losses_kwh = (
        sum(ts.total_losses_kw for ts in converged_or_all(result.timesteps)) * step_hours
    )

    # Resolve the named tariff for the cost KPIs (registry-validated).
    from app.metrics.tariffs import get_tariff
    try:
        tariff = get_tariff(request.tariff)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Pluggable impact KPIs (the metrics a DER-penetration study compares).
    # Expected feeder net load per timestep backs the energy-balance self-check.
    n_steps = int(profiles["metadata"].get("timesteps", 0))
    expected_net = tuple(
        sum(
            float(bus["timeseries"][t].get("net_load_kw", 0.0))
            for bus in profiles["buses"].values()
        )
        for t in range(n_steps)
    )
    kpis = compute_kpis(
        result,
        KpiContext(
            step_hours=step_hours,
            v_lower=settings.VOLTAGE_LOWER_PU,
            v_upper=settings.VOLTAGE_UPPER_PU,
            thermal_limit_pct=settings.THERMAL_LIMIT_PCT,
            transformer_branch_ids=frozenset(
                int(br["branch_id"]) for br in net.branches if net.is_transformer(br)
            ),
            tariff_peak_rate=settings.TARIFF_PEAK_RATE,
            tariff_offpeak_rate=settings.TARIFF_OFFPEAK_RATE,
            tariff_feed_in_rate=settings.TARIFF_FEED_IN_RATE,
            tariff_peak_start=settings.TARIFF_PEAK_START,
            tariff_peak_end=settings.TARIFF_PEAK_END,
            expected_net_kw=expected_net,
            tariff=tariff,
            transformer_ambient_c=settings.TRANSFORMER_AMBIENT_C,
            emissions_kg_per_kwh=settings.EMISSIONS_KG_PER_KWH,
        ),
    )

    # Per-timestep result series so the UI charts straight from the bus event —
    # no shared result CSV needed.
    result_series = _build_result_series(result)
    if result.doe_active:
        result_series["doe_envelope_total"] = result.doe_envelope_total
        result_series["doe_export_total"] = result.doe_export_total

    return SimulationResponse(
        status="completed",
        scenario_name=result.scenario_name,
        network_id=net.id,
        seed=result.seed,
        der_penetration_percent=result.der_penetration_percent,
        coordination_mode=request.coordination_mode,
        solve_mode=solve_mode,
        solver=request.solver,
        total_timesteps=len(result.timesteps),
        converged_timesteps=sum(1 for ts in result.timesteps if ts.converged),
        resolution_minutes=resolution_minutes,
        total_voltage_violations=result.total_voltage_violations,
        total_thermal_violations=result.total_thermal_violations,
        min_voltage_pu=result.min_voltage_pu,
        max_voltage_pu=result.max_voltage_pu,
        max_loading_pct=result.max_loading_pct,
        total_losses_kwh=round(total_losses_kwh, 3),
        simulation_time_seconds=result.simulation_time_seconds,
        buses_with_pv=der_bus_counts.get("buses_with_pv", 0),
        buses_with_bess=der_bus_counts.get("buses_with_bess", 0),
        buses_with_ev=der_bus_counts.get("buses_with_ev", 0),
        prosumer_twins=dr_summary.get("prosumer_twins", 0),
        buses_curtailed=dr_summary.get("buses_curtailed", 0),
        total_pv_curtailed_kwh=dr_summary.get("total_pv_curtailed_kwh", 0.0),
        total_ev_deferred_kwh=dr_summary.get("total_ev_deferred_kwh", 0.0),
        total_pv_shared_kwh=dr_summary.get("total_pv_shared_kwh", 0.0),
        total_bess_support_kwh=dr_summary.get("total_bess_support_kwh", 0.0),
        total_other_shed_kwh=dr_summary.get("total_other_shed_kwh", 0.0),
        tariff=tariff.name,
        doe_mode=doe.mode if doe_active else "off",
        doe_allocation=doe.allocation if (doe_active and doe.mode == "dynamic") else None,
        doe_curtailed_kwh=result.doe_curtailed_kwh,
        doe_envelope_utilisation_pct=round(kpis.get("doe_envelope_utilisation_pct", 0.0), 2),
        kpis=kpis,
        result_series=result_series,
        bus_voltage_summary=bus_voltage_summary,
        branch_loading_summary=branch_loading_summary,
    )


def _bus_simulate(payload: dict) -> dict:
    """Bus command handler for QSTS simulation."""
    request = SimulationRequest(**payload)
    try:
        return run_simulation(request).model_dump()
    except HTTPException as exc:
        raise RuntimeError(exc.detail)


def _bus_get_network(payload: dict) -> dict:
    """Bus command handler: full topology of one network model."""
    network_id = payload.get("network_id") or payload.get("id")
    if not network_id:
        raise ValueError("Provide 'network_id'.")
    return app.state.registry.get(network_id).to_dict()  # KeyError -> error event


def _bus_save_network(payload: dict) -> dict:
    """Bus command handler: validate and persist a user network model."""
    return _save_network(NetworkUpload(**payload).model_dump())


def _bus_import_network(payload: dict) -> dict:
    """Bus command handler: import a PSS/E RAW/RAWX or CIM/CGMES network."""
    return _import_network(NetworkImport(**payload))


def _bus_delete_network(payload: dict) -> dict:
    """Bus command handler: delete a user-supplied network model."""
    network_id = payload.get("network_id") or payload.get("id")
    if not network_id:
        raise ValueError("Provide 'network_id'.")
    app.state.registry.delete(network_id)  # KeyError/NetworkValidationError -> error event
    return {"status": "deleted", "id": network_id}


def _build_result_series(result) -> dict:
    """Compact per-timestep series for the UI charts (carried in the bus event)."""
    ts_list = result.timesteps
    if not ts_list:
        return {}
    bus_ids = sorted(ts_list[0].bus_voltages_pu)
    v_min, v_max, v_mean, max_loading = [], [], [], []
    for ts in ts_list:
        vs = list(ts.bus_voltages_pu.values())
        v_min.append(round(min(vs), 5))
        v_max.append(round(max(vs), 5))
        v_mean.append(round(sum(vs) / len(vs), 5))
        lds = list(ts.branch_loadings_pct.values())
        max_loading.append(round(max(lds), 3) if lds else 0.0)
    return {
        "timestamps": [ts.timestamp for ts in ts_list],
        "bus_ids": bus_ids,
        "v_by_bus": {
            str(bid): [round(ts.bus_voltages_pu.get(bid, 1.0), 5) for ts in ts_list]
            for bid in bus_ids
        },
        "v_min": v_min,
        "v_max": v_max,
        "v_mean": v_mean,
        "max_loading_over_time": max_loading,
    }


def _build_bus_voltage_summary(result) -> list[BusVoltageResult]:
    """Compute per-bus min/max/mean voltage and violation count."""
    bus_voltages: dict[int, list[float]] = defaultdict(list)
    bus_violations: dict[int, int] = defaultdict(int)

    for ts in converged_or_all(result.timesteps):
        violated_bus_ids = {v["bus_id"] for v in ts.voltage_violations}
        for bus_id, v_pu in ts.bus_voltages_pu.items():
            bus_voltages[bus_id].append(v_pu)
            if bus_id in violated_bus_ids:
                bus_violations[bus_id] += 1

    summary = []
    for bus_id in sorted(bus_voltages.keys()):
        vs = bus_voltages[bus_id]
        summary.append(
            BusVoltageResult(
                bus_id=bus_id,
                min_voltage_pu=round(min(vs), 6),
                max_voltage_pu=round(max(vs), 6),
                mean_voltage_pu=round(sum(vs) / len(vs), 6),
                violation_count=bus_violations[bus_id],
            )
        )
    return summary


def _build_branch_loading_summary(result) -> list[BranchLoadingResult]:
    """Compute per-branch max loading and violation count."""
    branch_max: dict[int, float] = defaultdict(float)
    branch_violations: dict[int, int] = defaultdict(int)

    for ts in converged_or_all(result.timesteps):
        violated_branch_ids = {v["branch_id"] for v in ts.thermal_violations}
        for branch_id, loading_pct in ts.branch_loadings_pct.items():
            branch_max[branch_id] = max(branch_max[branch_id], loading_pct)
            if branch_id in violated_branch_ids:
                branch_violations[branch_id] += 1

    summary = []
    for branch_id in sorted(branch_max.keys()):
        summary.append(
            BranchLoadingResult(
                branch_id=branch_id,
                max_loading_pct=round(branch_max[branch_id], 3),
                violation_count=branch_violations[branch_id],
            )
        )
    return summary
