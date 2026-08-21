"""Pydantic v2 schemas for Load Engine v5.0 API request/response contracts."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class BusConfig(BaseModel):
    """Full per-bus configuration: load profile + DER assignment.

    `customer_class` is a built-in archetype name or "custom:<name>" for an
    uploaded profile. Bus ids are unconstrained so any network model works.
    """

    bus_id: int = Field(ge=0)
    base_load_kw: float = Field(ge=0)
    base_load_kvar: float = Field(ge=0)
    customer_class: str
    pv_capacity_kw: float = Field(ge=0, default=0.0)
    bess_config: str | None = None
    bess_capacity_kwh: float = Field(ge=0, default=0.0)
    bess_max_charge_kw: float = Field(
        ge=0, default=0.0,
        description="Max battery charge power (kW). 0 uses the named config's rate.",
    )
    bess_max_discharge_kw: float = Field(
        ge=0, default=0.0,
        description="Max battery discharge power (kW). 0 uses the named config's rate.",
    )
    ev_config: str | None = None
    ev_charge_rate_kw: float = Field(ge=0, default=0.0)


class NetworkBusSpec(BaseModel):
    """Minimal bus description used for auto-assignment over any network."""

    bus_id: int = Field(ge=0)
    base_load_kw: float = Field(ge=0, default=0.0)
    base_load_kvar: float | None = Field(ge=0, default=None)


class DiversityConfig(BaseModel):
    """Per-bus load-diversity (household aggregation) settings.

    A bus aggregates many households whose demand peaks do not coincide, so the
    single archetype/custom day-shape is turned into the diversified bus demand.
    Set `enabled=False` to reproduce the legacy synchronised (coincident) loads.
    """

    enabled: bool = True
    admd_kw: float = Field(
        default=1.5, gt=0,
        description="Assumed after-diversity maximum demand per household (kW); "
        "sets how many households a bus aggregates (base_load_kw / admd_kw). "
        "Default 1.5 kW is a nationally representative measured diversified "
        "residential ADMD across Australian DNSPs (established areas ~1-2 kW; "
        "new all-electric estates ~3-5 kW). Note DNSP *design* allowances for new "
        "connections are higher (~6-8 kVA/dwelling); set per DNSP standard.",
    )
    sigma_minutes: float = Field(
        default=45.0, ge=0,
        description="Std-dev of the per-household activity time-shift (minutes).",
    )
    magnitude_cv: float = Field(
        default=0.4, ge=0,
        description="Coefficient of variation of per-household magnitude.",
    )
    appliance_cv: float = Field(
        default=0.1, ge=0,
        description="Per-timestep multiplicative appliance switching-noise CV.",
    )
    max_households: int = Field(
        default=400, ge=1,
        description="Cap on households per bus (performance bound).",
    )
    preserve_peak: bool = Field(
        default=True,
        description="Renormalise so the bus peak equals base_load_kw "
        "(treat base_load_kw as the bus ADMD). False keeps the natural "
        "diversified peak (< base_load_kw).",
    )


class EVDiversityConfig(BaseModel):
    """Per-bus EV arrival-time diversity (uncontrolled charging).

    A bus serves several EVs that do not all plug in at the same minute. The bus
    EV demand is the mean of many single-EV profiles with staggered arrivals, so
    the synchronous charging block is smeared across the arrival window (same
    daily energy per charger, lower diversified peak). `enabled=False` reverts to
    a single EV per bus.
    """

    enabled: bool = True
    arrival_sigma_minutes: float = Field(
        default=90.0, ge=0,
        description="Std-dev of EV plug-in times across the bus (minutes). "
        "Default 90 min reflects the broad evening home-arrival spread seen in "
        "EV charging session data (e.g. ACN-Data).",
    )
    max_evs: int = Field(
        default=64, ge=1,
        description="Cap on modelled EVs per bus (performance/smoothness bound).",
    )


class SimulationRequest(BaseModel):
    scenario_name: str = Field(default="scenario")
    seed: int = Field(default=42)
    network_id: str = Field(
        default="",
        description="Network the profiles target (recorded as metadata). Set by "
        "the UI to the selected network; no specific network is assumed.",
    )
    der_penetration_percent: float = Field(default=100, ge=0, le=500)
    timesteps: int = Field(
        default=96, ge=1, le=1440,
        description="Steps per day; with resolution_minutes must span one 24-h day.",
    )
    resolution_minutes: int = Field(default=15, ge=1, le=1440)
    days: int = Field(
        default=1, ge=1, le=31,
        description="Number of days to simulate. The per-day shape repeats with "
        "day-to-day variation; the total horizon is days x timesteps.",
    )
    pv_buses: list[int] | None = Field(
        default=None,
        description="Buses that receive PV in auto-assign mode "
        "(default: all load buses).",
    )
    network_buses: list[NetworkBusSpec] | None = Field(
        default=None,
        description="Bus list of the target network for auto-assignment. The "
        "engine ships no built-in network: supply this (or explicit bus_data).",
    )
    bus_data: list[BusConfig] | None = Field(
        default=None,
        description="Explicit per-bus configuration; overrides auto-assignment.",
    )
    bess_penetration: float = Field(ge=0.0, le=1.0, default=0.3)
    bess_config: str = Field(default="powerwall_2")
    bess_dispatch_mode: Literal["self_consumption", "time_of_use"] = Field(
        default="self_consumption",
        description="Battery dispatch strategy: self_consumption (charge from PV, "
        "discharge to load) or time_of_use (charge off-peak, discharge at peak).",
    )
    bess_charge_window: list[float] = Field(
        default=[1.0, 6.0],
        description="Time-of-use charge window [start_hour, end_hour], 24-h clock.",
    )
    bess_discharge_window: list[float] = Field(
        default=[17.0, 21.0],
        description="Time-of-use discharge window [start_hour, end_hour], 24-h clock.",
    )
    ev_penetration: float = Field(ge=0.0, le=1.0, default=0.2)
    ev_config: str = Field(default="level2_7kw")
    pv_profile: str | None = Field(
        default=None,
        description="Optional custom PV day-shape for every PV bus: the name of "
        "an uploaded kind='pv' profile (with or without the 'custom:' prefix). "
        "Replaces the clear-sky/cloud model with the measured shape.",
    )
    ev_profile: str | None = Field(
        default=None,
        description="Optional custom per-charger EV day-shape for every EV bus: "
        "an uploaded kind='ev' profile. Replaces the charging-session model; "
        "still scaled by charger rating and fleet size.",
    )
    ev_charging_mode: Literal["uncontrolled", "offpeak", "smart"] = Field(
        default="uncontrolled",
        description="EV charging strategy: uncontrolled (plug-in), offpeak "
        "(timer), or smart (valley-fill spread).",
    )
    ev_offpeak_start_hour: float = Field(
        default=23.0, ge=0.0, le=24.0,
        description="Hour the off-peak timer starts charging (offpeak mode).",
    )
    season: Literal["summer", "winter", "shoulder"] = Field(default="summer")
    weather_source: Literal["none", "synthetic", "file"] = Field(
        default="none",
        description="Weather source for temperature-driven HVAC, PV derating, "
        "and measured-irradiance PV where available: 'none' (fixed season "
        "curves), 'synthetic' (offline diurnal model), or 'file' (local CSV "
        "of hourly temp[,ghi] rows via WEATHER_FILE).",
    )
    reactive_floor: float = Field(
        default=0.0, ge=0.0, lt=1.0,
        description="Constant fraction of base_load_kvar always present, so "
        "power factor degrades at light load. 0 = constant power factor "
        "(legacy); ~0.15 is realistic.",
    )
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)
    ev_diversity: EVDiversityConfig = Field(default_factory=EVDiversityConfig)

    @field_validator("bess_charge_window", "bess_discharge_window")
    @classmethod
    def _check_window(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("window must be [start_hour, end_hour].")
        if not all(0.0 <= h <= 24.0 for h in v):
            raise ValueError("window hours must be in [0, 24].")
        return v

    @model_validator(mode="after")
    def _check_single_day(self):
        # All profile shapes assume one 24-h day (np.linspace(0, 24, timesteps)),
        # while timestamps and the sim engine's energy maths advance by
        # resolution_minutes. They only stay consistent if the steps span exactly
        # one day, so reject combinations that do not.
        span = self.timesteps * self.resolution_minutes
        if span != 1440:
            raise ValueError(
                f"timesteps × resolution_minutes must equal 1440 (one 24-h day); "
                f"got {self.timesteps} × {self.resolution_minutes} = {span}."
            )
        return self


class CustomProfileUpload(BaseModel):
    """A user-supplied daily load shape.

    Provide either `values` (list of numbers) or `csv_text` (pasted CSV).
    Values are normalised to per-unit of peak on save.
    """

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["load", "pv", "ev"] = Field(
        default="load",
        description="What the shape drives: a load customer class, a measured "
        "PV day (scenario pv_profile), or a per-charger EV day (ev_profile).",
    )
    description: str = Field(default="")
    values: list[float] | None = None
    csv_text: str | None = None


class BusSummary(BaseModel):
    bus_id: int
    customer_class: str
    peak_load_kw: float
    peak_pv_kw: float
    min_net_load_kw: float
    bess_capacity_kwh: float = 0.0
    bess_soh: float = 1.0
    bess_cycles: float = 0.0
    ev_charge_rate_kw: float = 0.0


class SimulationResponse(BaseModel):
    status: str
    scenario_name: str
    network_id: str = ""
    seed: int
    der_penetration_percent: float
    total_buses: int
    timesteps: int
    resolution_minutes: int
    buses_with_pv: int
    buses_with_bess: int
    total_bess_capacity_kwh: float
    total_bess_cycles: float = 0.0
    mean_bess_soh: float = 1.0
    buses_with_ev: int
    total_ev_charge_capacity_kw: float
    peak_total_load_kw: float
    sum_bus_peak_load_kw: float = 0.0
    coincidence_factor: float = 1.0
    total_pv_capacity_kw: float
    peak_total_pv_kw: float
    min_net_load_kw: float
    bus_summaries: list[BusSummary]
