"""Graph projection: row envelopes to vertices, edges and edge removals."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from arango_connector.envelope import Envelope, Scope
from arango_connector.graph.mapping import GraphMapping, load_mapping
from arango_connector.graph.project import (
    EdgeRemoval,
    GraphDocument,
    ProjectionError,
    project,
)
from arango_connector.payloads import MessagePayload
from arango_connector.sources.databricks.config import parse_table_spec
from arango_connector.sources.databricks.normalize import normalize_row
from delta_tables import SPECS

MAPPING = load_mapping(Path(__file__).resolve().parents[1] / "examples/graph/retail.yaml")
COMMIT = datetime(2024, 6, 1, tzinfo=UTC)


def row(table: str, **columns: Any) -> Envelope[Any]:
    envelope = normalize_row(columns, SPECS[f"main.retail.{table}"], "local", "insert", 1, COMMIT)
    assert envelope is not None
    return envelope


def order(customer_id: int | None = 1) -> Envelope[Any]:
    return row("orders", order_id=100, customer_id=customer_id, status="placed", ordered_at=None)


def test_vertex_with_renamed_attributes() -> None:
    batch = project(row("products", product_id=10, name="Keyboard", price="49.90"), MAPPING)
    assert batch.vertices == [
        GraphDocument("Product", {"_key": "10", "name": "Keyboard", "price": "49.90"})
    ]
    assert batch.edges == batch.edge_removals == []


def test_renames_use_the_target_name() -> None:
    mapping = GraphMapping.model_validate(
        {
            "version": 1,
            "graph": "g",
            "vertices": [
                {
                    "collection": "Product",
                    "source": "databricks",
                    "table": "main.retail.products",
                    "attributes": {"title": "name"},
                }
            ],
        }
    )
    batch = project(row("products", product_id=10, name="Keyboard", price="1"), mapping)
    assert batch.vertices[0].document == {"_key": "10", "title": "Keyboard"}


def test_vertex_with_all_columns() -> None:
    batch = project(row("customers", customer_id=1, name="Ada", email=None), MAPPING)
    assert batch.vertices == [
        GraphDocument("Customer", {"_key": "1", "customer_id": 1, "name": "Ada", "email": None})
    ]


def test_one_table_yields_a_vertex_and_an_edge() -> None:
    batch = project(order(), MAPPING)
    assert batch.vertices == [
        GraphDocument("Order", {"_key": "100", "status": "placed", "ordered_at": None})
    ]
    assert batch.edges == [
        GraphDocument("PLACED_BY", {"_key": "100", "_from": "Order/100", "_to": "Customer/1"})
    ]


def test_bridge_table_edge_with_attributes() -> None:
    batch = project(row("order_items", order_id=100, product_id=10, quantity=2), MAPPING)
    assert batch.vertices == []
    assert batch.edges == [
        GraphDocument(
            "CONTAINS",
            {"_key": "100:10", "_from": "Order/100", "_to": "Product/10", "quantity": 2},
        )
    ]


def test_null_foreign_key_removes_the_edge() -> None:
    batch = project(order(customer_id=None), MAPPING)
    assert batch.edges == []
    assert batch.edge_removals == [EdgeRemoval("PLACED_BY", "100")]
    assert len(batch.vertices) == 1


def test_changed_foreign_key_keeps_the_edge_key() -> None:
    before, after = project(order(1), MAPPING).edges[0], project(order(2), MAPPING).edges[0]
    assert before.document["_key"] == after.document["_key"] == "100"
    assert after.document["_to"] == "Customer/2"


def test_unmapped_table_and_other_kinds_project_nothing() -> None:
    returns = parse_table_spec("main.retail.returns:order_id")
    other = normalize_row({"order_id": 1}, returns, "local", "insert", 1, COMMIT)
    assert other is not None
    assert not project(other, MAPPING)
    message = Envelope[MessagePayload](
        key="slack_x",
        source="slack",
        source_id="x",
        scope=Scope(container_id="C1"),
        created_at=None,
        payload=MessagePayload(author_type="user"),
        raw={},
    )
    assert not project(message, MAPPING)


def test_missing_column_raises() -> None:
    with pytest.raises(ProjectionError, match="'status' is not in the row"):
        project(row("orders", order_id=100, customer_id=1, ordered_at=None), MAPPING)
