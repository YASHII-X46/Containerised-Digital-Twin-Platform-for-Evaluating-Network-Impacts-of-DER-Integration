"""OpenDSS solver NATS service entry point."""

import logging
import os
import tempfile
import time

from dss_solver.bus import BusParticipant, make_transport
from dss_solver.config import settings
from dss_solver.service import OpenDSSSolverService

logger = logging.getLogger(__name__)

# Readiness marker for the container healthcheck: written once the bus
# participant is connected and subscribed (see main()). Overridable so the
# Windows images can probe a known path (C:\ready\service.ready).
READY_FILE = os.environ.get(
    "READY_FILE", os.path.join(tempfile.gettempdir(), "service.ready")
)


def _mark_ready() -> None:
    try:
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            fh.write("ready")
    except OSError as exc:  # pragma: no cover - non-fatal
        logger.warning("Could not write readiness file %s: %s", READY_FILE, exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = OpenDSSSolverService(settings)
    participant = BusParticipant(
        make_transport(settings.BUS_TRANSPORT, settings, "opendss-solver"),
        service="opendss-solver",
        prefix=settings.BUS_PREFIX,
    )
    service.register(participant)
    participant.start()
    _mark_ready()  # bus connected + subscribed -> container is healthy
    logger.info("OpenDSS solver service ready on %s", settings.BUS_PREFIX)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Stopping OpenDSS solver service")
    finally:
        participant.stop()


if __name__ == "__main__":
    main()
