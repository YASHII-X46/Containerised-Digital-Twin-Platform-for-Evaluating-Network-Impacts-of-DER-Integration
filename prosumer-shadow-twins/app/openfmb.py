"""OpenFMB-structured JSON message builders for prosumer shadow twins."""

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


def build_der_status(
    bus_id: int,
    timestamp: str,
    voltage_pu: float,
    pv_kw: float,
    ev_kw: float,
    soc: float,
    pv_capacity_kw: float,
    bess_capacity_kwh: float,
    step_hours: float,
    **extra_readings: float,
) -> dict:
    mrid = f"bus-{bus_id:03d}-der"
    readings = {
        "voltageMagnitude_pu": round(voltage_pu, 6),
        "pvOutput_kW": round(pv_kw, 3),
        "evCharge_kW": round(ev_kw, 3),
        "stateOfCharge": round(soc, 4),
        "pvCapacity_kW": round(pv_capacity_kw, 3),
        "bessCapacity_kWh": round(bess_capacity_kwh, 3),
        "stepDuration_h": round(step_hours, 6),
    }
    # Extra DER series (e.g. a custom heat-pump plugin's output) are carried under
    # their own keys so a matching control plugin can read them — keeps the status
    # schema extensible without edits here.
    for key, value in extra_readings.items():
        readings[key] = round(float(value), 3)
    return _build_envelope("DERStatusProfile", mrid, timestamp, readings)


def build_topic(profile: str, mrid: str, topic_prefix: str = "openfmb") -> str:
    return f"{topic_prefix}/{profile}/{mrid}"
