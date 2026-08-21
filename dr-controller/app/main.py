"""DR controller NATS service."""

import logging
import os
import re
import tempfile
import time

from app import strategy_registry
from app.bus import BusParticipant, make_transport
from app.config import settings as default_settings
from app.control_plugins import control_plugin_names
from app.openfmb import build_der_control, build_topic

logger = logging.getLogger(__name__)

# Readiness marker for the container healthcheck: written once the bus
# participant is connected and subscribed (see main()).
# Path is overridable so a container healthcheck can probe a known location
# (the Windows images point READY_FILE at C:\ready\service.ready — the Linux
# default temp dir is not a convenient probe target there).
READY_FILE = os.environ.get(
    "READY_FILE", os.path.join(tempfile.gettempdir(), "service.ready")
)

_BUS_RE = re.compile(r"bus-(\d+)-der")


def _mark_ready() -> None:
    try:
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            fh.write("ready")
    except OSError as exc:  # pragma: no cover - non-fatal
        logger.warning("Could not write readiness file %s: %s", READY_FILE, exc)


def _bus_id(mrid: str) -> int | None:
    match = _BUS_RE.search(mrid or "")
    return int(match.group(1)) if match else None


class DRControllerService:
    """Command handlers for the DR controller container."""

    def __init__(self, settings=default_settings):
        self.settings = settings
        self._sessions: dict[str, dict] = {}

    def register(self, participant: BusParticipant) -> None:
        participant.on_command("configure", self.configure)
        participant.on_command("control", self.control)
        participant.on_command("strategies", self.strategies)
        participant.on_command("control-devices", self.control_devices)
        participant.on_command("stop", self.stop)

    def configure(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        mode = str(payload.get("mode") or "dr_only")
        topic_prefix = str(payload.get("topic_prefix") or self.settings.BUS_PREFIX)
        controller = self._build_controller(mode, topic_prefix)
        self._sessions[session_id] = {
            "mode": mode,
            "topic_prefix": topic_prefix,
            "controller": controller,
        }
        return {
            "session_id": session_id,
            "mode": mode,
            "controller_mode": controller.mode,
            "max_iterations": controller.max_iterations,
            "v_upper": controller.v_upper,
            "v_lower": controller.v_lower,
        }

    def strategies(self, payload: dict) -> dict:
        return {"strategies": strategy_registry.available()}

    def control_devices(self, payload: dict) -> dict:
        """List the installed control-device plugins (in execution order)."""
        return {"control_devices": control_plugin_names()}

    def stop(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        existed = session_id in self._sessions
        self._sessions.pop(session_id, None)
        return {"session_id": session_id, "stopped": existed}

    def control(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "default")
        session = self._sessions.get(session_id)
        if session is None:
            configured = self.configure(payload)
            session = self._sessions[str(configured["session_id"])]

        controller = session["controller"]
        topic_prefix = str(payload.get("topic_prefix") or session["topic_prefix"])
        controls = []

        for status in payload.get("statuses", []):
            status_payload = status.get("payload") if isinstance(status, dict) else None
            status_payload = status_payload or status or {}
            readings = status_payload.get("readings") or {}
            bus_id = status.get("bus_id") if isinstance(status, dict) else None
            bus_id = int(bus_id) if bus_id is not None else _bus_id(status_payload.get("mRID", ""))
            if bus_id is None:
                continue

            control = controller.control_for(readings)
            control_payload = build_der_control(
                bus_id,
                status_payload.get("timestamp", ""),
                **control,
            )
            controls.append(
                {
                    "bus_id": bus_id,
                    "topic": build_topic(
                        "DERControlProfile",
                        control_payload["mRID"],
                        topic_prefix,
                    ),
                    "payload": control_payload,
                    "readings": control_payload["readings"],
                }
            )

        return {
            "session_id": session_id,
            "mode": session["mode"],
            "controls": controls,
            "control_count": len(controls),
        }

    def _build_controller(self, mode: str, topic_prefix: str):
        if not strategy_registry.is_registered(mode):
            raise ValueError(
                f"Unknown coordination strategy '{mode}'. "
                f"Available: {[strategy['name'] for strategy in strategy_registry.available()]}"
            )
        return strategy_registry.create(mode, self.settings, topic_prefix)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = DRControllerService(default_settings)
    participant = BusParticipant(
        make_transport(default_settings.BUS_TRANSPORT, default_settings, "dr-controller"),
        service="dr-controller",
        prefix=default_settings.BUS_PREFIX,
    )
    service.register(participant)
    participant.start()
    _mark_ready()  # bus connected + subscribed -> container is healthy
    logger.info("DR controller service ready on %s", default_settings.BUS_PREFIX)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Stopping DR controller service")
    finally:
        participant.stop()


if __name__ == "__main__":
    main()
