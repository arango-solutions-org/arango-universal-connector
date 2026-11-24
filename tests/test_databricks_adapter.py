"""Adapter tests: snapshot then change-feed sync against an in-memory client and store."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from arango_connector.envelope import Envelope
from arango_connector.sources.databricks.config import parse_table_spec
from arango_connector_standalone.config import CoreSettings
from arango_connector_standalone.registry import build_source
from arango_connector_standalone.sources.databricks.adapter import (
    STATE_NAMESPACE,
    DatabricksPollingAdapter,
)
from arango_connector_standalone.state import InMemoryStateStore
from databricks_fakes import FakeDeltaClient

ORDERS = parse_table_spec("main.retail.orders:order_id")
CUSTOMERS = parse_table_spec("main.retail.customers:customer_id")
ORDER_COLUMNS = ("order_id", "customer_id", "status")


def order(order_id: int, customer_id: int | None = 1, status: str = "placed") -> dict[str, Any]:
    return {"order_id": order_id, "customer_id": customer_id, "status": status}


def make(
    client: FakeDeltaClient, tables: tuple[Any, ...] = (ORDERS,)
) -> tuple[DatabricksPollingAdapter, InMemoryStateStore]:
    state = InMemoryStateStore()
    adapter = DatabricksPollingAdapter(list(tables), lambda: client, state, workspace="local")
    return adapter, state


def poll(adapter: DatabricksPollingAdapter) -> list[Envelope[Any]]:
    emitted: list[Envelope[Any]] = []
    adapter.poll_once(emitted.append)
    return emitted


def cursor(state: InMemoryStateStore, table: str = "main.retail.orders") -> int | None:
    value = state.get(STATE_NAMESPACE, f"local:{table}")
    return None if value is None else int(value["version"])


@pytest.fixture
def client() -> FakeDeltaClient:
    fake = FakeDeltaClient(batch_size=2)
    fake.create(ORDERS, ORDER_COLUMNS)
    fake.insert(ORDERS.name, order(1), order(2), order(3))
    return fake


def test_first_poll_snapshots_then_idles(client: FakeDeltaClient) -> None:
    adapter, state = make(client)
    emitted = poll(adapter)
    assert sorted(e.source_id for e in emitted) == ["1", "2", "3"]
    assert {e.raw["_change_type"] for e in emitted} == {"snapshot"}
    assert cursor(state) == 1
    assert state.get(STATE_NAMESPACE, "local:main.retail.orders")["last_synced_at"]  # type: ignore[index]

    assert poll(adapter) == []
    assert cursor(state) == 1


def test_changes_emit_inserts_and_postimages_in_order(
    client: FakeDeltaClient, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, state = make(client)
    poll(adapter)
    client.update(ORDERS.name, {"order_id": 2}, status="shipped")
    client.insert(ORDERS.name, order(4))
    client.delete(ORDERS.name, {"order_id": 1})
    client.update(ORDERS.name, {"order_id": 4}, customer_id=2)

    with caplog.at_level(logging.INFO):
        emitted = poll(adapter)
    assert [(e.source_id, e.raw["_change_type"], e.raw["_commit_version"]) for e in emitted] == [
        ("2", "update_postimage", 2),
        ("4", "insert", 3),
        ("4", "update_postimage", 5),
    ]
    assert emitted[0].payload.columns["status"] == "shipped"
    assert cursor(state) == 5
    assert "databricks_deletes_skipped table=main.retail.orders count=1" in caplog.text
    assert "mode=changes rows=3 skipped=0 deletes_skipped=1 version=5" in caplog.text


def test_unavailable_feed_falls_back_to_snapshot(
    client: FakeDeltaClient, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, state = make(client)
    poll(adapter)
    client.insert(ORDERS.name, order(4))
    client.feed_unavailable.add(ORDERS.name)
    emitted = poll(adapter)
    assert sorted(e.source_id for e in emitted) == ["1", "2", "3", "4"]
    assert {e.raw["_change_type"] for e in emitted} == {"snapshot"}
    assert cursor(state) == 2
    assert "databricks_change_feed_unavailable" in caplog.text


def test_cursor_ahead_of_table_falls_back_to_snapshot(
    client: FakeDeltaClient, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, state = make(client)
    state.put(STATE_NAMESPACE, "local:main.retail.orders", {"version": 9})
    emitted = poll(adapter)
    assert len(emitted) == 3
    assert cursor(state) == 1
    assert "databricks_change_feed_unavailable" in caplog.text


def test_unusable_tables_fail_alone_and_together(
    client: FakeDeltaClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.create(CUSTOMERS, ("customer_id", "name"), cdf=False)
    no_key = parse_table_spec("main.retail.orders_copy:missing_id")
    client.create(no_key, ORDER_COLUMNS)
    adapter, state = make(client, (CUSTOMERS, ORDERS, no_key))
    assert len(poll(adapter)) == 3
    assert cursor(state) == 1
    assert cursor(state, CUSTOMERS.name) is None
    assert "databricks_table_unusable table=main.retail.customers" in caplog.text
    assert "primary key column(s) not in table: missing_id" in caplog.text

    broken, _ = make(client, (CUSTOMERS, no_key))
    with pytest.raises(RuntimeError, match="all 2 Databricks table"):
        poll(broken)


def test_missing_table_fails_only_that_table(client: FakeDeltaClient) -> None:
    adapter, state = make(client, (parse_table_spec("main.retail.nope:id"), ORDERS))
    assert len(poll(adapter)) == 3
    assert cursor(state) == 1


def test_null_keys_are_skipped(client: FakeDeltaClient, caplog: pytest.LogCaptureFixture) -> None:
    client.insert(ORDERS.name, order(None))  # type: ignore[arg-type]
    adapter, _ = make(client)
    with caplog.at_level(logging.INFO):
        assert len(poll(adapter)) == 3
    assert "mode=snapshot rows=3 skipped=1" in caplog.text


def test_stopping_mid_table_keeps_the_cursor(client: FakeDeltaClient) -> None:
    adapter, state = make(client)
    poll(adapter)
    client.insert(ORDERS.name, order(4), order(5), order(6))  # two batches of changes

    emitted: list[Envelope[Any]] = []

    def emit(envelope: Envelope[Any]) -> None:
        emitted.append(envelope)
        adapter.stop()

    adapter.poll_once(emit)
    assert [e.source_id for e in emitted] == ["4", "5"]  # the rest of the first batch
    assert cursor(state) == 1


def test_start_closes_the_client(client: FakeDeltaClient) -> None:
    adapter, _ = make(client)
    original = adapter.poll_once

    def once(emit: Any) -> None:
        original(emit)
        adapter.stop()

    adapter.poll_once = once  # type: ignore[method-assign]
    adapter.start(lambda _e: None)
    assert client.closed


def test_registry_builds_the_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_TABLES", "main.retail.orders:order_id")
    monkeypatch.setenv("DATABRICKS_DELTA_ROOT", str(tmp_path))
    settings = CoreSettings(source="databricks", arango_password="x")  # type: ignore[arg-type]
    adapter = build_source(settings, InMemoryStateStore())
    assert isinstance(adapter, DatabricksPollingAdapter)
    assert adapter.name == "databricks"
