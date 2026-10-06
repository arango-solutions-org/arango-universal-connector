"""Wires a SourceAdapter to a Sink, with retention stamping and dead-lettering."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arango_connector.record import Record
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import SourceAdapter
from arango_connector.state.base import StateStore

logger = logging.getLogger(__name__)

# How often the dead-letter file is re-checked while the connector runs. The sink's
# TTL index is swept by ArangoDB itself; the file has no equivalent, so we sweep it.
PRUNE_INTERVAL_SECONDS = 3600


def _retention_base(line: str) -> datetime | None:
    """Read the time retention counts from (``created_at``, else ``ingested_at``) from one
    dead-letter line, or ``None`` if unreadable."""
    try:
        doc = json.loads(line)
        parsed = datetime.fromisoformat(doc.get("created_at") or doc["ingested_at"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class Pipeline:
    """Forwards every Record emitted by a source to a sink.

    Stamps each Record's ``expires_at`` from ``retention_days`` before writing, and applies
    the same window to the dead-letter file, which the sink's TTL index cannot reach.
    Sets up the source's ``state`` store, if any, before the source starts.

    TODO(future): put a queue between source and sink and batch writes; replace the
    dead-letter file with a dead-letter collection, which would let the TTL index cover
    it and make this pruning unnecessary.
    """

    def __init__(
        self,
        source: SourceAdapter,
        sink: Sink,
        dead_letter_path: Path | str = "./dead_letter.jsonl",
        retention_days: int = 0,
        prune_interval_seconds: float = PRUNE_INTERVAL_SECONDS,
        state: StateStore | None = None,
    ) -> None:
        self.source = source
        self.sink = sink
        self.state = state
        self.dead_letter_path = Path(dead_letter_path)
        self.retention_days = retention_days
        self._prune_interval = prune_interval_seconds
        self._dead_letter_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        self._prune_stopped = threading.Event()
        self._prune_thread: threading.Thread | None = None

    def run(self) -> None:
        """Prepare the sink, then start the source. Blocks until the source stops."""
        if self.state is not None:
            self.state.setup()
        self.sink.setup()
        self._start_pruning()
        logger.info(
            "pipeline_starting source=%s sink=%s", self.source.name, type(self.sink).__name__
        )
        self.source.start(emit=self._handle)

    def stamp_expiry(self, record: Record[Any]) -> Record[Any]:
        """Return ``record`` with ``expires_at`` set from the retention window."""
        if self.retention_days <= 0:
            return record.model_copy(update={"expires_at": None})
        expires_at = record.retention_base + timedelta(days=self.retention_days)
        return record.model_copy(update={"expires_at": expires_at})

    def _handle(self, record: Record[Any]) -> None:
        """Write one record; on failure dead-letter it. Never raises."""
        try:
            record = self.stamp_expiry(record)
            self.sink.write([record])
            logger.debug("record_written key=%s", record.key)
        except Exception as exc:
            logger.error(
                "record_write_failed key=%s error_type=%s error=%s dead_letter=%s",
                record.key,
                type(exc).__name__,
                exc,
                self.dead_letter_path,
            )
            self._dead_letter(record)

    def _dead_letter(self, record: Record[Any]) -> None:
        try:
            line = json.dumps(record.to_document(), ensure_ascii=False)
            with self._dead_letter_lock:
                self.dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
                with self.dead_letter_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            # Last line of defence: the source loop must never crash.
            logger.exception("dead_letter_write_failed key=%s", record.key)

    # -- dead-letter retention ---------------------------------------------

    def _start_pruning(self) -> None:
        """Prune once, then keep pruning on an interval for as long as we run."""
        if self.retention_days <= 0:
            return
        self.prune_dead_letter()
        self._prune_thread = threading.Thread(
            target=self._prune_loop, name="dead-letter-prune", daemon=True
        )
        self._prune_thread.start()

    def _prune_loop(self) -> None:
        while not self._prune_stopped.wait(self._prune_interval):
            self.prune_dead_letter()

    def prune_dead_letter(self) -> int:
        """Drop dead-lettered documents older than the retention window.

        Returns the number of entries removed. Retention is measured from a record's
        ``created_at`` (else ``ingested_at``) with the current window, matching how the
        sink derives ``expires_at``, so a dead-lettered record ages out at the same time as
        it would have in ArangoDB. Never raises: losing the
        prune must not take the connector down.
        """
        if self.retention_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        try:
            with self._dead_letter_lock:
                return self._prune_locked(cutoff)
        except Exception:
            logger.exception("dead_letter_prune_failed path=%s", self.dead_letter_path)
            return 0

    def _prune_locked(self, cutoff: datetime) -> int:
        if not self.dead_letter_path.is_file():
            return 0

        kept: list[str] = []
        removed = 0
        unreadable = 0
        with self.dead_letter_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                base = _retention_base(line)
                if base is not None and base < cutoff:
                    removed += 1
                    continue
                # A line we cannot read is kept rather than silently destroyed; a
                # corrupt half-write should surface as a warning, not as data loss.
                unreadable += base is None
                kept.append(line if line.endswith("\n") else line + "\n")

        if unreadable:
            logger.warning(
                "dead_letter_prune_unreadable path=%s lines=%s", self.dead_letter_path, unreadable
            )
        if not removed:
            return 0

        # Rewrite via a temp file so a crash mid-prune cannot truncate the original.
        tmp = self.dead_letter_path.with_name(self.dead_letter_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(kept)
        os.replace(tmp, self.dead_letter_path)

        logger.info(
            "dead_letter_pruned path=%s removed=%s kept=%s retention_days=%s",
            self.dead_letter_path,
            removed,
            len(kept),
            self.retention_days,
        )
        return removed

    def shutdown(self) -> None:
        """Stop pruning and the source, then close the sink and state store.

        Safe to call more than once.
        """
        with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
        logger.info("pipeline_shutting_down")
        self._prune_stopped.set()
        if self._prune_thread is not None:
            self._prune_thread.join(timeout=5)
        try:
            self.source.stop()
        except Exception:
            logger.exception("source_stop_failed")
        try:
            self.sink.close()
        except Exception:
            logger.exception("sink_close_failed")
        if self.state is not None:
            try:
                self.state.close()
            except Exception:
                logger.exception("state_close_failed")
