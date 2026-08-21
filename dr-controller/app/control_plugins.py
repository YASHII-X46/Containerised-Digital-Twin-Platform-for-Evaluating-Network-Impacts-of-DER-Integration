"""Pluggable DR control-device registry.

Each controllable device (PV curtailment, EV deferral, BESS self-absorption — and
any device a user adds) is a ``ControlPlugin`` that reads the per-bus DER status
and writes its setpoint into a shared ``ControlContext``. ``DRController`` simply
runs whatever plugins are *installed*, in ascending ``order`` so couplings hold
(BESS absorbs PV excess before PV curtails the remainder).

Adding a new controllable DER — a heat pump that sheds, a smart water heater,
an electrolyser — is: write a ``ControlPlugin``, call ``register()``. No edits
to the control law. Built-in plugins reproduce the volt-watt PV/EV/BESS behaviour
exactly. This mirrors the Load Engine's DER-generation plugin registry.
"""


def _clip01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


class ControlContext:
    """Per-bus readings + droop signals + the accumulating control setpoints.

    A plugin reads ``ctx.reading(...)`` and the voltage-droop signals, then writes
    into ``ctx.setpoints``. ``over_signal``/``under_signal`` are the volt-watt
    droop fractions in [0, 1] for over-/under-voltage (mutually exclusive).
    """

    def __init__(self, readings: dict, *, v_upper: float, v_lower: float,
                 droop_band: float, mode: str, control_ev: bool,
                 bess_max_soc: float, bess_min_soc: float = 0.1):
        self.readings = readings or {}
        self.v = float(self.readings.get("voltageMagnitude_pu", 1.0))
        self.v_upper = v_upper
        self.v_lower = v_lower
        self.droop_band = droop_band
        self.mode = mode
        self.control_ev = control_ev
        self.bess_max_soc = bess_max_soc
        self.bess_min_soc = bess_min_soc
        self.step_hours = float(self.readings.get("stepDuration_h", 0.25)) or 0.25
        self.over_signal = (
            _clip01((self.v - v_upper) / droop_band) if self.v > v_upper else 0.0
        )
        self.under_signal = (
            _clip01((v_lower - self.v) / droop_band) if self.v < v_lower else 0.0
        )
        # PV over-voltage reduction already met by storage; PV curtails the rest.
        self.absorbed_kw = 0.0
        self.setpoints: dict[str, float] = {}

    def reading(self, key: str, default: float = 0.0) -> float:
        return float(self.readings.get(key, default))


class ControlPlugin:
    """Base class: one controllable DER device."""

    name: str = "control"
    order: int = 100

    def compute(self, ctx: ControlContext) -> None:
        raise NotImplementedError


class BessAbsorbPlugin(ControlPlugin):
    """dr_p2p: charge the local battery from PV excess before curtailing PV."""

    name, order = "bess", 10

    def compute(self, ctx):
        if ctx.mode != "dr_p2p" or ctx.over_signal <= 0.0:
            return
        pv = ctx.reading("pvOutput_kW")
        bess_capacity = ctx.reading("bessCapacity_kWh")
        if pv <= 0.0 or bess_capacity <= 0.0:
            return
        desired = ctx.over_signal * pv
        headroom_kw = max(
            0.0, (ctx.bess_max_soc - ctx.reading("stateOfCharge")) * bess_capacity
        ) / ctx.step_hours
        charge = min(desired, headroom_kw)
        if charge > 0.0:
            ctx.setpoints["bess_charge_kw"] = charge
            ctx.absorbed_kw += charge


class BessSupportPlugin(ControlPlugin):
    """Discharge the battery to support under-voltage (peak support).

    The mirror of ``BessAbsorbPlugin``: on under-voltage, the battery injects
    real power in proportion to the droop signal, bounded by the energy above
    its minimum state of charge. Runs before EV deferral, so stored energy
    supports the peak before customer charging is deferred.
    """

    name, order = "bess_support", 12

    def compute(self, ctx):
        if ctx.under_signal <= 0.0:
            return
        capacity = ctx.reading("bessCapacity_kWh")
        if capacity <= 0.0:
            return
        available_kw = max(
            0.0, (ctx.reading("stateOfCharge") - ctx.bess_min_soc) * capacity
        ) / ctx.step_hours
        support = ctx.under_signal * available_kw
        if support > 0.0:
            ctx.setpoints["bess_discharge_kw"] = support


class PvCurtailPlugin(ControlPlugin):
    """Volt-Watt PV curtailment for the over-voltage not absorbed by storage."""

    name, order = "pv", 20

    def compute(self, ctx):
        pv = ctx.reading("pvOutput_kW")
        if ctx.over_signal <= 0.0 or pv <= 0.0:
            return
        curtail = ctx.over_signal * pv - ctx.absorbed_kw
        if curtail > 0.0:
            ctx.setpoints["pv_curtailment_kw"] = curtail


class PvReactivePlugin(ControlPlugin):
    """Commanded inverter reactive power (a DERMS VAr dispatch).

    Distinct from the autonomous Volt-VAr mode: this is a controller-issued
    setpoint. Injects VArs on under-voltage and absorbs them on over-voltage,
    scaled by the droop signal up to 44% of the inverter rating (the AS/NZS
    4777.2 reactive capability).
    """

    name, order = "pv_reactive", 22

    Q_MAX_FRAC = 0.44

    def compute(self, ctx):
        rating = ctx.reading("pvCapacity_kW")
        if rating <= 0.0:
            return
        q = 0.0
        if ctx.under_signal > 0.0:
            q = self.Q_MAX_FRAC * rating * ctx.under_signal      # inject
        elif ctx.over_signal > 0.0:
            q = -self.Q_MAX_FRAC * rating * ctx.over_signal      # absorb
        if q:
            ctx.setpoints["pv_reactive_kvar"] = q


class EnvelopeExportPlugin(ControlPlugin):
    """Hold site export at the published operating-envelope limit.

    Managed dynamic-operating-envelope enforcement: when a status carries an
    ``exportLimit_kW`` (published by the utility side), the excess of
    ``PV - (load + EV)`` over the limit is stored in the battery first (within
    the SOC headroom not already committed by other plugins) and the remainder
    is curtailed. Runs after the voltage-based plugins; curtailment combines by
    maximum, so both the voltage response and the envelope hold.
    """

    name, order = "envelope", 25

    def compute(self, ctx):
        if "exportLimit_kW" not in ctx.readings:
            return
        limit = ctx.reading("exportLimit_kW")
        pv = ctx.reading("pvOutput_kW")
        demand = ctx.reading("loadDemand_kW") + ctx.reading("evCharge_kW")
        excess = pv - demand - limit
        if excess <= 0.0:
            return
        bess_capacity = ctx.reading("bessCapacity_kWh")
        committed = ctx.setpoints.get("bess_charge_kw", 0.0)
        absorb = 0.0
        if bess_capacity > 0.0:
            headroom_kw = max(
                0.0, (ctx.bess_max_soc - ctx.reading("stateOfCharge")) * bess_capacity
            ) / ctx.step_hours - committed
            absorb = min(excess, max(0.0, headroom_kw))
            if absorb > 0.0:
                ctx.setpoints["bess_charge_kw"] = committed + absorb
        remainder = excess - absorb
        if remainder > 0.0:
            ctx.setpoints["pv_curtailment_kw"] = max(
                ctx.setpoints.get("pv_curtailment_kw", 0.0), remainder
            )


class EvDeferPlugin(ControlPlugin):
    """Defer EV charging on under-voltage (when EV control is enabled)."""

    name, order = "ev", 30

    def compute(self, ctx):
        if not ctx.control_ev or ctx.under_signal <= 0.0:
            return
        ev = ctx.reading("evCharge_kW")
        if ev > 0.0:
            ctx.setpoints["ev_curtailment_kw"] = ctx.under_signal * ev


_REGISTRY: dict[str, ControlPlugin] = {}


def register(plugin: ControlPlugin) -> None:
    _REGISTRY[plugin.name] = plugin


def installed_control_plugins() -> list[ControlPlugin]:
    """All registered control plugins, in execution (dependency) order."""
    return sorted(_REGISTRY.values(), key=lambda p: p.order)


def control_plugin_names() -> list[str]:
    return [p.name for p in installed_control_plugins()]


def compute_setpoints(readings: dict, *, v_upper: float, v_lower: float,
                      droop_band: float, mode: str, control_ev: bool,
                      bess_max_soc: float, bess_min_soc: float = 0.1) -> dict[str, float]:
    """Run every installed control plugin and return the accumulated setpoints."""
    ctx = ControlContext(
        readings, v_upper=v_upper, v_lower=v_lower, droop_band=droop_band,
        mode=mode, control_ev=control_ev, bess_max_soc=bess_max_soc,
        bess_min_soc=bess_min_soc,
    )
    for plugin in installed_control_plugins():
        plugin.compute(ctx)
    return ctx.setpoints


for _p in (BessAbsorbPlugin(), BessSupportPlugin(), PvCurtailPlugin(),
           PvReactivePlugin(), EnvelopeExportPlugin(), EvDeferPlugin()):
    register(_p)
