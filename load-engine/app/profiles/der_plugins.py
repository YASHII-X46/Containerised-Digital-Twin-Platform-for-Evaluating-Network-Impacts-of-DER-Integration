"""Pluggable DER device registry.

Each DER type (load, PV, BESS, EV — and any device a user adds) is a
``DERPlugin`` that fills its own per-bus time series into a shared
``GenerationContext``. The profile generator simply iterates whatever plugins
are *installed*, in ascending ``order`` so dependencies hold (PV and EV before
BESS, which dispatches against net demand).

Adding a new DER type — a heat pump, an electrolyser, a community battery —
is now: write a ``DERPlugin``, call ``register()``. No edits to the generator.
"""

import numpy as np

from app.profiles.bess_model import BESS_CONFIGS, create_bess, degrade_soh, dispatch_bess
from app.profiles.diversity import diversified_shape, households_for_bus
from app.profiles.ev_model import (
    EV_CONFIGS,
    create_ev_profile,
    generate_diversified_ev_profile,
)
from app.profiles.load_shapes import get_load_shape
from app.profiles.solar_model import generate_solar_profile


class GenerationContext:
    """Per-bus inputs + the accumulating series each plugin contributes to."""

    def __init__(self, bus, bus_seed, *, timesteps, resolution_minutes, season,
                 reactive_floor, diversity, ev_diversity,
                 ev_charging_mode, ev_offpeak_start_hour, custom_store,
                 pv_profile=None, ev_profile=None,
                 bess_dispatch_mode="self_consumption",
                 bess_charge_window=(1.0, 6.0), bess_discharge_window=(17.0, 21.0),
                 temperature=None, irradiance=None, day_type="weekday",
                 bess_initial_soc=0.5, bess_initial_soh=1.0):
        self.bus = bus
        self.bus_seed = bus_seed
        self.timesteps = timesteps
        self.resolution_minutes = resolution_minutes
        self.season = season
        self.reactive_floor = reactive_floor
        self.diversity = diversity
        self.ev_diversity = ev_diversity
        self.ev_charging_mode = ev_charging_mode
        self.ev_offpeak_start_hour = ev_offpeak_start_hour
        self.custom_store = custom_store
        # Optional scenario-level custom day-shapes (store names, kind-checked).
        self.pv_profile = pv_profile
        self.ev_profile = ev_profile
        self.bess_dispatch_mode = bess_dispatch_mode
        self.bess_charge_window = bess_charge_window
        self.bess_discharge_window = bess_discharge_window
        self.temperature = temperature
        self.irradiance = irradiance        # measured GHI trace (W/m2) or None
        self.day_type = day_type            # "weekday" | "weekend"
        self.bess_initial_soc = bess_initial_soc  # carried across days
        self.bess_initial_soh = bess_initial_soh  # state of health, carried across days
        self.series: dict[str, np.ndarray] = {}
        self.meta: dict = {}

    @property
    def step_hours(self) -> float:
        return self.resolution_minutes / 60.0

    def get(self, key: str) -> np.ndarray:
        """A contributed series, or zeros if no plugin has produced it yet."""
        s = self.series.get(key)
        return s if s is not None else np.zeros(self.timesteps)


class DERPlugin:
    """Base class: one DER device type.

    ``net_load`` declares how this device's contributed series feed the bus net
    load, as ``{series_key: sign}`` where ``+1`` adds to net demand (a
    consumer, e.g. load or EV) and ``-1`` reduces it (generation or storage
    discharge, e.g. PV or BESS). The generator sums these across all installed
    plugins, so a new DER type affects net load — and therefore the downstream
    simulation — purely by registering, with no edits to the generator.
    """

    name: str = "der"
    order: int = 100
    net_load: dict[str, float] = {}

    def applies_to(self, bus: dict) -> bool:
        raise NotImplementedError

    def generate(self, ctx: GenerationContext) -> None:
        raise NotImplementedError


class LoadPlugin(DERPlugin):
    name, order = "load", 10
    net_load = {"load_kw": +1.0}

    def applies_to(self, bus):
        return float(bus.get("base_load_kw", 0.0)) > 0

    def generate(self, ctx):
        bus = ctx.bus
        base_kw = float(bus["base_load_kw"])
        base_kvar = float(bus["base_load_kvar"])
        shape = get_load_shape(
            bus["customer_class"], seed=ctx.bus_seed, season=ctx.season,
            timesteps=ctx.timesteps, custom_store=ctx.custom_store,
            day_type=ctx.day_type,
        )
        if ctx.diversity["enabled"]:
            shape = diversified_shape(
                shape,
                n_households=households_for_bus(
                    base_kw, ctx.diversity["admd_kw"], ctx.diversity["max_households"]
                ),
                seed=ctx.bus_seed,
                sigma_minutes=ctx.diversity["sigma_minutes"],
                magnitude_cv=ctx.diversity["magnitude_cv"],
                appliance_cv=ctx.diversity["appliance_cv"],
                resolution_minutes=ctx.resolution_minutes,
                preserve_peak=ctx.diversity["preserve_peak"],
            )
        f = ctx.reactive_floor
        ctx.series["load_kw"] = base_kw * shape
        ctx.series["load_kvar"] = base_kvar * (f + (1.0 - f) * shape)


class PVPlugin(DERPlugin):
    name, order = "pv", 20
    net_load = {"pv_kw": -1.0}

    def applies_to(self, bus):
        return float(bus.get("pv_capacity_kw", 0.0)) > 0

    def generate(self, ctx):
        capacity = float(ctx.bus["pv_capacity_kw"])
        if ctx.pv_profile and ctx.custom_store is not None:
            # Measured PV day: uploaded per-unit shape x capacity (the shape
            # already embodies weather/clouds, so no synthetic derating).
            shape = ctx.custom_store.get_shape(ctx.pv_profile, ctx.timesteps, kind="pv")
            ctx.series["pv_kw"] = capacity * shape
            return
        ctx.series["pv_kw"] = generate_solar_profile(
            capacity, seed=ctx.bus_seed,
            season=ctx.season, timesteps=ctx.timesteps,
            temperature=ctx.temperature, irradiance=ctx.irradiance,
        )


class EVPlugin(DERPlugin):
    name, order = "ev", 30
    net_load = {"ev_charge_kw": +1.0}

    def applies_to(self, bus):
        return bus.get("ev_config") is not None

    def generate(self, ctx):
        base_kw = float(ctx.bus.get("base_load_kw", 0.0))
        # An EV bus represents its whole household fleet, so its EV demand is the
        # AGGREGATE of that many vehicles — not one representative charger. The
        # fleet size scales with the bus (its households); the diversified shape is
        # sampled from a capped subset for performance and scaled up to the fleet.
        fleet = households_for_bus(
            base_kw, ctx.diversity["admd_kw"], ctx.diversity["max_households"]
        )
        if fleet < 1:
            return
        rate = float(ctx.bus.get("ev_charge_rate_kw") or 0.0) or float(
            EV_CONFIGS.get(ctx.bus["ev_config"], {}).get("charge_rate_kw", 0.0)
        )
        if ctx.ev_profile and ctx.custom_store is not None:
            # Measured per-charger EV day: uploaded per-unit shape x charger
            # rating, scaled to the bus fleet (replaces the session model).
            shape = ctx.custom_store.get_shape(ctx.ev_profile, ctx.timesteps, kind="ev")
            ctx.series["ev_charge_kw"] = rate * shape * fleet
            ctx.meta["ev_fleet_capacity_kw"] = fleet * rate
            return
        sample = min(fleet, ctx.ev_diversity["max_evs"])
        if ctx.ev_diversity["enabled"]:
            res = generate_diversified_ev_profile(
                ctx.bus["ev_config"], n_evs=sample, seed=ctx.bus_seed,
                arrival_sigma_minutes=ctx.ev_diversity["arrival_sigma_minutes"],
                timesteps=ctx.timesteps, mode=ctx.ev_charging_mode,
                offpeak_start_hour=ctx.ev_offpeak_start_hour,
            )
        else:
            res = create_ev_profile(
                ctx.bus["ev_config"], seed=ctx.bus_seed, timesteps=ctx.timesteps,
                mode=ctx.ev_charging_mode, offpeak_start_hour=ctx.ev_offpeak_start_hour,
            )
        # generate_diversified_ev_profile / create_ev_profile give one charger's
        # demand; scale by the fleet size for the bus aggregate. Report the true
        # EV charging capacity (fleet x charger rating) to match.
        ctx.series["ev_charge_kw"] = res["ev_charge_kw"] * fleet
        ctx.meta["ev_fleet_capacity_kw"] = fleet * rate


class BESSPlugin(DERPlugin):
    name, order = "bess", 40
    # Positive bess_power_kw is discharge, which reduces net load.
    net_load = {"bess_power_kw": -1.0}

    def applies_to(self, bus):
        return bus.get("bess_config") is not None

    def generate(self, ctx):
        bus = ctx.bus
        # Rated (nameplate) capacity, before any state-of-health derating.
        nameplate = float(bus.get("bess_capacity_kwh") or 0.0)
        if nameplate <= 0:
            nameplate = float(BESS_CONFIGS.get(bus["bess_config"], {}).get("capacity_kwh", 0.0))

        # Per-bus overrides for capacity and the independent charge/discharge
        # power limits (falling back to the named config when unset/zero). The
        # usable capacity is the nameplate scaled by the carried state of health.
        bess = create_bess(
            bus["bess_config"], initial_soc=ctx.bess_initial_soc,
            initial_soh=ctx.bess_initial_soh,
            capacity_kwh=bus.get("bess_capacity_kwh") or None,
            max_charge_kw=bus.get("bess_max_charge_kw") or None,
            max_discharge_kw=bus.get("bess_max_discharge_kw") or None,
        )
        # Dispatch against total demand (load + EV) minus PV — contributed by the
        # load/EV/PV plugins that ran before this one. The mode selects how the
        # battery charges and discharges.
        res = dispatch_bess(
            ctx.bess_dispatch_mode,
            ctx.get("load_kw") + ctx.get("ev_charge_kw"), ctx.get("pv_kw"),
            bess, step_hours=ctx.step_hours,
            charge_window=ctx.bess_charge_window,
            discharge_window=ctx.bess_discharge_window,
        )
        ctx.series["bess_power_kw"] = res["bess_power_kw"]
        ctx.series["bess_soc"] = res["bess_soc"]
        ctx.meta["bess_capacity_kwh"] = nameplate  # report the rated capacity
        # Carried across days: final SoC and the aged state of health.
        ctx.meta["bess_final_soc"] = float(res["bess_soc"][-1])
        equivalent_cycles = res["energy_discharged_kwh"] / nameplate if nameplate > 0 else 0.0
        ctx.meta["bess_cycles_day"] = equivalent_cycles
        ctx.meta["bess_final_soh"] = degrade_soh(ctx.bess_initial_soh, equivalent_cycles, days=1)


_REGISTRY: dict[str, DERPlugin] = {}


def register(plugin: DERPlugin) -> None:
    _REGISTRY[plugin.name] = plugin


def installed_plugins() -> list[DERPlugin]:
    """All registered DER plugins, in dependency (execution) order."""
    return sorted(_REGISTRY.values(), key=lambda p: p.order)


def der_types() -> list[str]:
    return [p.name for p in installed_plugins()]


def compute_net_load(ctx: GenerationContext) -> np.ndarray:
    """Bus net load = sum of every installed plugin's signed contribution.

    Driven entirely by each plugin's ``net_load`` declaration, so a newly
    registered DER type flows into net load (and the downstream simulation)
    without touching the generator. With the built-in plugins this reduces
    exactly to ``load_kw - pv_kw + ev_charge_kw - bess_power_kw``.
    """
    total = np.zeros(ctx.timesteps)
    for plugin in installed_plugins():
        for key, sign in plugin.net_load.items():
            total = total + sign * ctx.get(key)
    return total


def contributed_series(ctx: GenerationContext) -> set[str]:
    """All series keys any installed plugin declares as net-load contributors."""
    return {key for plugin in installed_plugins() for key in plugin.net_load}


for _p in (LoadPlugin(), PVPlugin(), EVPlugin(), BESSPlugin()):
    register(_p)
