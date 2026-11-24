"""End to end: local Delta tables -> LocalDeltaClient -> adapter -> Pipeline ->
ArangoGraphSink and ArangoStateStore, one poll per fresh pipeline."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arango import ArangoClient

from arango_connector.graph.mapping import load_mapping
from arango_connector.sources.databricks.local_delta import LocalDeltaClient
from arango_connector_standalone.pipeline import Pipeline
from arango_connector_standalone.retention import RetentionPolicy
from arango_connector_standalone.sinks.graph import ArangoGraphSink
from arango_connector_standalone.sources.databricks.adapter import DatabricksPollingAdapter
from arango_connector_standalone.state.arango import ArangoStateStore
from delta_tables import SPECS, append, delete, init, update

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

MAPPING = load_mapping(Path(__file__).resolve().parents[1] / "examples/graph/retail.yaml")
RETENTION = RetentionPolicy(0)
COLLECTIONS = ("Customer", "Product", "Order", "PLACED_BY", "CONTAINS")


class Harness:
    """Runs one poll per call through a fresh Pipeline, like a connector restart."""

    def __init__(self, conn: ArangoConn, root: Path) -> None:
        self.conn = conn
        self.root = root
        self.dead_letter = root / "dead_letter.jsonl"

    def _arango(self) -> dict[str, Any]:
        return {
            "hosts": self.conn.hosts,
            "username": self.conn.username,
            "password": self.conn.password,
            "db_name": self.conn.db_name,
        }

    def run_once(self) -> None:
        state = ArangoStateStore(**self._arango(), collection_name="connector_state")
        sink = ArangoGraphSink(**self._arango(), mapping=MAPPING)
        adapter = DatabricksPollingAdapter(
            list(SPECS.values()),
            lambda: LocalDeltaClient(self.root, batch_size=2),
            state,
            workspace="local",
            interval_seconds=0.05,
        )
        pipeline = Pipeline(
            adapter, sink, dead_letter_path=self.dead_letter, retention=RETENTION, state=state
        )
        poll_once = adapter.poll_once

        def poll_then_stop(emit: Any) -> None:
            poll_once(emit)
            adapter.stop()

        adapter.poll_once = poll_then_stop  # type: ignore[method-assign]
        pipeline.run()
        pipeline.shutdown()

    def db(self) -> Any:
        client = ArangoClient(hosts=self.conn.hosts)
        return client.db(
            self.conn.db_name, username=self.conn.username, password=self.conn.password
        )

    def counts(self) -> dict[str, int]:
        db = self.db()
        return {name: db.collection(name).count() for name in COLLECTIONS}

    def cursors(self) -> dict[str, int]:
        return {
            d["_key"]: d["value"]["version"] for d in self.db().collection("connector_state").all()
        }

    def placed_by(self, order_id: int) -> list[str]:
        return [e["_to"] for e in self.db().collection("PLACED_BY").find({"_key": str(order_id)})]


@pytest.fixture
def harness(arango_conn: ArangoConn, tmp_path: Path) -> Iterator[Harness]:
    init(tmp_path)
    yield Harness(arango_conn, tmp_path)


def test_graph_follows_the_tables(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    # 1. Initial poll: everything from the seed.
    harness.run_once()
    assert harness.counts() == {
        "Customer": 3, "Product": 3, "Order": 3, "PLACED_BY": 3, "CONTAINS": 4
    }  # fmt: skip
    assert harness.db().collection("Product").get("10")["price"] == "49.90"
    item = harness.db().collection("CONTAINS").get("100:11")
    assert (item["_from"], item["_to"], item["quantity"]) == ("Order/100", "Product/11", 2)
    cursors = harness.cursors()
    assert cursors == {f"databricks_local:{name}": 0 for name in SPECS}

    # 2. Nothing changed: counts and cursors stand, nothing is re-read.
    caplog.clear()
    harness.run_once()
    assert harness.counts()["Order"] == 3
    assert harness.cursors() == cursors
    assert "databricks_table_synced" not in caplog.text

    # 3. A new customer and their order.
    append(harness.root, "main.retail.customers", [{"customer_id": 4, "name": "Barbara"}])
    append(
        harness.root,
        "main.retail.orders",
        [
            {
                "order_id": 103,
                "customer_id": 4,
                "status": "placed",
                "ordered_at": datetime(2024, 6, 1),
            }
        ],
    )
    caplog.clear()
    harness.run_once()
    counts = harness.counts()
    assert (counts["Customer"], counts["Order"], counts["PLACED_BY"]) == (4, 4, 4)
    assert harness.placed_by(103) == ["Customer/4"]
    assert "mode=changes rows=1" in caplog.text  # 8. resumed from the stored cursor

    # 4. A non-key attribute: the vertex changes, the edges do not.
    update(harness.root, "main.retail.customers", "customer_id = 1", {"email": "'ada@new.example'"})
    harness.run_once()
    assert harness.db().collection("Customer").get("1")["email"] == "ada@new.example"
    assert harness.counts() == counts

    # 5. An order moves to another customer: still one edge, now pointing there.
    update(harness.root, "main.retail.orders", "order_id = 101", {"customer_id": "3"})
    harness.run_once()
    assert harness.placed_by(101) == ["Customer/3"]
    assert harness.counts()["PLACED_BY"] == 4

    # 6. A null foreign key removes the edge, not the order.
    update(harness.root, "main.retail.orders", "order_id = 102", {"customer_id": "NULL"})
    harness.run_once()
    assert harness.placed_by(102) == []
    assert harness.counts()["PLACED_BY"] == 3
    assert harness.db().collection("Order").has("102")

    # 7. A deleted row stays, and the skip is logged.
    delete(harness.root, "main.retail.customers", "customer_id = 2")
    caplog.clear()
    harness.run_once()
    assert harness.db().collection("Customer").has("2")
    assert "databricks_deletes_skipped table=main.retail.customers count=1" in caplog.text

    assert harness.cursors()["databricks_local:main.retail.orders"] == 3
    assert not harness.dead_letter.exists()
