"""The DeltaTableClient contract, as a suite any backend can run.

Each backend supplies a ``Backend`` in the ``backend`` fixture: a client factory over a
table history it has prepared (see ``_local_backend``). TODO(future): a Databricks SQL
backend, gated on credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from arango_connector.sources.databricks.client import (
    ChangeFeedUnavailable,
    DeltaTableClient,
    DeltaTableNotFound,
)
from arango_connector.sources.databricks.config import parse_table_spec
from arango_connector.sources.databricks.local_delta import LocalDeltaClient
from delta_tables import SEED, SPECS, append, delete, update

ORDERS = SPECS["main.retail.orders"]
NO_CDF = SPECS["main.retail.customers"]
MISSING = parse_table_spec("main.retail.missing:id")
PLAIN_TYPES = (type(None), bool, int, float, str, Decimal, datetime, date, bytes, list, dict)


@dataclass
class Backend:
    """``main.retail.orders`` with CDF and versions 0 (seed), 1 (update order 101),
    2 (delete order 102), 3 (insert order 103); ``main.retail.customers`` without CDF."""

    make_client: Callable[[int], DeltaTableClient]


def _local_backend(root: Path) -> Backend:
    append(root, "main.retail.orders", SEED["main.retail.orders"])
    update(root, "main.retail.orders", "order_id = 101", {"status": "'shipped'"})
    delete(root, "main.retail.orders", "order_id = 102")
    append(root, "main.retail.orders", [{"order_id": 103, "customer_id": 2, "status": "placed"}])
    append(root, "main.retail.customers", SEED["main.retail.customers"], cdf=False)
    return Backend(lambda batch_size: LocalDeltaClient(root, batch_size=batch_size))


@pytest.fixture(params=["local"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Backend:
    return _local_backend(tmp_path)


def flat(batches: Any) -> list[Any]:
    return [item for batch in batches for item in batch]


def test_describe(backend: Backend) -> None:
    client = backend.make_client(1000)
    info = client.describe(ORDERS)
    assert info.version == 3
    assert info.timestamp.tzinfo is not None and info.timestamp.utcoffset() == UTC.utcoffset(None)
    assert abs((datetime.now(UTC) - info.timestamp).total_seconds()) < 600
    assert info.columns == ("order_id", "customer_id", "status", "ordered_at")
    assert info.change_feed_enabled is True
    assert client.describe(NO_CDF).change_feed_enabled is False


def test_read_snapshot_at_an_older_version(backend: Backend) -> None:
    client = backend.make_client(1000)
    rows = flat(client.read_snapshot(ORDERS, 0))
    assert sorted(rows, key=lambda r: r["order_id"]) == SEED["main.retail.orders"]
    latest = {r["order_id"]: r for r in flat(client.read_snapshot(ORDERS, 3))}
    assert set(latest) == {100, 101, 103}
    assert latest[101]["status"] == "shipped"
    assert all(isinstance(v, PLAIN_TYPES) for r in rows for v in r.values())


def test_read_changes(backend: Backend) -> None:
    client = backend.make_client(1000)
    changes = flat(client.read_changes(ORDERS, 1, 3))
    assert [c.commit_version for c in changes] == sorted(c.commit_version for c in changes)
    by_type = {(c.change_type, c.row["order_id"], c.commit_version) for c in changes}
    assert by_type == {
        ("update_preimage", 101, 1),
        ("update_postimage", 101, 1),
        ("delete", 102, 2),
        ("insert", 103, 3),
    }
    for change in changes:
        assert set(change.row) == {"order_id", "customer_id", "status", "ordered_at"}
        assert change.commit_timestamp.utcoffset() == UTC.utcoffset(None)
        assert all(isinstance(v, PLAIN_TYPES) for v in change.row.values())
    post = next(c for c in changes if c.change_type == "update_postimage")
    assert post.row["status"] == "shipped"


def test_batches_respect_the_batch_size(backend: Backend) -> None:
    client = backend.make_client(2)
    assert [len(b) for b in client.read_snapshot(ORDERS, 0)] == [2, 1]
    assert [len(b) for b in client.read_changes(ORDERS, 0, 3)] == [2, 2, 2, 1]


def test_table_without_change_feed(backend: Backend) -> None:
    with pytest.raises(ChangeFeedUnavailable):
        flat(backend.make_client(1000).read_changes(NO_CDF, 0, 0))


def test_missing_table(backend: Backend) -> None:
    client = backend.make_client(1000)
    with pytest.raises(DeltaTableNotFound):
        client.describe(MISSING)
    with pytest.raises(DeltaTableNotFound):
        flat(client.read_snapshot(MISSING, 0))


@pytest.mark.parametrize(("start", "end"), [(4, 4), (10, 12)])
def test_range_outside_history(backend: Backend, start: int, end: int) -> None:
    with pytest.raises(ChangeFeedUnavailable):
        flat(backend.make_client(1000).read_changes(ORDERS, start, end))
