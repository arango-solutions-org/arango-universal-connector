"""Create and mutate the sample retail Delta tables (plan section 6) for the local demo.

    uv run python dev/delta_seed.py init   [--root ./dev/delta]   # create and seed
    uv run python dev/delta_seed.py mutate [--root ./dev/delta]   # insert, update, delete

The tests import the schemas and helpers from here (``dev`` is on pytest's path), so the
demo and the tests share one definition of the sample data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

DEFAULT_ROOT = Path(__file__).parent / "delta"
CDF = {"delta.enableChangeDataFeed": "true"}

SCHEMAS: dict[str, pa.Schema] = {
    "main.retail.customers": pa.schema(
        [("customer_id", pa.int64()), ("name", pa.string()), ("email", pa.string())]
    ),
    "main.retail.products": pa.schema(
        [("product_id", pa.int64()), ("name", pa.string()), ("price", pa.decimal128(10, 2))]
    ),
    "main.retail.orders": pa.schema(
        [
            ("order_id", pa.int64()),
            ("customer_id", pa.int64()),
            ("status", pa.string()),
            ("ordered_at", pa.timestamp("us")),
        ]
    ),
    "main.retail.order_items": pa.schema(
        [("order_id", pa.int64()), ("product_id", pa.int64()), ("quantity", pa.int32())]
    ),
}

# The DATABRICKS_TABLES value for these tables.
TABLES = (
    "main.retail.customers:customer_id,main.retail.products:product_id,"
    "main.retail.orders:order_id,main.retail.order_items:order_id+product_id"
)

SEED: dict[str, list[dict[str, Any]]] = {
    "main.retail.customers": [
        {"customer_id": 1, "name": "Ada", "email": "ada@example.com"},
        {"customer_id": 2, "name": "Grace", "email": "grace@example.com"},
        {"customer_id": 3, "name": "Edsger", "email": "edsger@example.com"},
    ],
    "main.retail.products": [
        {"product_id": 10, "name": "Keyboard", "price": Decimal("49.90")},
        {"product_id": 11, "name": "Mouse", "price": Decimal("19.00")},
        {"product_id": 12, "name": "Monitor", "price": Decimal("199.99")},
    ],
    "main.retail.orders": [
        {"order_id": 100, "customer_id": 1, "status": "shipped",
         "ordered_at": datetime(2024, 5, 1, 9, 30)},
        {"order_id": 101, "customer_id": 2, "status": "placed",
         "ordered_at": datetime(2024, 5, 2, 14, 0)},
        {"order_id": 102, "customer_id": 1, "status": "placed",
         "ordered_at": datetime(2024, 5, 3, 8, 15)},
    ],
    "main.retail.order_items": [
        {"order_id": 100, "product_id": 10, "quantity": 1},
        {"order_id": 100, "product_id": 11, "quantity": 2},
        {"order_id": 101, "product_id": 12, "quantity": 1},
        {"order_id": 102, "product_id": 10, "quantity": 3},
    ],
}  # fmt: skip


def table_path(root: Path, name: str) -> Path:
    catalog, schema, table = name.split(".")
    return root / catalog / schema / table


def append(root: Path, name: str, rows: list[dict[str, Any]], *, cdf: bool = True) -> None:
    """Append rows, creating the table (with the change feed on, by default) if missing."""
    data = pa.Table.from_pylist(rows, schema=SCHEMAS[name])
    write_deltalake(table_path(root, name), data, mode="append", configuration=CDF if cdf else None)


def update(root: Path, name: str, predicate: str, updates: dict[str, str]) -> None:
    """``updates`` maps columns to SQL expressions, e.g. ``{"status": "'shipped'"}``."""
    DeltaTable(table_path(root, name)).update(predicate=predicate, updates=updates)


def delete(root: Path, name: str, predicate: str) -> None:
    DeltaTable(table_path(root, name)).delete(predicate)


def init(root: Path) -> None:
    for name, rows in SEED.items():
        if table_path(root, name).exists():
            raise SystemExit(f"{table_path(root, name)} exists; remove {root} to start over")
        append(root, name, rows)


def mutate(root: Path) -> None:
    """One insert, one update and one delete, as a later poll would see them."""
    append(root, "main.retail.customers", [{"customer_id": 4, "name": "Barbara", "email": None}])
    append(
        root,
        "main.retail.orders",
        [{"order_id": 103, "customer_id": 4, "status": "placed", "ordered_at": datetime.now()}],
    )
    update(root, "main.retail.orders", "order_id = 101", {"customer_id": "3"})
    delete(root, "main.retail.order_items", "order_id = 102 AND product_id = 10")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["init", "mutate"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    (init if args.command == "init" else mutate)(args.root)
    print(f"{args.command}: done under {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
