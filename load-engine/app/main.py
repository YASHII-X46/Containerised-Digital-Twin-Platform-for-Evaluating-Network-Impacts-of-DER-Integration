"""Load Engine v5.0 — fully modular load/DER profile generation (FastAPI).

Plug-and-play features:
  - custom load profiles: upload any daily shape and reference it per bus
    with customer_class "custom:<name>";
  - per-bus DER assignment: explicit `bus_data` gives full control of load,
    PV, BESS and EV at every bus;
  - any network: pass `network_buses` (from the Simulation Engine's
    /networks/{id}) and the auto-assigner distributes DERs across it.
"""

import logging
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException

from app.config import settings
from app.models.schemas import (
    BusSummary,
    CustomProfileUpload,
    SimulationRequest,
    SimulationResponse,
)
from app.profiles.bess_model import BESS_CONFIGS
from app.profiles.custom import (
    CUSTOM_PREFIX,
    CustomProfileError,
    CustomProfileStore,
    parse_csv_values,
)
from app.profiles.ev_model import EV_CONFIGS
from app.profiles.generator import (
    ProfileGenerator,
    build_bus_data,
    build_profiles_payload,
)
from app.profiles.archetypes import available_archetypes, get_archetype
from app.profiles.load_shapes import get_available_classes

logger = logging.getLogger(__name__)

VERSION = "5.0.0"


# Module-level so it exists even when the ASGI lifespan is not run (tests).
custom_store = CustomProfileStore(settings.CUSTOM_PROFILES_DIR)

# Load any drop-in plugin modules / directory files (DER plugins, archetypes,
# weather providers). Off by default; configured via DER_PLUGIN_MODULES /
# DER_PLUGINS_DIR. Built-ins are already registered by this point.
from app.profiles.plugin_loader import load_external_plugins  # noqa: E402

load_external_plugins(settings.DER_PLUGIN_MODULES, settings.DER_PLUGINS_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the OpenFMB bus participant."""
    # OpenFMB bus participant: lets the engine be driven over the bus
    # (command openfmb/command/load-engine/generate -> event .../event/...).
    app.state.bus = None
    try:
        if not settings.BUS_ENABLED:
            raise RuntimeError("bus disabled")
        from app.bus import BusParticipant, make_transport
        participant = BusParticipant(
            make_transport(settings.BUS_TRANSPORT, settings, "load-engine-bus"),
            service="load-engine", prefix=settings.BUS_PREFIX,
        )
        # Every metadata/profile operation the UI needs is exposed as an OpenFMB
        # command so the control panel reaches this engine over NATS only — no
        # inter-container HTTP. The matching FastAPI routes below stay for direct
        # host-side debugging.
        participant.on_command("generate", _bus_generate)
        participant.on_command("health", lambda _p: _health_payload())
        participant.on_command("config", lambda _p: _config_payload())
        participant.on_command("archetypes", lambda _p: _archetypes_payload())
        participant.on_command("classes", lambda _p: _classes_payload())
        participant.on_command("der-types", lambda _p: _der_types_payload())
        participant.on_command("weather-sources", lambda _p: _weather_sources_payload())
        participant.on_command("bess-configs", lambda _p: BESS_CONFIGS)
        participant.on_command("ev-configs", lambda _p: EV_CONFIGS)
        participant.on_command("list-custom-profiles", lambda _p: _custom_list_payload())
        participant.on_command("save-custom-profile", _bus_save_custom_profile)
        participant.on_command("delete-custom-profile", _bus_delete_custom_profile)
        participant.on_command("bus-data-preview", _bus_preview_bus_data)
        participant.start()
        app.state.bus = participant
    except Exception as exc:
        logger.warning("Bus participant unavailable on startup: %s", exc)

    yield

    if getattr(app.state, "bus", None) is not None:
        app.state.bus.stop()


app = FastAPI(
    title="Load Engine",
    version=VERSION,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Metadata payload builders — shared by the FastAPI routes (host debugging)
# and the OpenFMB bus command handlers (the path the UI actually uses).
# ---------------------------------------------------------------------------


def _health_payload() -> dict:
    return {"status": "ok", "service": "load-engine", "version": VERSION}


def _config_payload() -> dict:
    return {
        "bus_prefix": settings.BUS_PREFIX,
        "nats_url": settings.NATS_URL,
        "default_seed": settings.DEFAULT_SEED,
        "custom_profiles_dir": settings.CUSTOM_PROFILES_DIR,
        "default_timesteps": 96,
        "default_resolution_minutes": 15,
    }


def _archetypes_payload() -> dict:
    """All registered building archetypes (residential and commercial)."""
    result = {}
    for name in available_archetypes():
        archetype = get_archetype(name)
        result[name] = {
            "category": archetype.category,
            "peak_kw_summer": round(archetype.peak_kw("summer"), 3),
            "peak_kw_winter": round(archetype.peak_kw("winter"), 3),
        }
    return result


def _classes_payload() -> dict:
    # Only load-kind shapes are customer classes; pv/ev shapes are selected
    # through the scenario's pv_profile / ev_profile instead.
    custom = [
        f"{CUSTOM_PREFIX}{p['name']}"
        for p in custom_store.list_profiles() if p.get("kind", "load") == "load"
    ]
    return {"archetypes": get_available_classes(), "custom": custom}


def _der_types_payload() -> dict:
    from app.profiles.der_plugins import installed_plugins
    return {"der_types": [{"name": p.name, "order": p.order} for p in installed_plugins()]}


def _weather_sources_payload() -> dict:
    from app.profiles.weather import available_weather_sources
    return {"weather_sources": available_weather_sources()}


def _custom_list_payload() -> dict:
    return {"profiles": custom_store.list_profiles()}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return _health_payload()


@app.get("/config")
async def get_config():
    """Return current non-sensitive configuration."""
    return _config_payload()


@app.get("/archetypes")
async def get_archetypes():
    """Return residential archetypes with peak demand info."""
    return _archetypes_payload()


@app.get("/classes")
async def get_classes():
    """All usable customer classes: built-in archetypes + custom profiles."""
    return _classes_payload()


@app.get("/der-types")
async def get_der_types():
    """Return the installed DER device plugins (in execution order)."""
    return _der_types_payload()


@app.get("/weather-sources")
async def get_weather_sources():
    """Return the available weather sources for temperature-driven modelling."""
    return _weather_sources_payload()


@app.get("/bess-configs")
async def get_bess_configs():
    """Return available BESS configurations."""
    return BESS_CONFIGS


@app.get("/ev-configs")
async def get_ev_configs():
    """Return available EV charging configurations."""
    return EV_CONFIGS


# ---------------------------------------------------------------------------
# Custom load profiles (plug-and-play)
# ---------------------------------------------------------------------------


@app.get("/profiles/custom")
async def list_custom_profiles():
    """List all uploaded custom load profiles."""
    return _custom_list_payload()


@app.post("/profiles/custom", status_code=201)
async def upload_custom_profile(body: CustomProfileUpload):
    """Save a custom daily load shape (values are normalised to per-unit).

    The profile becomes selectable as customer_class "custom:<name>".
    """
    values = body.values
    if values is None and body.csv_text:
        try:
            values = parse_csv_values(body.csv_text)
        except CustomProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if not values:
        raise HTTPException(
            status_code=400, detail="Provide 'values' or non-empty 'csv_text'."
        )
    try:
        saved = custom_store.save(body.name, values, body.description, kind=body.kind)
    except CustomProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "saved", **saved, "customer_class": f"{CUSTOM_PREFIX}{saved['name']}"}


@app.delete("/profiles/custom/{name}")
async def delete_custom_profile(name: str):
    """Delete an uploaded custom profile."""
    try:
        custom_store.delete(name)
    except CustomProfileError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Profile generation
# ---------------------------------------------------------------------------


def _resolve_bus_data(request: SimulationRequest) -> list[dict]:
    """Pick the bus configuration source: explicit `bus_data` > network auto-assign.

    No network is hardcoded — one of `bus_data` (full per-bus config) or
    `network_buses` (the selected network's bus list) must be supplied.
    """
    if request.bus_data is not None:
        return [b.model_dump() for b in request.bus_data]

    if request.network_buses is not None:
        return build_bus_data(
            [b.model_dump() for b in request.network_buses],
            der_penetration_percent=request.der_penetration_percent,
            pv_buses=request.pv_buses,
            bess_penetration=request.bess_penetration,
            bess_config=request.bess_config,
            ev_penetration=request.ev_penetration,
            ev_config=request.ev_config,
        )

    raise ValueError(
        "No network supplied: provide `network_buses` (the selected network's "
        "bus list) or an explicit `bus_data`."
    )


@app.post("/bus-data/preview")
async def preview_bus_data(request: SimulationRequest):
    """Return the per-bus configuration the auto-assigner would use.

    Lets the UI's bus editor start from the quick-mode assignment and tweak
    individual buses before generating.
    """
    try:
        return {"bus_data": _resolve_bus_data(request)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def run_generation(request: SimulationRequest) -> tuple[SimulationResponse, dict]:
    """Core profile generation used by the bus command and tests.

    Returns (summary response, profiles payload). The payload is the full
    bus-level time series in the Sim Engine's wire format, so profiles travel over
    the message bus — no shared CSV. Raises ValueError / CustomProfileError on bad
    input (callers map these to an HTTP 400 or a bus error event).
    """
    bus_data = _resolve_bus_data(request)

    # Fetch the day's air-temperature trace from the selected weather source
    # (None when 'none'); they drive temperature-dependent HVAC and PV derating,
    # one trace per simulated day.
    from app.profiles.weather import weather_traces
    weather = weather_traces(
        request.weather_source, request.season, request.days, request.timesteps,
    )

    config = {
        "bus_data": bus_data,
        "seed": request.seed,
        "timesteps": request.timesteps,
        "days": request.days,
        "resolution_minutes": request.resolution_minutes,
        "season": request.season,
        "reactive_floor": request.reactive_floor,
        "ev_charging_mode": request.ev_charging_mode,
        "ev_offpeak_start_hour": request.ev_offpeak_start_hour,
        "bess_dispatch_mode": request.bess_dispatch_mode,
        "bess_charge_window": request.bess_charge_window,
        "bess_discharge_window": request.bess_discharge_window,
        "temperatures": weather["temperatures"],
        "irradiances": weather["irradiance"],
        "custom_store": custom_store,
        "pv_profile": request.pv_profile,
        "ev_profile": request.ev_profile,
        "diversity": request.diversity.model_dump(),
        "ev_diversity": request.ev_diversity.model_dump(),
    }

    generator = ProfileGenerator(config)
    profiles = generator.generate_all_profiles()
    summary = generator.get_summary()

    bus_summaries = []
    bus_lookup = {bus["bus_id"]: bus for bus in bus_data}

    for bus_id, data in profiles.items():
        bus_info = bus_lookup[bus_id]
        bus_summaries.append(
            BusSummary(
                bus_id=bus_id,
                customer_class=data["customer_class"],
                peak_load_kw=round(float(np.max(data["load_kw"])), 3),
                peak_pv_kw=round(float(np.max(data["pv_kw"])), 3),
                min_net_load_kw=round(float(np.min(data["net_load_kw"])), 3),
                bess_capacity_kwh=round(float(data["bess_capacity_kwh"]), 3),
                bess_soh=round(float(data.get("bess_soh", 1.0)), 6),
                bess_cycles=round(float(data.get("bess_cycles", 0.0)), 4),
                ev_charge_rate_kw=round(float(bus_info.get("ev_charge_rate_kw", 0.0)), 3),
            )
        )

    # Aggregate battery ageing across the buses that have a battery.
    bess_buses = [d for d in profiles.values() if d.get("bess_capacity_kwh", 0) > 0]
    total_bess_cycles = round(sum(float(d.get("bess_cycles", 0.0)) for d in bess_buses), 3)
    mean_bess_soh = (
        round(sum(float(d.get("bess_soh", 1.0)) for d in bess_buses) / len(bess_buses), 6)
        if bess_buses else 1.0
    )

    response = SimulationResponse(
        status="completed",
        scenario_name=request.scenario_name,
        network_id=request.network_id,
        seed=request.seed,
        der_penetration_percent=request.der_penetration_percent,
        total_buses=summary["total_buses"],
        timesteps=request.timesteps * request.days,
        resolution_minutes=request.resolution_minutes,
        buses_with_pv=summary["buses_with_pv"],
        buses_with_bess=summary["buses_with_bess"],
        total_bess_capacity_kwh=summary["total_bess_capacity_kwh"],
        total_bess_cycles=total_bess_cycles,
        mean_bess_soh=mean_bess_soh,
        buses_with_ev=summary["buses_with_ev"],
        total_ev_charge_capacity_kw=summary["total_ev_charge_capacity_kw"],
        peak_total_load_kw=round(summary["peak_total_load_kw"], 3),
        sum_bus_peak_load_kw=summary["sum_bus_peak_load_kw"],
        coincidence_factor=summary["coincidence_factor"],
        total_pv_capacity_kw=round(summary["total_pv_capacity_kw"], 3),
        peak_total_pv_kw=round(summary["peak_total_pv_kw"], 3),
        min_net_load_kw=round(summary["min_net_load_kw"], 3),
        bus_summaries=bus_summaries,
    )
    payload = build_profiles_payload(
        profiles, bus_data, request.scenario_name, request.seed,
        request.der_penetration_percent, request.resolution_minutes,
        days=request.days,
    )
    return response, payload


def _bus_generate(payload: dict) -> dict:
    """Bus command handler: generation + the profiles payload, over the bus.

    The event carries the full profiles so the Sim Engine can run from them
    directly — no shared CSV file.
    """
    request = SimulationRequest(**payload)
    response, profiles_payload = run_generation(request)
    return {**response.model_dump(), "profiles": profiles_payload}


def _bus_save_custom_profile(payload: dict) -> dict:
    """Bus command handler: save a custom load shape (mirrors POST /profiles/custom)."""
    body = CustomProfileUpload(**payload)
    values = body.values
    if values is None and body.csv_text:
        values = parse_csv_values(body.csv_text)  # CustomProfileError -> error event
    if not values:
        raise ValueError("Provide 'values' or non-empty 'csv_text'.")
    saved = custom_store.save(body.name, values, body.description, kind=body.kind)
    return {"status": "saved", **saved, "customer_class": f"{CUSTOM_PREFIX}{saved['name']}"}


def _bus_delete_custom_profile(payload: dict) -> dict:
    """Bus command handler: delete a custom load profile by name."""
    name = payload.get("name")
    if not name:
        raise ValueError("Provide the custom profile 'name' to delete.")
    custom_store.delete(name)  # CustomProfileError -> error event
    return {"status": "deleted", "name": name}


def _bus_preview_bus_data(payload: dict) -> dict:
    """Bus command handler: per-bus auto-assignment preview for the bus editor."""
    request = SimulationRequest(**payload)
    return {"bus_data": _resolve_bus_data(request)}
