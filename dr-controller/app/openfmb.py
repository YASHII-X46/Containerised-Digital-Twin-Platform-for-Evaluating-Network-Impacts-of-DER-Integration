"""OpenFMB-structured JSON message builders for DR control."""

from datetime import datetime, timezone
from uuid import uuid4


def _build_envelope(profile: str, mrid: str, timestamp: str, readings: dict) -> dict:
    return {
        "profile": profile,
        "mRID": mrid,
        "timestamp": timestamp,
        "messageInfo": {
            "messageId": str(uuid4()),
            "messageTimeStamp": datetime.now(timezone.utc).isoformat(),
        },
        "readings": readings,
    }


def build_der_control(
    bus_id: int,
    timestamp: str,
    pv_curtailment_kw: float = 0.0,
    ev_curtailment_kw: float = 0.0,
    bess_charge_kw: float = 0.0,
    bess_discharge_kw: float = 0.0,
    pv_reactive_kvar: float = 0.0,
    **extra_setpoints: float,
) -> dict:
    mrid = f"bus-{bus_id:03d}-der-control"
    readings = {
        "curtailment_kW": round(pv_curtailment_kw, 3),
        "evCurtailment_kW": round(ev_curtailment_kw, 3),
        "bessCharge_kW": round(bess_charge_kw, 3),
        "bessDischarge_kW": round(bess_discharge_kw, 3),
        "pvReactive_kVAr": round(pv_reactive_kvar, 3),
    }
    # Setpoints from custom control plugins are carried under their own keys so the
    # control law stays extensible without changing the message schema.
    for key, value in extra_setpoints.items():
        readings[key] = round(float(value), 3)
    return _build_envelope("DERControlProfile", mrid, timestamp, readings)


def build_topic(profile: str, mrid: str, topic_prefix: str = "openfmb") -> str:
    return f"{topic_prefix}/{profile}/{mrid}"
