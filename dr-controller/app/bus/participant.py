"""OpenFMB command/event bus participant."""

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CommandHandler = Callable[[dict], dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def envelope(payload: dict, correlation_id: str | None = None, status: str = "ok") -> dict:
    return {
        "messageId": str(uuid.uuid4()),
        "correlationId": correlation_id or str(uuid.uuid4()),
        "timestamp": _now(),
        "status": status,
        "payload": payload,
    }


class BusParticipant:
    """A service that handles commands and publishes events on the bus."""

    def __init__(self, transport, service: str, prefix: str = "openfmb"):
        self._t = transport
        self.service = service
        self.prefix = prefix
        self._handlers: dict[str, CommandHandler] = {}

    def on_command(self, action: str, handler: CommandHandler) -> None:
        self._handlers[action] = handler

    def start(self) -> None:
        self._t.connect()
        self._t.subscribe(f"{self.prefix}/command/{self.service}/+", self._on_command)
        logger.info("Bus participant '%s' listening for commands", self.service)

    def stop(self) -> None:
        self._t.disconnect()

    def publish_event(
        self,
        action: str,
        payload: dict,
        correlation_id: str | None = None,
        status: str = "ok",
    ) -> None:
        self._t.publish(
            f"{self.prefix}/event/{self.service}/{action}",
            envelope(payload, correlation_id, status),
        )

    def _on_command(self, topic: str, message: dict) -> None:
        action = topic.rsplit("/", 1)[-1]
        handler = self._handlers.get(action)
        if handler is None:
            return
        corr = message.get("correlationId")
        try:
            result = handler(message.get("payload") or {})
            self.publish_event(action, result, corr, "ok")
        except Exception as exc:
            logger.exception("Command %s/%s failed", self.service, action)
            self.publish_event(action, {"error": str(exc)}, corr, "error")

    def request(
        self,
        target_service: str,
        action: str,
        payload: dict,
        timeout: float = 60.0,
    ) -> dict | None:
        corr = str(uuid.uuid4())
        result: dict = {}
        done = threading.Event()

        def waiter(_topic, msg):
            if msg.get("correlationId") == corr:
                result["msg"] = msg
                done.set()

        self._t.subscribe(f"{self.prefix}/event/{target_service}/{action}", waiter)
        self._t.publish(
            f"{self.prefix}/command/{target_service}/{action}",
            envelope(payload, corr),
        )
        done.wait(timeout)
        return result.get("msg")
