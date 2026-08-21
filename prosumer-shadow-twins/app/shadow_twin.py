"""Prosumer shadow twins."""

from dataclasses import dataclass, field

from app.config import TwinConfig
from app.config import settings as default_settings
from app.openfmb import build_der_status, build_topic

# Per-timestep keys the status message already represents explicitly (or that are
# bookkeeping, not DER series). Anything else in a bus timeseries is an extra DER
# series forwarded verbatim so a matching control plugin can act on it.
_BUILTIN_SERIES_KEYS = frozenset({
    "timestep", "timestamp", "load_kw", "load_kvar", "pv_kw",
    "bess_power_kw", "bess_soc", "ev_charge_kw", "net_load_kw", "other_der_kw",
})


@dataclass
class ProsumerShadowTwin:
    """Per-bus mirror of one prosumer's expected and actual state."""

    bus_id: int
    pv_capacity_kw: float
    bess_capacity_kwh: float
    ev_charge_rate_kw: float
    timeseries: list[dict]

    curtailed_pv_kwh: float = 0.0
    deferred_ev_kwh: float = 0.0
    shared_pv_kwh: float = 0.0
    other_shed_kwh: float = 0.0
    bess_support_kwh: float = 0.0   # battery discharged for voltage support
    measured_voltage_pu: list[float] = field(default_factory=list)

    @property
    def is_prosumer(self) -> bool:
        return self.pv_capacity_kw > 0 or self.bess_capacity_kwh > 0

    def expected_pv_kw(self, timestep: int) -> float:
        return float(self.timeseries[timestep].get("pv_kw", 0.0))

    def expected_ev_kw(self, timestep: int) -> float:
        return float(self.timeseries[timestep].get("ev_charge_kw", 0.0))

    def soc(self, timestep: int) -> float:
        return float(self.timeseries[timestep].get("bess_soc", 0.0))

    def extra_readings(self, timestep: int) -> dict[str, float]:
        """Any DER series beyond the built-ins, keyed as carried in the profile.

        These are the contributions of additional Load Engine DER plugins (e.g.
        a heat pump's ``heatpump_kw``). Forwarding them lets a matching control
        plugin act on the device — fully modular DR end to end.
        """
        return {
            k: float(v)
            for k, v in self.timeseries[timestep].items()
            if k not in _BUILTIN_SERIES_KEYS
        }

    def status_message(
        self,
        timestep: int,
        voltage_pu: float,
        step_hours: float,
        timestamp: str,
        topic_prefix: str = "openfmb",
    ) -> tuple[str, dict]:
        ts = self.timeseries[timestep]
        payload = build_der_status(
            bus_id=self.bus_id,
            timestamp=timestamp,
            voltage_pu=voltage_pu,
            pv_kw=self.expected_pv_kw(timestep),
            ev_kw=self.expected_ev_kw(timestep),
            soc=self.soc(timestep),
            pv_capacity_kw=self.pv_capacity_kw,
            bess_capacity_kwh=self.bess_capacity_kwh,
            step_hours=step_hours,
            # Site demand backs export-limit (operating-envelope) control:
            # export = PV - (loadDemand + evCharge).
            loadDemand_kW=float(ts.get("load_kw", 0.0)) + float(ts.get("other_der_kw", 0.0)),
            **self.extra_readings(timestep),
        )
        return build_topic("DERStatusProfile", payload["mRID"], topic_prefix), payload

    def record(
        self,
        timestep: int,
        voltage_pu: float,
        step_hours: float,
        curtailed_pv_kw: float = 0.0,
        deferred_ev_kw: float = 0.0,
        shared_kw: float = 0.0,
        other_shed_kw: float = 0.0,
        support_kw: float = 0.0,
    ) -> None:
        self.measured_voltage_pu.append(voltage_pu)
        self.curtailed_pv_kwh += curtailed_pv_kw * step_hours
        self.deferred_ev_kwh += deferred_ev_kw * step_hours
        self.shared_pv_kwh += shared_kw * step_hours
        self.other_shed_kwh += other_shed_kw * step_hours
        self.bess_support_kwh += support_kw * step_hours

    def summary(self) -> dict:
        peak_v = max(self.measured_voltage_pu) if self.measured_voltage_pu else None
        min_v = min(self.measured_voltage_pu) if self.measured_voltage_pu else None
        return {
            "bus_id": self.bus_id,
            "pv_capacity_kw": round(self.pv_capacity_kw, 3),
            "bess_capacity_kwh": round(self.bess_capacity_kwh, 3),
            "curtailed_pv_kwh": round(self.curtailed_pv_kwh, 4),
            "deferred_ev_kwh": round(self.deferred_ev_kwh, 4),
            "shared_pv_kwh": round(self.shared_pv_kwh, 4),
            "other_shed_kwh": round(self.other_shed_kwh, 4),
            "bess_support_kwh": round(self.bess_support_kwh, 4),
            "peak_measured_voltage_pu": round(peak_v, 6) if peak_v is not None else None,
            "min_measured_voltage_pu": round(min_v, 6) if min_v is not None else None,
        }


def build_shadow_twins(
    profiles: dict, config: TwinConfig | None = None
) -> dict[int, ProsumerShadowTwin]:
    """Build one shadow twin per qualifying bus.

    `config` controls selection thresholds (which buses become twins). When
    omitted it falls back to the environment-backed defaults, whose all-zero
    thresholds reproduce the legacy "any DER present" rule.
    """
    config = config or TwinConfig.from_settings(default_settings)
    twins: dict[int, ProsumerShadowTwin] = {}
    for bus_id, bus in profiles["buses"].items():
        pv = float(bus.get("pv_capacity_kw", 0.0))
        bess = float(bus.get("bess_capacity_kwh", 0.0))
        ev = float(bus.get("ev_charge_rate_kw", 0.0))
        if config.qualifies(pv, bess, ev):
            twins[bus_id] = ProsumerShadowTwin(
                bus_id=bus_id,
                pv_capacity_kw=pv,
                bess_capacity_kwh=bess,
                ev_charge_rate_kw=ev,
                timeseries=bus["timeseries"],
            )
    return twins
