"""``DeltaTableClient`` on local Delta Lake tables via delta-rs.

The only module allowed to import ``deltalake`` or ``pyarrow`` (a test enforces this).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from deltalake import DeltaTable
from deltalake.exceptions import DeltaError, TableNotFoundError

from arango_connector.sources.databricks.client import (
    ChangeFeedUnavailable,
    ChangeRow,
    DeltaClientError,
    DeltaTableNotFound,
    TableInfo,
)
from arango_connector.sources.databricks.config import TableSpec

_CDF_FLAG = "delta.enableChangeDataFeed"
_META_COLUMNS = ("_change_type", "_commit_version", "_commit_timestamp")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LocalDeltaClient:
    """Reads ``catalog.schema.table`` from ``{root}/{catalog}/{schema}/{table}``."""

    def __init__(self, root: Path | str, *, batch_size: int = 1000) -> None:
        self.root = Path(root)
        self.batch_size = batch_size

    def _path(self, table: TableSpec) -> str:
        return str(self.root / table.catalog / table.schema / table.table)

    def _open(self, table: TableSpec, version: int | None = None) -> DeltaTable:
        try:
            return DeltaTable(self._path(table), version=version)
        except TableNotFoundError as exc:
            raise DeltaTableNotFound(table.name) from exc
        except DeltaError as exc:
            raise DeltaClientError(f"{table.name}: {exc}") from exc

    def describe(self, table: TableSpec) -> TableInfo:
        dt = self._open(table)
        latest = next(iter(dt.history(1)))
        configuration = dt.metadata().configuration or {}
        return TableInfo(
            version=dt.version(),
            timestamp=datetime.fromtimestamp(latest["timestamp"] / 1000, tz=UTC),
            columns=tuple(field.name for field in dt.schema().fields),
            change_feed_enabled=str(configuration.get(_CDF_FLAG, "")).lower() == "true",
        )

    def read_snapshot(self, table: TableSpec, version: int) -> Iterator[list[dict[str, Any]]]:
        dataset = self._open(table, version).to_pyarrow_dataset()
        for batch in dataset.to_batches(batch_size=self.batch_size):
            if batch.num_rows:
                yield batch.to_pylist()

    def read_changes(
        self, table: TableSpec, start_version: int, end_version: int
    ) -> Iterator[list[ChangeRow]]:
        dt = self._open(table)
        try:
            # shortcut: the whole range is materialized and sorted in memory; stream
            # per-version reads if ranges get large. TODO(future)
            changes = pa.table(
                dt.load_cdf(starting_version=start_version, ending_version=end_version)
            )
        except DeltaError as exc:
            # delta-rs reports a disabled feed and an out-of-history range the same way.
            raise ChangeFeedUnavailable(f"{table.name}: {exc}") from exc
        # Sorted as Python rows: Arrow's sort has no kernel for delta-rs's string_view columns.
        rows = sorted(changes.to_pylist(), key=lambda r: r["_commit_version"])
        data_columns = [c for c in changes.column_names if c not in _META_COLUMNS]
        for start in range(0, len(rows), self.batch_size):
            yield [
                ChangeRow(
                    row={c: r[c] for c in data_columns},
                    change_type=str(r["_change_type"]),
                    commit_version=int(r["_commit_version"]),
                    commit_timestamp=_utc(r["_commit_timestamp"]),
                )
                for r in rows[start : start + self.batch_size]
            ]

    def close(self) -> None:
        """Nothing to release: every call opens the table afresh."""
