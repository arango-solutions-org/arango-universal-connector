"""An in-memory DeltaTableClient for adapter tests."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from arango_connector.sources.databricks.client import (
    ChangeFeedUnavailable,
    ChangeRow,
    DeltaTableNotFound,
    TableInfo,
)
from arango_connector.sources.databricks.config import TableSpec

EPOCH = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


@dataclass
class FakeTable:
    columns: tuple[str, ...]
    key: tuple[str, ...]
    cdf: bool = True
    # The rows as of each version, and every change row ever committed.
    versions: list[list[dict[str, Any]]] = field(default_factory=list)
    changes: list[ChangeRow] = field(default_factory=list)

    def _commit(self, rows: list[dict[str, Any]], changes: list[tuple[str, dict[str, Any]]]) -> int:
        version = len(self.versions)
        self.versions.append(copy.deepcopy(rows))
        self.changes += [
            ChangeRow(copy.deepcopy(row), kind, version, EPOCH + timedelta(minutes=version))
            for kind, row in changes
        ]
        return version


class FakeDeltaClient:
    """Tables keyed by name. Each mutation commits one version with its change rows."""

    def __init__(self, batch_size: int = 2) -> None:
        self.batch_size = batch_size
        self.tables: dict[str, FakeTable] = {}
        # Tables whose change feed reports the range unreadable (e.g. vacuumed history).
        self.feed_unavailable: set[str] = set()
        self.closed = False

    # -- test setup ------------------------------------------------------------------

    def create(self, spec: TableSpec, columns: tuple[str, ...], *, cdf: bool = True) -> None:
        self.tables[spec.name] = FakeTable(columns, spec.primary_key, cdf)
        self.tables[spec.name]._commit([], [])

    def insert(self, name: str, *rows: dict[str, Any]) -> int:
        table = self.tables[name]
        return table._commit(table.versions[-1] + list(rows), [("insert", r) for r in rows])

    def update(self, name: str, match: dict[str, Any], **values: Any) -> int:
        table = self.tables[name]
        current, changes = [], []
        for row in table.versions[-1]:
            if all(row.get(k) == v for k, v in match.items()):
                new = {**row, **values}
                changes += [("update_preimage", row), ("update_postimage", new)]
                row = new
            current.append(row)
        return table._commit(current, changes)

    def delete(self, name: str, match: dict[str, Any]) -> int:
        table = self.tables[name]
        hit = [r for r in table.versions[-1] if all(r.get(k) == v for k, v in match.items())]
        kept = [r for r in table.versions[-1] if r not in hit]
        return table._commit(kept, [("delete", r) for r in hit])

    # -- DeltaTableClient ------------------------------------------------------------

    def _table(self, spec: TableSpec) -> FakeTable:
        if spec.name not in self.tables:
            raise DeltaTableNotFound(spec.name)
        return self.tables[spec.name]

    def describe(self, spec: TableSpec) -> TableInfo:
        table = self._table(spec)
        version = len(table.versions) - 1
        return TableInfo(version, EPOCH + timedelta(minutes=version), table.columns, table.cdf)

    def read_snapshot(self, spec: TableSpec, version: int) -> Iterator[list[dict[str, Any]]]:
        rows = copy.deepcopy(self._table(spec).versions[version])
        for start in range(0, len(rows), self.batch_size):
            yield rows[start : start + self.batch_size]

    def read_changes(self, spec: TableSpec, start: int, end: int) -> Iterator[list[ChangeRow]]:
        table = self._table(spec)
        if not table.cdf or spec.name in self.feed_unavailable or end >= len(table.versions):
            raise ChangeFeedUnavailable(spec.name)
        rows = [c for c in table.changes if start <= c.commit_version <= end]
        for i in range(0, len(rows), self.batch_size):
            yield rows[i : i + self.batch_size]

    def close(self) -> None:
        self.closed = True
