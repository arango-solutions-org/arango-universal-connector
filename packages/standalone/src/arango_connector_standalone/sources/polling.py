"""Base for sources that poll an API on an interval rather than receiving pushes."""

from __future__ import annotations

import logging
import threading
import time
from abc import abstractmethod

from arango_connector_standalone.sources.base import EmitFn, SourceAdapter

logger = logging.getLogger(__name__)

# Waits are sliced so signal handlers on the main thread get a chance to run.
_WAIT_SLICE_SECONDS = 0.5


class PollingSourceAdapter(SourceAdapter):
    """Calls :meth:`poll_once` every ``interval_seconds`` until :meth:`stop`.

    A poll that raises is logged and retried with exponential backoff (capped at
    ``max_backoff_seconds``), so one bad poll never stops the source. Subclasses should
    check :attr:`stopping` inside long polls to shut down promptly.
    """

    def __init__(self, interval_seconds: float, max_backoff_seconds: float = 300.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.max_backoff_seconds = max(max_backoff_seconds, interval_seconds)
        self._stopped = threading.Event()
        self._consecutive_failures = 0

    @property
    def stopping(self) -> bool:
        return self._stopped.is_set()

    @abstractmethod
    def poll_once(self, emit: EmitFn) -> None:
        """Fetch whatever is new since the last poll and ``emit`` each Envelope."""

    def start(self, emit: EmitFn) -> None:
        """Poll until :meth:`stop` is called. Never raises because of a failed poll."""
        logger.info(
            "polling_source_starting source=%s interval_seconds=%s",
            self.name,
            self.interval_seconds,
        )
        while not self.stopping:
            try:
                self.poll_once(emit)
                self._consecutive_failures = 0
            except Exception:
                self._consecutive_failures += 1
                logger.exception(
                    "poll_failed source=%s consecutive_failures=%s",
                    self.name,
                    self._consecutive_failures,
                )
            self._wait(self._next_delay())
        logger.info("polling_source_stopped source=%s", self.name)

    def _next_delay(self) -> float:
        if self._consecutive_failures == 0:
            return self.interval_seconds
        backoff = self.interval_seconds * 2 ** (self._consecutive_failures - 1)
        return float(min(backoff, self.max_backoff_seconds))

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stopped.wait(timeout=min(remaining, _WAIT_SLICE_SECONDS))

    def stop(self) -> None:
        """Unblock :meth:`start`. Safe to call more than once."""
        self._stopped.set()
