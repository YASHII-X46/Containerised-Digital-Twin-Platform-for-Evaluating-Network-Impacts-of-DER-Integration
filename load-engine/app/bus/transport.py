"""Message-bus transports for inter-container OpenFMB messaging.

All transports share one interface so the same participant code runs over any of
them — selected by ``BUS_TRANSPORT`` (default ``nats``):

  - NatsTransport: OpenFMB transport over NATS (the primary message bus).
  - LoopbackTransport: in-process pub/sub — publishing invokes matching
    subscribers inline, deterministically (used in tests; no broker).

Topics use ``/`` separators and ``+``/``#`` wildcard filters; the NATS
transport translates these to ``.``-separated subjects with ``*``/``>`` wildcards.
"""

import json
import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

OnMessage = Callable[[str, dict], None]


def topic_matches(filt: str, topic: str) -> bool:
    """Topic-filter match supporting '+' (one level) and trailing '#'."""
    f, t = filt.split("/"), topic.split("/")
    for i, seg in enumerate(f):
        if seg == "#":
            return True
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(f) == len(t)


class LoopbackTransport:
    """In-process synchronous transport (deterministic; used in tests)."""

    def __init__(self):
        self._subs: list[tuple[str, OnMessage]] = []
        self.published: list[tuple[str, dict]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, topic_filter: str, on_message: OnMessage) -> None:
        self._subs.append((topic_filter, on_message))

    def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))
        for topic_filter, cb in list(self._subs):
            if topic_matches(topic_filter, topic):
                cb(topic, payload)



def _topic_to_subject(topic: str) -> str:
    """openfmb/command/load-engine/generate -> openfmb.command.load-engine.generate"""
    return topic.replace("/", ".")


def _filter_to_subject(topic_filter: str) -> str:
    """Translate a slash topic filter to a NATS subject filter (+ -> *, # -> >)."""
    tokens = []
    for tok in topic_filter.split("/"):
        tokens.append(">" if tok == "#" else "*" if tok == "+" else tok)
    return ".".join(tokens)


class NatsTransport:
    """OpenFMB transport over NATS — async nats-py bridged to the sync interface.

    A background asyncio loop owns the NATS connection; publish/subscribe are
    scheduled onto it thread-safely. Incoming messages dispatch the (sync)
    handler in a thread pool so a slow command handler never blocks the NATS loop.
    """

    def __init__(self, servers: str, client_id: str):
        self._servers = servers
        self._name = client_id
        self._subs: list[tuple[str, OnMessage]] = []
        self._loop = None
        self._thread = None
        self._nc = None

    def connect(self) -> None:
        """Connect with retries; each attempt fully owns a private loop/thread.

        An attempt is adopted only after its connection succeeds. If an attempt
        outlives its wait window and succeeds late, it finds it was not adopted
        and closes itself — it never touches shared state. (The old retry loop
        reassigned self._loop while earlier attempt threads were still running,
        so two threads could race run_forever() on one loop, wedging the bus
        while the service still reported healthy.)
        """
        import asyncio

        deadline = time.monotonic() + 30
        last_error = None
        while time.monotonic() < deadline:
            loop = asyncio.new_event_loop()
            outcome = {"nc": None, "error": None}
            finished = threading.Event()   # attempt has a result (nc or error)
            decided = threading.Event()    # main thread has ruled on adoption
            keep = threading.Event()       # ruling: keep this attempt running

            def _attempt(loop=loop, outcome=outcome, finished=finished,
                         decided=decided, keep=keep):
                import nats

                asyncio.set_event_loop(loop)
                try:
                    outcome["nc"] = loop.run_until_complete(
                        nats.connect(self._servers, name=self._name)
                    )
                except Exception as exc:  # noqa: BLE001 — retried by connect()
                    outcome["error"] = exc
                    finished.set()
                    loop.close()
                    return
                finished.set()
                decided.wait(timeout=10)
                if keep.is_set():
                    loop.run_forever()
                    return
                # Not adopted (main thread moved on): clean up quietly.
                try:
                    loop.run_until_complete(outcome["nc"].close())
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
                finally:
                    loop.close()

            thread = threading.Thread(target=_attempt, daemon=True)
            thread.start()
            if finished.wait(timeout=5) and outcome["nc"] is not None:
                self._loop = loop
                self._thread = thread
                self._nc = outcome["nc"]
                keep.set()
                decided.set()
                return
            # Failed, or still in flight past the window: rule against adoption
            # (a late success closes itself) and retry on a fresh loop.
            decided.set()
            if outcome["error"] is not None:
                last_error = outcome["error"]
                logger.warning("NATS connect failed: %s", outcome["error"])
            time.sleep(1)
        raise ConnectionError(
            f"NATS bus could not connect to {self._servers}"
            + (f" (last error: {last_error})" if last_error else "")
        )

    def subscribe(self, topic_filter: str, on_message: OnMessage) -> None:
        import asyncio

        self._subs.append((topic_filter, on_message))
        subject = _filter_to_subject(topic_filter)

        async def _do_subscribe():
            async def handler(msg):
                try:
                    payload = json.loads(msg.data.decode())
                except Exception:
                    return
                topic = msg.subject.replace(".", "/")
                self._loop.run_in_executor(None, on_message, topic, payload)
            await self._nc.subscribe(subject, cb=handler)

        future = asyncio.run_coroutine_threadsafe(_do_subscribe(), self._loop)
        future.result(timeout=5)

    def publish(self, topic: str, payload: dict) -> None:
        import asyncio

        data = json.dumps(payload).encode()
        future = asyncio.run_coroutine_threadsafe(
            self._nc.publish(_topic_to_subject(topic), data), self._loop
        )
        future.result(timeout=5)

    def disconnect(self) -> None:
        import asyncio

        if self._nc is not None:
            asyncio.run_coroutine_threadsafe(self._nc.drain(), self._loop)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


def make_transport(kind: str, settings, client_id: str):
    """Build a bus transport by name: 'nats' (default) or 'loopback'."""
    kind = (kind or "nats").lower()
    if kind == "nats":
        return NatsTransport(settings.NATS_URL, client_id)
    if kind == "loopback":
        return LoopbackTransport()
    raise ValueError(f"Unknown BUS_TRANSPORT '{kind}' (use 'nats' or 'loopback').")
