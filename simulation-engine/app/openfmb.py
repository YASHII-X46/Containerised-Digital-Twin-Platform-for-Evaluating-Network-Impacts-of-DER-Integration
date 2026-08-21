"""OpenFMB-structured JSON message builders for bus publishing."""

from datetime import datetime, timezone
from uuid import uuid4


def _build_envelope(profile: str, mrid: str, timestamp: str, readings: dict) -> dict:
    """Build the common OpenFMB message envelope."""
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


def build_voltage_reading(
    bus_id: int,
    timestamp: str,
    voltage_pu: float,
    is_violation: bool,
    violation_type: str | None = None,
) -> dict:
    """Build a VoltageReadingProfile message for a bus."""
    mrid = f"bus-{bus_id:03d}-voltage"
    readings = {
        "voltageMagnitude_pu": round(voltage_pu, 6),
        "isViolation": is_violation,
        "violationType": violation_type,
    }
    return _build_envelope("VoltageReadingProfile", mrid, timestamp, readings)


def build_thermal_reading(
    branch_id: int,
    timestamp: str,
    loading_pct: float,
    is_violation: bool,
) -> dict:
    """Build a ThermalReadingProfile message for a branch."""
    mrid = f"branch-{branch_id:03d}-thermal"
    readings = {
        "loadingPercent": round(loading_pct, 3),
        "isViolation": is_violation,
    }
    return _build_envelope("ThermalReadingProfile", mrid, timestamp, readings)


def build_ess_status(
    bus_id: int,
    timestamp: str,
    soc: float,
    power_kw: float,
    capacity_kwh: float,
    mode: str,
) -> dict:
    """Build an ESSStatusProfile message for a BESS."""
    mrid = f"bus-{bus_id:03d}-ess"
    readings = {
        "stateOfCharge": round(soc, 4),
        "activePower_kW": round(power_kw, 3),
        "capacityRating_kWh": round(capacity_kwh, 3),
        "operatingMode": mode,
    }
    return _build_envelope("ESSStatusProfile", mrid, timestamp, readings)


def build_ev_status(
    bus_id: int,
    timestamp: str,
    charge_kw: float,
    soc: float,
    mode: str,
) -> dict:
    """Build an EVStatusProfile message for EV charging."""
    mrid = f"bus-{bus_id:03d}-ev"
    readings = {
        "chargePower_kW": round(charge_kw, 3),
        "stateOfCharge": round(soc, 4),
        "chargingMode": mode,
    }
    return _build_envelope("EVStatusProfile", mrid, timestamp, readings)


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
) -> dict:
    """Build a DERStatusProfile message — a prosumer shadow twin's published state.

    This is what a twin broadcasts each timestep; the DR controller subscribes to
    it and decides the control response.
    """
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
    return _build_envelope("DERStatusProfile", mrid, timestamp, readings)


def build_der_control(
    bus_id: int,
    timestamp: str,
    pv_curtailment_kw: float,
    ev_curtailment_kw: float,
    bess_charge_kw: float,
) -> dict:
    """Build a DERControlProfile message — the DR controller's setpoint for a bus.

    ``curtailment_kW`` (PV) is kept for compatibility with existing OpenFMB consumers.
    """
    mrid = f"bus-{bus_id:03d}-der-control"
    readings = {
        "curtailment_kW": round(pv_curtailment_kw, 3),
        "evCurtailment_kW": round(ev_curtailment_kw, 3),
        "bessCharge_kW": round(bess_charge_kw, 3),
    }
    return _build_envelope("DERControlProfile", mrid, timestamp, readings)


def build_simulation_status(
    timestep: int,
    timestamp: str,
    converged: bool,
    total_losses_kw: float,
    total_power_kw: float,
    voltage_violations: int,
    thermal_violations: int,
) -> dict:
    """Build a SimulationStatusProfile message."""
    mrid = "simulation-engine-status"
    readings = {
        "timestep": timestep,
        "converged": converged,
        "totalLosses_kW": round(total_losses_kw, 3),
        "totalPower_kW": round(total_power_kw, 3),
        "voltageViolationCount": voltage_violations,
        "thermalViolationCount": thermal_violations,
    }
    return _build_envelope("SimulationStatusProfile", mrid, timestamp, readings)


def build_topic(profile: str, mrid: str, topic_prefix: str = "openfmb") -> str:
    """Construct an OpenFMB bus topic from profile and mRID."""
    return f"{topic_prefix}/{profile}/{mrid}"


def generate_mrid(entity_id: int, entity_type: str, prefix: str = "bus") -> str:
    """Generate a standardized mRID string."""
    return f"{prefix}-{entity_id:03d}-{entity_type}"
