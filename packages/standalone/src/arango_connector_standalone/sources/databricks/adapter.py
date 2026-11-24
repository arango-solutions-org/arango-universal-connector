"""Databricks polling source: snapshot each table once, then follow its change data feed."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from arango_connector.sources.databricks.client import (
    ChangeFeedUnavailable,
    DeltaTableClient,
    TableInfo,
)
from arango_connector.sources.databricks.config import TableSpec
from arango_connector.sources.databricks.normalize import SNAPSHOT, normalize_row
from arango_connector_standalone.sources.base import EmitFn
from arango_connector_standalone.sources.polling import PollingSourceAdapter
from arango_connector_standalone.state.base import StateStore

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "databricks"
_EMITTED_CHANGES = frozenset({"insert", "update_postimage"})

ClientFactory = Callable[[], DeltaTableClient]


class _Stopped(Exception):
    """The adapter is stopping; the table's cursor is left where it was."""


class _TableUnusable(Exception):
    """The table cannot be synced as configured (no change feed, missing key column)."""


@dataclass
class _Stats:
    rows: int = 0
    skipped: int = 0
    deletes_skipped: int = 0


class DatabricksPollingAdapter(PollingSourceAdapter):
    """Polls each configured Delta table for commits newer than its cursor.

    The first sync of a table emits a snapshot at its latest version; later syncs read
    the change data feed from the cursor to the latest version, emitting inserts and
    update post-images. When the feed cannot serve the range, the table is snapshotted
    again. The cursor (the last version emitted) is saved only after a whole snapshot or
    range, so delivery is at-least-once; deterministic keys make repeats harmless.

    TODO(future): propagate row deletes once Envelope and Sink support them.
    TODO(future): tables without a change feed (watermark polling) and views.
    """

    name = "databricks"

    def __init__(
        self,
        tables: list[TableSpec],
        client_factory: ClientFactory,
        state: StateStore,
        *,
        workspace: str,
        interval_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(interval_seconds)
        self._tables = list(tables)
        self._client_factory = client_factory
        self._client: DeltaTableClient | None = None
        self._state = state
        self._workspace = workspace
        self._clock = clock

    def start(self, emit: EmitFn) -> None:
        try:
            super().start(emit)
        finally:
            if self._client is not None:
                self._client.close()
                self._client = None

    def poll_once(self, emit: EmitFn) -> None:
        """Sync every table in order. One failing table does not stop the others; raises
        only if every table failed."""
        if self._client is None:
            self._client = self._client_factory()
        failures = 0
        for table in self._tables:
            if self.stopping:
                return
            try:
                self._sync(self._client, table, emit)
            except _Stopped:
                logger.info("databricks_sync_interrupted table=%s cursor unchanged", table.name)
                return
            except _TableUnusable as exc:
                failures += 1
                logger.error("databricks_table_unusable table=%s reason=%s", table.name, exc)
            except Exception:
                failures += 1
                logger.exception("databricks_table_sync_failed table=%s", table.name)
        if failures and failures == len(self._tables):
            raise RuntimeError(f"all {failures} Databricks table(s) failed to sync")

    # -- per table -----------------------------------------------------------------

    def _sync(self, client: DeltaTableClient, table: TableSpec, emit: EmitFn) -> None:
        info = client.describe(table)
        if not info.change_feed_enabled:
            raise _TableUnusable("change data feed is not enabled (delta.enableChangeDataFeed)")
        missing = [c for c in table.primary_key if c not in info.columns]
        if missing:
            raise _TableUnusable(f"primary key column(s) not in table: {', '.join(missing)}")

        cursor = self._state.get(STATE_NAMESPACE, self._cursor_key(table))
        version = int(cursor["version"]) if cursor is not None else None
        if version == info.version:
            return

        stats = _Stats()
        mode = "changes"
        if version is not None and version < info.version:
            try:
                self._emit_changes(client, table, version + 1, info.version, emit, stats)
            except ChangeFeedUnavailable as exc:
                logger.warning(
                    "databricks_change_feed_unavailable table=%s from_version=%s error=%s "
                    "falling back to a snapshot",
                    table.name,
                    version + 1,
                    exc,
                )
                mode = "snapshot"
        else:
            if version is not None:  # ahead of the table: it was recreated or restored
                logger.warning(
                    "databricks_change_feed_unavailable table=%s cursor_version=%s "
                    "table_version=%s falling back to a snapshot",
                    table.name,
                    version,
                    info.version,
                )
            mode = "snapshot"
        if mode == "snapshot":
            self._emit_snapshot(client, table, info, emit, stats)

        self._state.put(
            STATE_NAMESPACE,
            self._cursor_key(table),
            {"version": info.version, "last_synced_at": self._clock().isoformat()},
        )
        if stats.deletes_skipped:
            logger.warning(
                "databricks_deletes_skipped table=%s count=%s", table.name, stats.deletes_skipped
            )
        logger.info(
            "databricks_table_synced table=%s mode=%s rows=%s skipped=%s deletes_skipped=%s "
            "version=%s",
            table.name,
            mode,
            stats.rows,
            stats.skipped,
            stats.deletes_skipped,
            info.version,
        )

    def _emit_snapshot(
        self,
        client: DeltaTableClient,
        table: TableSpec,
        info: TableInfo,
        emit: EmitFn,
        stats: _Stats,
    ) -> None:
        for batch in client.read_snapshot(table, info.version):
            if self.stopping:
                raise _Stopped
            for row in batch:
                self._emit(row, table, SNAPSHOT, info.version, info.timestamp, emit, stats)

    def _emit_changes(
        self,
        client: DeltaTableClient,
        table: TableSpec,
        start: int,
        end: int,
        emit: EmitFn,
        stats: _Stats,
    ) -> None:
        for batch in client.read_changes(table, start, end):
            if self.stopping:
                raise _Stopped
            for change in batch:
                if change.change_type == "delete":
                    stats.deletes_skipped += 1
                elif change.change_type in _EMITTED_CHANGES:
                    self._emit(
                        change.row,
                        table,
                        change.change_type,
                        change.commit_version,
                        change.commit_timestamp,
                        emit,
                        stats,
                    )

    def _emit(
        self,
        row: dict[str, object],
        table: TableSpec,
        change_type: str,
        version: int,
        timestamp: datetime,
        emit: EmitFn,
        stats: _Stats,
    ) -> None:
        envelope = normalize_row(row, table, self._workspace, change_type, version, timestamp)
        if envelope is None:
            stats.skipped += 1
            logger.debug("databricks_row_skipped table=%s reason=unusable_primary_key", table.name)
            return
        emit(envelope)
        stats.rows += 1

    def _cursor_key(self, table: TableSpec) -> str:
        return f"{self._workspace}:{table.name}"
