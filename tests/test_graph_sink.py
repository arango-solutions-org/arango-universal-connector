"""ArangoGraphSink against a real ArangoDB."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arango import ArangoClient
from arango.exceptions import DocumentInsertError

from arango_connector.envelope import Envelope
from arango_connector.graph.mapping import load_mapping
from arango_connector.sources.databricks.config import parse_table_spec
from arango_connector.sources.databricks.normalize import normalize_row
from arango_connector_standalone.sinks.graph import ArangoGraphSink
from delta_tables import SPECS

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

MAPPING = load_mapping(Path(__file__).resolve().parents[1] / "examples/graph/retail.yaml")
COMMIT = datetime(2024, 6, 1, tzinfo=UTC)


def row(table: str, **columns: Any) -> Envelope[Any]:
    envelope = normalize_row(columns, SPECS[f"main.retail.{table}"], "local", "insert", 1, COMMIT)
    assert envelope is not None
    return envelope


def order(order_id: int = 100, customer_id: int | None = 1, status: Any = "placed") -> Any:
    return row("orders", order_id=order_id, customer_id=customer_id, status=status, ordered_at=None)


@pytest.fixture
def sink(arango_conn: ArangoConn) -> Iterator[ArangoGraphSink]:
    built = ArangoGraphSink(
        arango_conn.hosts,
        arango_conn.username,
        arango_conn.password,
        arango_conn.db_name,
        MAPPING,
        retry_attempts=1,
    )
    built.setup()
    yield built
    built.close()


@pytest.fixture
def db(arango_conn: ArangoConn) -> Any:
    client = ArangoClient(hosts=arango_conn.hosts)
    return client.db(
        arango_conn.db_name, username=arango_conn.username, password=arango_conn.password
    )


def test_setup_creates_collections_schema_and_graph(
    sink: ArangoGraphSink, db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("Customer", "Product", "Order"):
        assert db.collection(name).properties()["edge"] is False
    for name in ("PLACED_BY", "CONTAINS"):
        assert db.collection(name).properties()["edge"] is True
    schema = db.collection("Order").properties()["schema"]
    assert schema["level"] == "strict"
    assert schema["rule"]["required"] == ["status"]
    assert db.collection("Customer").properties().get("schema") is None
    graph = db.graph("retail")
    assert {d["edge_collection"] for d in graph.edge_definitions()} == {"PLACED_BY", "CONTAINS"}
    assert set(graph.vertex_collections()) == {"Customer", "Product", "Order"}

    with caplog.at_level(logging.INFO):
        sink.setup()
    noisy = ("created", "changed", "differs")
    assert not [r.message for r in caplog.records if any(w in r.message for w in noisy)]


def test_writes_are_idempotent(sink: ArangoGraphSink, db: Any) -> None:
    sink.write([order(), order()])
    assert db.collection("Order").count() == 1
    assert db.collection("PLACED_BY").count() == 1
    assert db.collection("Order").get("100")["status"] == "placed"


def test_changed_foreign_key_moves_the_edge(sink: ArangoGraphSink, db: Any) -> None:
    sink.write([order(customer_id=1)])
    sink.write([order(customer_id=2)])
    edges = list(db.collection("PLACED_BY").all())
    assert [(e["_key"], e["_from"], e["_to"]) for e in edges] == [
        ("100", "Order/100", "Customer/2")
    ]


def test_null_foreign_key_removes_the_edge(sink: ArangoGraphSink, db: Any) -> None:
    sink.write([order(customer_id=1)])
    sink.write([order(customer_id=None)])
    assert db.collection("PLACED_BY").count() == 0
    sink.write([order(customer_id=None)])  # removing an absent edge is fine
    assert db.collection("Order").count() == 1


def test_schema_violation_raises(sink: ArangoGraphSink, db: Any) -> None:
    with pytest.raises(DocumentInsertError) as info:
        sink.write([order(status=None)])
    assert info.value.http_code == 400
    assert db.collection("Order").count() == 0


def test_traversal_reaches_the_customer(sink: ArangoGraphSink, db: Any) -> None:
    sink.write(
        [
            row("customers", customer_id=1, name="Ada", email="ada@example.com"),
            order(),
            row("products", product_id=10, name="Keyboard", price="49.90"),
            row("order_items", order_id=100, product_id=10, quantity=2),
        ]
    )
    names = list(
        db.aql.execute(
            "FOR v, e IN 1..1 OUTBOUND 'Order/100' GRAPH 'retail' SORT v.name "
            "RETURN [v.name, e.quantity]"
        )
    )
    assert names == [["Ada", None], ["Keyboard", 2]]


def test_unmapped_rows_are_skipped_and_logged_once(
    sink: ArangoGraphSink, caplog: pytest.LogCaptureFixture
) -> None:
    other = normalize_row(
        {"id": 1}, parse_table_spec("main.retail.returns:id"), "local", "insert", 1, COMMIT
    )
    assert other is not None
    sink.write([other, other])
    assert (
        caplog.text.count("graph_unmapped_table source=databricks table=main.retail.returns") == 1
    )
