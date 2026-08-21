"""Prosumer shadow-twins NATS service."""

import logging
import os
import tempfile
import time

from app.bus import BusParticipant, make_transport
from app.config import TwinConfig
from app.config import settings as default_settings
from app.shadow_twin import build_shadow_twins

logger = logging.getLogger(__name__)

# Readiness marker for the container healthcheck: written once the bus
# participant is connected and subscribed (see main()).
# Path is overridable so a container healthcheck can probe a known location
# (the Windows images point READY_FILE at C:\ready\service.ready — the Linux
# default temp dir is not a convenient probe target there).
READY_FILE = os.environ.get(
    "READY_FILE", os.path.join(tempfile.gettempdir(), "service.ready")
)


def _mark_ready() -> None:
    try:
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            fh.write("ready")
    except OSError as exc:  # pragma: no cover - non-fatal
        logger.warning("Could not write readiness file %s: %s", READY_FILE, exc)


def _normalize_profiles(profiles: dict) -> dict:
    buses = {int(bus_id): data for bus_id, data in profiles.get("buses", {}).items()}
    metadata = dict(profiles.get("metadata", {}))
    metadata.setdefault("timesteps", len(next(iter(buses.values()))["timeseries"]) if buses else 0)
    metadata.setdefault("resolution_minutes", 15)
    metadata.setdefault("total_buses", len(buses))
    return {"metadata": metadata, "buses": buses}


def _float_map(values: dict | None) -> dict[int, float]:
    return {int(key): float(value) for key, value in (values or {}).items()}


class ProsumerShadowTwinsService:
    """Command handlers for the prosumer shadow-twins container."""

    def __init__(self, settings=default_settings):
        self.settings = settings
        # Environment-backed default twin configuration; per-session `start`
        # payloads may override any field.
        self.twin_config = TwinConfig.from_settings(settings)
        self._sessions: dict[str, dict] = {}

    def register(self, participant: BusParticipant) -> None:
        participant.on_command("start", self.start)
        participant.on_command("status", self.status)
        participant.on_command("record", self.record)
        participant.on_command("summary", self.summary)
        participant.on_command("config", self.config)
        participant.on_command("stop", self.stop)

    def config(self, payload: dict) -> dict:
        """Return the default twin configuration (env-backed)."""
        return {"config": self.twin_config.as_dict()}

    def start(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        config = self.twin_config.merged(payload.get("config"))
        profiles = _normalize_profiles(payload.get("profiles") or {})
        twins = build_shadow_twins(profiles, config)
        self._sessions[session_id] = {
            "twins": twins,
            "config": config,
            "mode": payload.get("mode") or "unknown",
            "messages": 0,
            "topic_prefix": payload.get("topic_prefix") or self.settings.BUS_PREFIX,
        }
        return {
            "session_id": session_id,
            "prosumer_twins": len(twins),
            "bus_ids": sorted(twins),
            "config": config.as_dict(),
        }

    def status(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        session = self._session(session_id)
        config = session["config"]
        voltages = _float_map(payload.get("voltages"))
        timestep = int(payload.get("timestep", 0))
        step_hours = float(payload.get("step_hours", config.default_step_hours))
        timestamp = str(payload.get("timestamp") or "")
        topic_prefix = str(payload.get("topic_prefix") or session["topic_prefix"])

        statuses = []
        for bus_id, twin in session["twins"].items():
            topic, status_payload = twin.status_message(
                timestep,
                voltages.get(bus_id, config.nominal_voltage_pu),
                step_hours,
                timestamp,
                topic_prefix,
            )
            statuses.append({"bus_id": bus_id, "topic": topic, "payload": status_payload})

        session["messages"] += len(statuses)
        return {
            "session_id": session_id,
            "prosumer_twins": len(session["twins"]),
            "statuses": statuses,
        }

    def record(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        session = self._session(session_id)
        config = session["config"]
        session["mode"] = payload.get("mode") or session["mode"]
        timestep = int(payload.get("timestep", 0))
        step_hours = float(payload.get("step_hours", config.default_step_hours))
        final_voltages = _float_map(payload.get("final_voltages"))
        controls = payload.get("controls") or {}

        for bus_id, twin in session["twins"].items():
            control = controls.get(str(bus_id)) or controls.get(bus_id) or {}
            twin.record(
                timestep,
                final_voltages.get(bus_id, config.nominal_voltage_pu),
                step_hours,
                curtailed_pv_kw=float(control.get("curtailed_pv_kw", 0.0)),
                deferred_ev_kw=float(control.get("deferred_ev_kw", 0.0)),
                shared_kw=float(control.get("shared_kw", 0.0)),
                other_shed_kw=float(control.get("other_shed_kw", 0.0)),
                support_kw=float(control.get("support_kw", 0.0)),
            )
        session["messages"] += len(controls)
        return {"session_id": session_id, "summary": self._summary(session)}

    def summary(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        return {"session_id": session_id, "summary": self._summary(self._session(session_id))}

    def stop(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        existed = session_id in self._sessions
        self._sessions.pop(session_id, None)
        return {"session_id": session_id, "stopped": existed}

    def _session(self, session_id: str) -> dict:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ValueError(f"Unknown prosumer-shadow-twins session '{session_id}'.") from exc

    @staticmethod
    def _summary(session: dict) -> dict:
        twin_summaries = [twin.summary() for twin in session["twins"].values()]
        return {
            "mode": session["mode"],
            "prosumer_twins": len(session["twins"]),
            "buses_curtailed": sum(
                1 for summary in twin_summaries if summary["curtailed_pv_kwh"] > 1e-6
            ),
            "buses_ev_deferred": sum(
                1 for summary in twin_summaries if summary["deferred_ev_kwh"] > 1e-6
            ),
            "buses_shed": sum(
                1 for summary in twin_summaries if summary["other_shed_kwh"] > 1e-6
            ),
            "total_pv_curtailed_kwh": round(
                sum(summary["curtailed_pv_kwh"] for summary in twin_summaries),
                4,
            ),
            "total_ev_deferred_kwh": round(
                sum(summary["deferred_ev_kwh"] for summary in twin_summaries),
                4,
            ),
            "total_bess_support_kwh": round(
                sum(summary["bess_support_kwh"] for summary in twin_summaries),
                4,
            ),
            "total_pv_shared_kwh": round(
                sum(summary["shared_pv_kwh"] for summary in twin_summaries),
                4,
            ),
            "total_other_shed_kwh": round(
                sum(summary["other_shed_kwh"] for summary in twin_summaries),
                4,
            ),
            "messages": session["messages"],
            "twins": twin_summaries,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = ProsumerShadowTwinsService(default_settings)
    participant = BusParticipant(
        make_transport(default_settings.BUS_TRANSPORT, default_settings, "prosumer-shadow-twins"),
        service="prosumer-shadow-twins",
        prefix=default_settings.BUS_PREFIX,
    )
    service.register(participant)
    participant.start()
    _mark_ready()  # bus connected + subscribed -> container is healthy
    logger.info("Prosumer shadow-twins service ready on %s", default_settings.BUS_PREFIX)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Stopping prosumer shadow-twins service")
    finally:
        participant.stop()


if __name__ == "__main__":
    main()
