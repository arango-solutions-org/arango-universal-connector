"""Wires a SourceAdapter to a Sink, with dead-lettering on write failure."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from arango_connector.envelope import Envelope
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import SourceAdapter

logger = logging.getLogger(__name__)


class Pipeline:
    """Forwards every Envelope emitted by a source to a sink.

    TODO(future): put a queue between source and sink and batch writes; replace the
    dead-letter file with a dead-letter collection.
    """

    def __init__(
        self,
        source: SourceAdapter,
        sink: Sink,
        dead_letter_path: Path | str = "./dead_letter.jsonl",
    ) -> None:
        self.source = source
        self.sink = sink
        self.dead_letter_path = Path(dead_letter_path)
        self._dead_letter_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shut_down = False

    def run(self) -> None:
        """Prepare the sink, then start the source. Blocks until the source stops."""
        self.sink.setup()
        logger.info(
            "pipeline_starting source=%s sink=%s", self.source.name, type(self.sink).__name__
        )
        self.source.start(emit=self._handle)

    def _handle(self, envelope: Envelope) -> None:
        """Write one envelope; on failure dead-letter it. Never raises."""
        try:
            self.sink.write([envelope])
            logger.debug("envelope_written key=%s", envelope.key)
        except Exception as exc:
            logger.error(
                "envelope_write_failed key=%s error_type=%s error=%s dead_letter=%s",
                envelope.key,
                type(exc).__name__,
                exc,
                self.dead_letter_path,
            )
            self._dead_letter(envelope)

    def _dead_letter(self, envelope: Envelope) -> None:
        try:
            line = json.dumps(envelope.to_document(), ensure_ascii=False)
            with self._dead_letter_lock:
                self.dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
                with self.dead_letter_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            # Last line of defence: the source loop must never crash.
            logger.exception("dead_letter_write_failed key=%s", envelope.key)

    def shutdown(self) -> None:
        """Stop the source, then close the sink. Safe to call more than once."""
        with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
        logger.info("pipeline_shutting_down")
        try:
            self.source.stop()
        except Exception:
            logger.exception("source_stop_failed")
        try:
            self.sink.close()
        except Exception:
            logger.exception("sink_close_failed")
