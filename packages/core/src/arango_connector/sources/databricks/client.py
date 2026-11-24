"""The contract between the Databricks adapter and whatever reads the Delta tables.

``LocalDeltaClient`` (delta-rs on a local directory) implements it today.
TODO(future): ``DatabricksSqlClient`` on databricks-sql-connector, against the same
contract tests (tests/test_databricks_client_contract.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from arango_connector.sources.databricks.config import TableSpec


@dataclass(frozen=True)
class TableInfo:
    # Latest committed version.
    version: int
    # Commit time of that version, timezone-aware UTC.
    timestamp: datetime
    columns: tuple[str, ...]
    change_feed_enabled: bool


@dataclass(frozen=True)
class ChangeRow:
    # Data columns only, no change-feed metadata columns.
    row: dict[str, Any]
    # insert | update_preimage | update_postimage | delete
    change_type: str
    commit_version: int
    # Timezone-aware UTC.
    commit_timestamp: datetime


class DeltaClientError(Exception):
    """Base for every error a ``DeltaTableClient`` raises."""


class DeltaTableNotFound(DeltaClientError):
    """The table does not exist (or is not visible)."""


class ChangeFeedUnavailable(DeltaClientError):
    """The change feed cannot serve the range: not enabled, no longer readable, or outside
    the table's history. The adapter falls back to a snapshot."""


class DeltaTableClient(Protocol):
    """The table operations the adapter needs. Row values are plain Python values
    (``int``, ``str``, ``Decimal``, ``datetime``, ``date``, ``bytes``, ``list``, ``dict``,
    ``None``); no backend objects cross this boundary."""

    def describe(self, table: TableSpec) -> TableInfo: ...

    def read_snapshot(self, table: TableSpec, version: int) -> Iterator[list[dict[str, Any]]]:
        """Yield batches of every row as of ``version``."""
        ...

    def read_changes(
        self, table: TableSpec, start_version: int, end_version: int
    ) -> Iterator[list[ChangeRow]]:
        """Yield batches of change rows for ``start_version..end_version`` inclusive, in
        ascending ``commit_version``."""
        ...

    def close(self) -> None: ...
