"""PSS SINCAL solver adapter entry point (NATS service)."""

import logging
import os
import tempfile
import time

from sincal_solver.bus import BusParticipant, make_transport
from sincal_solver.config import settings
from sincal_solver.service import SincalSolverService

logger = logging.getLogger(__name__)

# Overridable so the Windows container healthcheck can probe a known path.
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
    service = SincalSolverService(settings)
    participant = BusParticipant(
        make_transport(settings.BUS_TRANSPORT, settings, "sincal-solver"),
        service="sincal-solver",
        prefix=settings.BUS_PREFIX,
    )
    service.register(participant)
    participant.start()
    _mark_ready()
    probe = service.health({})
    logger.info(
        "SINCAL solver adapter ready on %s (sincal_available=%s: %s)",
        settings.BUS_PREFIX, probe["sincal_available"], probe["detail"],
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Stopping SINCAL solver adapter")
    finally:
        participant.stop()


if __name__ == "__main__":
    main()
