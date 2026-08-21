"""Pydantic data models and API schemas."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DoeConfig(BaseModel):
    """Dynamic-operating-envelope configuration (see app/control/envelopes.py)."""

    mode: Literal["off", "static", "dynamic"] = Field(
        default="off",
        description="'static' applies fixed_export_kw to every export site; "
        "'dynamic' computes per-site, per-interval limits from headroom.",
    )
    allocation: str = Field(
        default="equal",
        description="Allocation policy name from the envelope-allocation "
        "registry (built-ins: equal, prorata, max_total; registered custom "
        "policies are selectable; unknown names 400).",
    )
    method: Literal["sensitivity", "search"] = Field(
        default="sensitivity",
        description="'sensitivity' linearises with one perturbation per site; "
        "'search' binary-searches a uniform capability fraction per interval "
        "(exact; allocation is pro-rata by construction).",
    )
    fixed_export_kw: float = Field(
        default=1.5, ge=0.0,
        description="Static-mode per-site export limit (kW) — e.g. the "
        "1.5 kW fallback of SA Power Networks Flexible Exports.",
    )
    managed: bool = Field(
        default=False,
        description="Enforce through the DR coordination loop (utility "
        "publishes exportLimit_kW to each site controller) instead of "
        "autonomously at the inverter. Requires a coordination mode.",
    )


class SimulationRequest(BaseModel):
    scenario_name: str = Field(default="scenario")
    profiles: dict = Field(
        description="Inline profiles payload (Sim wire format), delivered over the "
        "message bus.",
    )
    network_id: str = Field(
        min_length=1,
        description="Network model to solve — required; must reference an "
        "uploaded network (see GET /networks). No default network is assumed.",
    )
    seed: int = Field(default=42)
    der_penetration_percent: float = Field(default=100, ge=0, le=500)
    coordination_mode: str = Field(
        default="uncoordinated",
        description="DR coordination strategy name (see GET /strategies), or "
        "'uncoordinated'. Validated against the strategy catalog.",
    )
    solve_mode: str = Field(
        default="balanced",
        description="Power-flow model: 'balanced' (symmetric three-phase) or "
        "'unbalanced' (honours per-bus phases).",
    )
    solver: str = Field(
        default="opendss",
        description="Power-flow solver backend name (see GET /solvers). Each "
        "solver runs as its own container implementing the solver bus "
        "contract; built-ins: 'opendss' (default), 'sincal'.",
    )
    twin_config: dict | None = Field(
        default=None,
        description="Optional prosumer shadow-twin configuration (selection "
        "thresholds / modelling assumptions). Forwarded to the prosumer-shadow-"
        "twins service over the bus when coordination is active; ignored "
        "otherwise. None uses that service's environment defaults.",
    )
    volt_var: bool = Field(
        default=False,
        description="Enable autonomous smart-inverter Volt-VAr (AS/NZS 4777.2): "
        "PV inverters set reactive power from local voltage during the solve.",
    )
    volt_watt: bool = Field(
        default=False,
        description="Enable autonomous smart-inverter Volt-Watt (AS/NZS 4777.2): "
        "PV inverters reduce real power linearly above the 1.09 pu knee, down "
        "to a 20% floor at 1.10 pu.",
    )
    doe: DoeConfig | None = Field(
        default=None,
        description="Export-limit scheme: omit (or mode 'off') for none, "
        "'static' for a fixed per-site export limit, 'dynamic' for operating "
        "envelopes computed from network headroom each interval.",
    )
    tariff: str = Field(
        default="tou_residential",
        description="Named tariff structure pricing the cost KPIs (see "
        "GET /tariffs). Registered tariffs are selectable; unknown names 400.",
    )

    @field_validator("solve_mode")
    @classmethod
    def _check_solve_mode(cls, v: str) -> str:
        mode = (v or "balanced").strip().lower()
        if mode not in ("balanced", "unbalanced"):
            raise ValueError("solve_mode must be 'balanced' or 'unbalanced'.")
        return mode


class NetworkUpload(BaseModel):
    """A user-supplied network definition (generic NetworkModel dict)."""

    id: str = Field(min_length=1, max_length=64)
    name: str | None = None
    base_voltage_kv: float = Field(gt=0)
    source_bus: int
    buses: list[dict]
    branches: list[dict]


class NetworkImport(BaseModel):
    """Import a network from an external format (PSS/E RAW/RAWX or CIM/CGMES)."""

    id: str = Field(min_length=1, max_length=64)
    content: str = Field(description="The raw file contents.")
    format: str | None = Field(
        default=None,
        description="raw | rawx | cim | json. Inferred from filename/content if omitted.",
    )
    filename: str | None = None


class BusVoltageResult(BaseModel):
    bus_id: int
    min_voltage_pu: float
    max_voltage_pu: float
    mean_voltage_pu: float
    violation_count: int


class BranchLoadingResult(BaseModel):
    branch_id: int
    max_loading_pct: float
    violation_count: int


class SimulationResponse(BaseModel):
    status: str
    scenario_name: str
    network_id: str
    seed: int
    der_penetration_percent: float
    coordination_mode: str
    solve_mode: str = "balanced"
    solver: str = "opendss"
    total_timesteps: int
    converged_timesteps: int
    resolution_minutes: int = 15
    total_voltage_violations: int
    total_thermal_violations: int
    min_voltage_pu: float
    max_voltage_pu: float
    max_loading_pct: float
    total_losses_kwh: float
    simulation_time_seconds: float
    buses_with_pv: int = 0
    buses_with_bess: int = 0
    buses_with_ev: int = 0
    # DR controller / prosumer shadow-twin outcomes (0 when uncoordinated).
    prosumer_twins: int = 0
    buses_curtailed: int = 0
    total_pv_curtailed_kwh: float = 0.0
    total_ev_deferred_kwh: float = 0.0
    total_pv_shared_kwh: float = 0.0
    # Battery energy discharged for under-voltage (peak) support.
    total_bess_support_kwh: float = 0.0
    # Custom (plugin) controllable-DER load shed under coordination.
    total_other_shed_kwh: float = 0.0
    # Dynamic-operating-envelope outcome ('off' when no scheme ran).
    doe_mode: str = "off"
    tariff: str = "tou_residential"
    doe_allocation: str | None = None
    doe_curtailed_kwh: float = 0.0
    doe_envelope_utilisation_pct: float = 0.0
    kpis: dict = {}
    result_series: dict = {}  # per-timestep chart data (carried over the bus)
    bus_voltage_summary: list[BusVoltageResult]
    branch_loading_summary: list[BranchLoadingResult]
