import dataclasses
from dataclasses import dataclass, replace

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BUS_PREFIX: str = "openfmb"
    BUS_TRANSPORT: str = "nats"
    NATS_URL: str = "nats://localhost:4222"

    # --- Prosumer-twin selection thresholds (env-overridable defaults) ------
    # A bus becomes a shadow twin when one of its DERs exceeds the threshold.
    # All-zero thresholds reproduce the legacy "any DER present" rule.
    MIN_PV_KW: float = 0.0
    MIN_BESS_KWH: float = 0.0
    MIN_EV_KW: float = 0.0
    # Track EV-only buses (no PV/BESS) as twins.
    INCLUDE_EV_ONLY: bool = True

    # --- Modelling assumptions used when a reading is missing ---------------
    NOMINAL_VOLTAGE_PU: float = 1.0
    DEFAULT_STEP_HOURS: float = 0.25

    model_config = {"env_prefix": ""}


@dataclass(frozen=True)
class TwinConfig:
    """Which buses become prosumer twins, plus modelling assumptions.

    The service builds one from the environment-backed ``Settings`` and lets the
    caller override any field per session via the ``start`` command's ``config``
    block — so twin selection and assumptions are tunable without redeploying.
    """

    min_pv_kw: float = 0.0
    min_bess_kwh: float = 0.0
    min_ev_kw: float = 0.0
    include_ev_only: bool = True
    nominal_voltage_pu: float = 1.0
    default_step_hours: float = 0.25

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TwinConfig":
        return cls(
            min_pv_kw=settings.MIN_PV_KW,
            min_bess_kwh=settings.MIN_BESS_KWH,
            min_ev_kw=settings.MIN_EV_KW,
            include_ev_only=settings.INCLUDE_EV_ONLY,
            nominal_voltage_pu=settings.NOMINAL_VOLTAGE_PU,
            default_step_hours=settings.DEFAULT_STEP_HOURS,
        )

    def merged(self, overrides: dict | None) -> "TwinConfig":
        """Return a copy with any recognised, non-null override keys applied."""
        if not overrides:
            return self
        fields = {f.name for f in dataclasses.fields(self)}
        clean = {k: v for k, v in overrides.items() if k in fields and v is not None}
        return replace(self, **clean)

    def qualifies(self, pv_kw: float, bess_kwh: float, ev_kw: float) -> bool:
        """Whether a bus with these DER capacities should get a shadow twin."""
        if pv_kw > self.min_pv_kw or bess_kwh > self.min_bess_kwh:
            return True
        return self.include_ev_only and ev_kw > self.min_ev_kw

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


settings = Settings()
