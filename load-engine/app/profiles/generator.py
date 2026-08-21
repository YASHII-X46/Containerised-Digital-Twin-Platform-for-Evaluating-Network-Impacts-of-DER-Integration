"""Profile generator orchestrator — network-agnostic.

Generates load, PV, BESS, and EV profiles for any set of buses in a single
pass. There is no built-in network: the bus list is always supplied, either as
  - an explicit per-bus configuration (full plug-and-play control), or
  - auto-assignment over the buses of any provided network (`build_bus_data`).
"""

from datetime import datetime, timedelta

import numpy as np

from app.profiles.bess_model import BESS_CONFIGS
from app.profiles.custom import CUSTOM_PREFIX, CustomProfileStore
from app.profiles.der_plugins import (
    GenerationContext,
    compute_net_load,
    installed_plugins,
)
from app.profiles.ev_model import EV_CONFIGS
from app.profiles.load_shapes import AVAILABLE_CLASSES

# Default load-diversity (aggregation) settings — a bus aggregates many
# households whose peaks do not coincide. Overridable via the request's
# `diversity` block; `enabled: False` reproduces the legacy synchronised loads.
DEFAULT_DIVERSITY = {
    "enabled": True,
    "admd_kw": 1.5,
    "sigma_minutes": 45.0,
    "magnitude_cv": 0.4,
    "appliance_cv": 0.1,
    "max_households": 400,
    "preserve_peak": True,
}

# Default EV arrival-time diversity (uncontrolled charging spread across a bus).
DEFAULT_EV_DIVERSITY = {
    "enabled": True,
    "arrival_sigma_minutes": 90.0,
    "max_evs": 64,
}


def _strip_custom(name: str | None) -> str | None:
    """Accept a custom-profile reference with or without the 'custom:' prefix."""
    if name and name.startswith(CUSTOM_PREFIX):
        return name[len(CUSTOM_PREFIX):]
    return name


class ProfileGenerator:
    """Orchestrates load, solar, BESS, and EV profile generation for all buses."""

    def __init__(self, config: dict):
        self.bus_data = config["bus_data"]
        self.seed = config.get("seed", 42)
        # `timesteps` is the per-day step count; `days` repeats it across a
        # multi-day horizon. Each day varies by its own seed.
        self.timesteps = config.get("timesteps", 96)
        self.days = int(config.get("days", 1))
        self.total_steps = self.days * self.timesteps
        self.resolution_minutes = config.get("resolution_minutes", 15)
        self.season = config.get("season", "summer")
        self.reactive_floor = float(config.get("reactive_floor", 0.0))
        self.ev_charging_mode = config.get("ev_charging_mode", "uncontrolled")
        self.ev_offpeak_start_hour = float(config.get("ev_offpeak_start_hour", 23.0))
        self.bess_dispatch_mode = config.get("bess_dispatch_mode", "self_consumption")
        self.bess_charge_window = tuple(config.get("bess_charge_window") or (1.0, 6.0))
        self.bess_discharge_window = tuple(config.get("bess_discharge_window") or (17.0, 21.0))
        # Optional per-day air-temperature traces (from a weather provider); drive
        # HVAC and PV derating. None -> fixed season curves.
        self.temperatures = config.get("temperatures")
        # Optional per-day measured-irradiance traces (drive PV directly,
        # replacing the synthetic cloud model). None -> cloud model applies.
        self.irradiances = config.get("irradiances")
        self.custom_store: CustomProfileStore | None = config.get("custom_store")
        # Optional scenario-level custom day-shapes for PV and EV (kind-checked
        # against the store at generation time).
        self.pv_profile = _strip_custom(config.get("pv_profile"))
        self.ev_profile = _strip_custom(config.get("ev_profile"))
        # Load- and EV-diversity settings (fall back to defaults for unset keys).
        self.diversity = {**DEFAULT_DIVERSITY, **(config.get("diversity") or {})}
        self.ev_diversity = {**DEFAULT_EV_DIVERSITY, **(config.get("ev_diversity") or {})}
        self._profiles: dict | None = None

    @property
    def step_hours(self) -> float:
        return self.resolution_minutes / 60.0

    def _build_bus_day(self, bus: dict, day_seed: int, *, temperature=None,
                       irradiance=None, day_type: str = "weekday",
                       bess_initial_soc: float = 0.5,
                       bess_initial_soh: float = 1.0) -> dict:
        """Generate one day's per-bus series via the DER-plugin pipeline."""
        ctx = GenerationContext(
            bus, day_seed,
            timesteps=self.timesteps, resolution_minutes=self.resolution_minutes,
            season=self.season,
            reactive_floor=self.reactive_floor, diversity=self.diversity,
            ev_diversity=self.ev_diversity, ev_charging_mode=self.ev_charging_mode,
            ev_offpeak_start_hour=self.ev_offpeak_start_hour,
            custom_store=self.custom_store,
            pv_profile=self.pv_profile, ev_profile=self.ev_profile,
            bess_dispatch_mode=self.bess_dispatch_mode,
            bess_charge_window=self.bess_charge_window,
            bess_discharge_window=self.bess_discharge_window,
            temperature=temperature, irradiance=irradiance, day_type=day_type,
            bess_initial_soc=bess_initial_soc, bess_initial_soh=bess_initial_soh,
        )
        # Each DER type is a registered plugin; they run in dependency order
        # (load -> PV -> EV -> BESS) and each fills its series into the context.
        for plugin in installed_plugins():
            if plugin.applies_to(bus):
                plugin.generate(ctx)

        result = {
            "load_kw": ctx.get("load_kw"),
            "load_kvar": ctx.get("load_kvar"),
            "pv_kw": ctx.get("pv_kw"),
            "bess_power_kw": ctx.get("bess_power_kw"),
            "bess_soc": ctx.get("bess_soc"),
            "ev_charge_kw": ctx.get("ev_charge_kw"),
            # Net load sums every installed plugin's signed contribution, so a
            # newly registered DER type affects it without editing the generator.
            "net_load_kw": compute_net_load(ctx),
        }
        # Preserve any series contributed by additional (non-built-in) plugins.
        for key, series in ctx.series.items():
            result.setdefault(key, series)
        result["bess_capacity_kwh"] = ctx.meta.get(
            "bess_capacity_kwh", float(bus.get("bess_capacity_kwh", 0.0))
        )
        result["bess_final_soc"] = ctx.meta.get("bess_final_soc")  # None when no BESS
        result["bess_final_soh"] = ctx.meta.get("bess_final_soh")
        result["bess_cycles_day"] = ctx.meta.get("bess_cycles_day", 0.0)
        # Aggregate EV fleet charging capacity at this bus (0 when no EV).
        result["ev_fleet_capacity_kw"] = ctx.meta.get("ev_fleet_capacity_kw", 0.0)
        return result

    def _day_seed(self, bus_id: int, day: int) -> int:
        """Per-bus, per-day seed. Day 0 of a single-day run keeps the legacy seed."""
        spec = [self.seed, bus_id] if self.days == 1 else [self.seed, bus_id, day]
        return int(np.random.SeedSequence(spec).generate_state(1)[0])

    # Scalar (non-array) per-day fields excluded from the concatenated horizon.
    _SCALAR_KEYS = (
        "bess_capacity_kwh", "bess_final_soc", "bess_final_soh",
        "bess_cycles_day", "ev_fleet_capacity_kw",
    )

    def generate_all_profiles(self) -> dict:
        """Generate load, solar, BESS, and EV profiles for every bus.

        For a multi-day horizon (``days`` > 1) each day is generated with its own
        seed, day type (weekday/weekend), and weather, and the daily series are
        concatenated. The battery state of charge carries from one day into the
        next, so the horizon is continuous rather than a set of independent days.

        Returns:
            Dict keyed by bus_id with all profile arrays (length days x timesteps).
        """
        start = datetime(2024, 1, 15, 0, 0, 0)  # a Monday
        start_weekday = start.weekday()
        timestamps = [
            (start + timedelta(minutes=i * self.resolution_minutes)).isoformat()
            for i in range(self.total_steps)
        ]

        profiles: dict = {}
        for bus in self.bus_data:
            bus_id = bus["bus_id"]
            soc = 0.5   # battery starts mid-charge on day 0, then carries over
            soh = 1.0   # battery starts at full health, then ages across days
            total_cycles = 0.0
            day_results = []
            for d in range(self.days):
                day_type = "weekend" if (start_weekday + d) % 7 >= 5 else "weekday"
                temperature = self.temperatures[d] if self.temperatures else None
                irradiance = self.irradiances[d] if self.irradiances else None
                result = self._build_bus_day(
                    bus, self._day_seed(bus_id, d),
                    temperature=temperature, irradiance=irradiance, day_type=day_type,
                    bess_initial_soc=soc, bess_initial_soh=soh,
                )
                day_results.append(result)
                if result.get("bess_final_soc") is not None:
                    soc = result["bess_final_soc"]  # next day starts where this ended
                if result.get("bess_final_soh") is not None:
                    soh = result["bess_final_soh"]  # battery ages day to day
                    total_cycles += result.get("bess_cycles_day", 0.0)

            array_keys = {
                k for day in day_results for k in day if k not in self._SCALAR_KEYS
            }
            merged = {
                key: np.concatenate(
                    [day.get(key, np.zeros(self.timesteps)) for day in day_results]
                )
                for key in array_keys
            }
            merged["bus_id"] = bus_id
            merged["customer_class"] = bus["customer_class"]
            merged["bess_capacity_kwh"] = day_results[0]["bess_capacity_kwh"]
            merged["bess_soh"] = round(soh, 6)            # state of health at horizon end
            merged["bess_cycles"] = round(total_cycles, 4)  # equivalent full cycles
            merged["ev_fleet_capacity_kw"] = day_results[0]["ev_fleet_capacity_kw"]
            merged["timestamps"] = timestamps
            profiles[bus_id] = merged

        self._profiles = profiles
        return profiles

    def get_summary(self) -> dict:
        """Return aggregated statistics across all buses."""
        if self._profiles is None:
            self.generate_all_profiles()

        profiles = self._profiles
        total_buses = len(profiles)

        buses_with_pv = sum(
            1 for p in profiles.values() if np.any(p["pv_kw"] > 0)
        )
        buses_with_bess = sum(
            1 for p in profiles.values() if p["bess_capacity_kwh"] > 0
        )
        buses_with_ev = sum(
            1 for p in profiles.values() if np.any(p["ev_charge_kw"] > 0)
        )

        total_load = np.zeros(self.total_steps)
        total_pv = np.zeros(self.total_steps)
        total_net = np.zeros(self.total_steps)

        for p in profiles.values():
            total_load += p["load_kw"]
            total_pv += p["pv_kw"]
            # True feeder net load: each bus's net_load_kw already sums every
            # installed DER plugin's signed contribution (PV, EV, BESS, extras),
            # so the feeder minimum reflects all DERs — not just load minus PV.
            total_net += p["net_load_kw"]

        # Coincidence factor = simultaneous feeder peak / sum of per-bus peaks.
        # With load diversity this is < 1 (peaks do not all line up); without
        # it, the synchronised shapes give exactly 1.0.
        sum_bus_peak_load_kw = sum(float(np.max(p["load_kw"])) for p in profiles.values())
        peak_total_load_kw = float(np.max(total_load))
        coincidence_factor = (
            peak_total_load_kw / sum_bus_peak_load_kw if sum_bus_peak_load_kw > 0 else 1.0
        )

        total_pv_capacity_kw = sum(
            float(bus.get("pv_capacity_kw", 0.0)) for bus in self.bus_data
        )
        total_bess_capacity_kwh = sum(
            float(p["bess_capacity_kwh"]) for p in profiles.values()
        )
        # True EV charging capacity: each EV bus's whole fleet (fleet x charger
        # rating), matching the aggregate demand series — not one charger per bus.
        total_ev_charge_capacity_kw = sum(
            float(p.get("ev_fleet_capacity_kw", 0.0)) for p in profiles.values()
        )

        return {
            "total_buses": total_buses,
            "buses_with_pv": buses_with_pv,
            "buses_with_bess": buses_with_bess,
            "total_bess_capacity_kwh": round(total_bess_capacity_kwh, 2),
            "buses_with_ev": buses_with_ev,
            "total_ev_charge_capacity_kw": round(total_ev_charge_capacity_kw, 2),
            "peak_total_load_kw": peak_total_load_kw,
            "sum_bus_peak_load_kw": round(sum_bus_peak_load_kw, 3),
            "coincidence_factor": round(coincidence_factor, 4),
            "total_pv_capacity_kw": float(total_pv_capacity_kw),
            "peak_total_pv_kw": float(np.max(total_pv)),
            "min_net_load_kw": float(np.min(total_net)),
        }


# ---------------------------------------------------------------------------
# Bus-data builder — auto-assigns DERs across the buses of *any* network. No
# network is hardcoded: the bus list comes from the selected network model.
# ---------------------------------------------------------------------------


def build_bus_data(
    network_buses: list[dict],
    der_penetration_percent: float = 100,
    pv_buses: list[int] | None = None,
    bess_penetration: float = 0.3,
    bess_config: str = "powerwall_2",
    ev_penetration: float = 0.2,
    ev_config: str = "level2_7kw",
    archetype_map: dict[int, str] | None = None,
    source_bus: int | None = None,
) -> list[dict]:
    """Auto-assign DERs across the buses of *any* network.

    Args:
        network_buses: List of {"bus_id", "base_load_kw", "base_load_kvar"}
            describing the network's load buses (the source bus may be
            included; it is kept but never assigned load or DERs).
        der_penetration_percent: Total PV capacity as % of total base load.
        pv_buses: Buses that receive PV (default: all load buses).
        bess_penetration: Fraction of PV buses that get a BESS (0.0 - 1.0).
        bess_config: Named BESS configuration.
        ev_penetration: Fraction of load buses that get an EV (0.0 - 1.0).
        ev_config: Named EV configuration.
        archetype_map: Optional bus_id -> customer_class override.
        source_bus: Slack bus id (no load/DERs); inferred as the bus with
            zero base load if not given.
    """
    archetype_map = archetype_map or {}

    specs: dict[int, dict] = {}
    for b in network_buses:
        bid = int(b["bus_id"])
        kw = float(b.get("base_load_kw", 0.0))
        kvar = b.get("base_load_kvar")
        specs[bid] = {
            "base_load_kw": kw,
            "base_load_kvar": float(kvar) if kvar is not None else round(kw * 0.4, 1),
        }

    if source_bus is None:
        zero_load = [bid for bid, s in specs.items() if s["base_load_kw"] == 0.0]
        source_bus = min(zero_load) if zero_load else None

    load_buses = sorted(
        bid for bid, s in specs.items()
        if bid != source_bus and s["base_load_kw"] > 0
    )

    if pv_buses is None:
        pv_buses = list(load_buses)
    pv_buses = [b for b in pv_buses if b in load_buses]

    if der_penetration_percent > 0 and not pv_buses:
        raise ValueError(
            "pv_buses cannot be empty when DER penetration is greater than 0."
        )

    total_peak_load_kw = sum(specs[b]["base_load_kw"] for b in load_buses)
    total_pv_capacity_kw = total_peak_load_kw * (der_penetration_percent / 100.0)
    pv_bus_load_total = sum(specs[b]["base_load_kw"] for b in pv_buses)

    # Deterministic BESS assignment: pick fraction of PV buses
    rng_bess = np.random.default_rng(7)
    n_bess = int(round(bess_penetration * len(pv_buses)))
    bess_buses = set(rng_bess.choice(pv_buses, size=n_bess, replace=False)) if n_bess > 0 else set()

    # Deterministic EV assignment: pick fraction of load buses
    rng_ev = np.random.default_rng(13)
    n_ev = int(round(ev_penetration * len(load_buses)))
    ev_buses = set(rng_ev.choice(load_buses, size=n_ev, replace=False)) if n_ev > 0 else set()

    bess_params = BESS_CONFIGS.get(bess_config, {})
    ev_params = EV_CONFIGS.get(ev_config, {})

    def class_for_bus(bus_id: int, ordinal: int) -> str:
        if bus_id in archetype_map:
            return archetype_map[bus_id]
        # Deterministic rotation through the archetypes for unmapped buses.
        return AVAILABLE_CLASSES[ordinal % len(AVAILABLE_CLASSES)]

    buses: list[dict] = []
    for ordinal, bus_id in enumerate(sorted(specs)):
        spec = specs[bus_id]
        is_load_bus = bus_id in load_buses

        pv_cap = 0.0
        if is_load_bus and bus_id in pv_buses and pv_bus_load_total > 0:
            allocation_fraction = spec["base_load_kw"] / pv_bus_load_total
            pv_cap = round(total_pv_capacity_kw * allocation_fraction, 3)

        has_bess = is_load_bus and bus_id in bess_buses
        has_ev = is_load_bus and bus_id in ev_buses

        buses.append(
            {
                "bus_id": bus_id,
                "base_load_kw": round(spec["base_load_kw"], 3) if is_load_bus else 0.0,
                "base_load_kvar": spec["base_load_kvar"] if is_load_bus else 0.0,
                "customer_class": class_for_bus(bus_id, ordinal),
                "pv_capacity_kw": pv_cap,
                "bess_config": bess_config if has_bess else None,
                "bess_capacity_kwh": bess_params.get("capacity_kwh", 0.0) if has_bess else 0.0,
                "bess_max_charge_kw": bess_params.get("max_charge_kw", 0.0) if has_bess else 0.0,
                "bess_max_discharge_kw": bess_params.get("max_discharge_kw", 0.0) if has_bess else 0.0,
                "ev_config": ev_config if has_ev else None,
                "ev_charge_rate_kw": ev_params.get("charge_rate_kw", 0.0) if has_ev else 0.0,
            }
        )

    return buses


def build_profiles_payload(
    profiles: dict,
    bus_data: list[dict],
    scenario_name: str,
    seed: int,
    der_penetration_percent: float,
    resolution_minutes: int = 15,
    days: int = 1,
) -> dict:
    """Serialise generated profiles into the wire format the Sim Engine consumes.

    Profiles travel load -> sim inline over the message bus with no shared file.
    Bus ids are kept as ints; JSON will stringify them, and the Sim Engine
    coerces them back.
    """
    bus_lookup = {bus["bus_id"]: bus for bus in bus_data}
    timesteps = len(next(iter(profiles.values()))["timestamps"]) if profiles else 0

    # Series the Sim Engine maps onto explicit OpenDSS elements (building load,
    # PV gen, BESS gen, EV load) plus bookkeeping keys. Anything else a DER plugin
    # contributed is an "extra" series: carried by name (for charting) and folded
    # into `other_der_kw` so the power flow honours it without the Sim Engine
    # knowing the DER type — fully modular end to end.
    builtin_keys = {
        "load_kw", "load_kvar", "pv_kw", "bess_power_kw", "bess_soc",
        "ev_charge_kw", "net_load_kw",
    }

    extra_series_names: set[str] = set()
    buses: dict[int, dict] = {}
    for bus_id, p in profiles.items():
        b = bus_lookup[bus_id]
        extra_keys = [
            k for k, v in p.items()
            if k not in builtin_keys and isinstance(v, np.ndarray)
        ]
        extra_series_names.update(extra_keys)
        series = []
        for i, timestamp in enumerate(p["timestamps"]):
            # Net minus the four built-in physical DERs = the contribution of any
            # additional DER plugins, applied generically by the Sim Engine.
            other_der_kw = float(p["net_load_kw"][i]) - (
                float(p["load_kw"][i]) - float(p["pv_kw"][i])
                + float(p["ev_charge_kw"][i]) - float(p["bess_power_kw"][i])
            )
            entry = {
                "timestep": i + 1,
                "timestamp": timestamp,
                "load_kw": round(float(p["load_kw"][i]), 6),
                "load_kvar": round(float(p["load_kvar"][i]), 6),
                "pv_kw": round(float(p["pv_kw"][i]), 6),
                "bess_power_kw": round(float(p["bess_power_kw"][i]), 6),
                "bess_soc": round(float(p["bess_soc"][i]), 6),
                "ev_charge_kw": round(float(p["ev_charge_kw"][i]), 6),
                "net_load_kw": round(float(p["net_load_kw"][i]), 6),
                "other_der_kw": round(other_der_kw, 6),
            }
            for k in extra_keys:
                entry[k] = round(float(p[k][i]), 6)
            series.append(entry)
        buses[bus_id] = {
            "customer_class": p["customer_class"],
            "base_load_kw": float(b["base_load_kw"]),
            "base_load_kvar": float(b["base_load_kvar"]),
            "pv_capacity_kw": float(b.get("pv_capacity_kw", 0.0)),
            "bess_capacity_kwh": round(float(p["bess_capacity_kwh"]), 3),
            "bess_soh": round(float(p.get("bess_soh", 1.0)), 6),
            "bess_cycles": round(float(p.get("bess_cycles", 0.0)), 4),
            "ev_charge_rate_kw": float(b.get("ev_charge_rate_kw", 0.0)),
            "timeseries": series,
        }

    return {
        "metadata": {
            "scenario_name": scenario_name,
            "seed": seed,
            "der_penetration_percent": der_penetration_percent,
            "total_buses": len(buses),
            "timesteps": timesteps,
            "days": days,
            "resolution_minutes": resolution_minutes,
            # Names of any extra DER series carried beyond the built-ins, so the
            # UI can chart them without hardcoding DER types.
            "extra_der_series": sorted(extra_series_names),
        },
        "buses": buses,
    }
