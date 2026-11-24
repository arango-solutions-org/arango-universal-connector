"""Databricks normalization: JSON-safe values and row envelopes."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from arango_connector.sources.databricks.config import TableSpec
from arango_connector.sources.databricks.normalize import normalize_row, to_json_safe

ORDERS = TableSpec("main.retail.orders", "main", "retail", "orders", ("order_id",))
ITEMS = TableSpec(
    "main.retail.order_items", "main", "retail", "order_items", ("order_id", "product_id")
)
COMMIT = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        ("text", "text"),
        (1.5, 1.5),
        (math.nan, None),
        (math.inf, None),
        (-math.inf, None),
        (Decimal("10.00"), "10.00"),
        (Decimal("1E+3"), "1000"),
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05+00:00"),
        (
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2))),
            "2024-01-02T01:04:05+00:00",
        ),
        (date(2024, 1, 2), "2024-01-02"),
        (b"\x00\xff", "AP8="),
        ([1, Decimal("2.5"), (3,)], [1, "2.5", [3]]),
        ({1: {"x": b"a"}}, {"1": {"x": "YQ=="}}),
    ],
)
def test_to_json_safe(value: Any, expected: Any) -> None:
    assert to_json_safe(value) == expected


def test_to_json_safe_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        to_json_safe(object())


def test_envelope_fields() -> None:
    row = {
        "order_id": 7,
        "customer_id": 3,
        "status": "shipped",
        "ordered_at": datetime(2024, 5, 1, 9, 30),
    }
    envelope = normalize_row(row, ORDERS, "local", "insert", 4, COMMIT)
    assert envelope is not None
    assert envelope.key == "databricks_main.retail.orders_7"
    assert envelope.source == "databricks"
    assert envelope.source_id == "7"
    assert envelope.scope.tenant_id == "local"
    assert envelope.scope.container_id == "main"
    assert envelope.scope.path == ["retail", "orders"]
    assert envelope.created_at is None
    assert envelope.updated_at == COMMIT
    assert envelope.kind == "row"
    assert envelope.payload.table == "main.retail.orders"
    assert envelope.payload.primary_key == {"order_id": 7}
    assert envelope.payload.columns["ordered_at"] == "2024-05-01T09:30:00+00:00"
    assert envelope.raw == {
        "_change_type": "insert",
        "_commit_version": 4,
        "_commit_timestamp": "2024-06-01T12:00:00+00:00",
    }
    json.dumps(envelope.to_document())


def test_composite_key_in_declared_order() -> None:
    row = {"product_id": 9, "quantity": 2, "order_id": 7}
    envelope = normalize_row(row, ITEMS, "local", "snapshot", 0, COMMIT)
    assert envelope is not None
    assert list(envelope.payload.primary_key) == ["order_id", "product_id"]
    assert envelope.source_id == "7:9"
    assert envelope.key == "databricks_main.retail.order_items_7:9"


@pytest.mark.parametrize(
    "row", [{"order_id": None, "status": "x"}, {"status": "x"}, {"order_id": 1.5}]
)
def test_unusable_key_returns_none(row: dict[str, Any]) -> None:
    assert normalize_row(row, ORDERS, "local", "insert", 1, COMMIT) is None
