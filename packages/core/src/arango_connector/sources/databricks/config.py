"""Settings for the Databricks source. Loaded only when ``SOURCE=databricks``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, split_csv

_NAME_PART_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class TableSpec:
    """One configured table: its three-part name and primary key columns."""

    name: str
    catalog: str
    schema: str
    table: str
    primary_key: tuple[str, ...]


def parse_table_name(name: str) -> tuple[str, str, str]:
    """Split ``catalog.schema.table``; raises ``ValueError`` unless it has three valid parts."""
    parts = name.split(".")
    if len(parts) != 3 or not all(_NAME_PART_RE.match(p) for p in parts):
        raise ValueError(f"not a catalog.schema.table name: {name!r}")
    return parts[0], parts[1], parts[2]


def parse_table_spec(entry: str) -> TableSpec:
    """Parse ``catalog.schema.table:pk`` or ``catalog.schema.table:pk1+pk2``."""
    name, sep, keys = entry.strip().partition(":")
    catalog, schema, table = parse_table_name(name.strip())
    primary_key = tuple(k.strip() for k in keys.split("+") if k.strip())
    if not sep or not primary_key:
        raise ValueError(f"{name.strip()!r} needs a primary key, e.g. {name.strip()}:id")
    for column in primary_key:
        if not _NAME_PART_RE.match(column):
            raise ValueError(f"not a column name: {column!r}")
    return TableSpec(name.strip(), catalog, schema, table, primary_key)


class DatabricksSettings(ConnectorSettings):
    databricks_backend: Literal["delta"] = Field(
        default="delta",
        # TODO(future): "sql", a DatabricksSqlClient on databricks-sql-connector.
        description="Where tables are read from. `delta`: local Delta Lake tables under "
        "`DATABRICKS_DELTA_ROOT` (a stand-in until Databricks access exists).",
    )
    databricks_workspace: str = Field(
        default="local",
        pattern=r"^[A-Za-z0-9_.\-]+$",
        description="Label for where the tables live, used as `scope.tenant_id` and in "
        "cursor keys. Changing it re-snapshots every table.",
    )
    databricks_tables: Annotated[list[TableSpec], NoDecode] = Field(
        min_length=1,
        description="Tables to sync, comma-separated `catalog.schema.table:pk` entries; "
        "composite keys as `pk1+pk2`, e.g. "
        "`main.retail.orders:order_id,main.retail.order_items:order_id+product_id`.",
    )
    databricks_delta_root: Path | None = Field(
        default=None,
        description="`delta` backend: local directory holding the tables; "
        "`catalog.schema.table` resolves to `{root}/{catalog}/{schema}/{table}`.",
    )
    databricks_poll_interval_seconds: float = Field(
        default=60, gt=0, description="Seconds between polls."
    )
    databricks_batch_size: int = Field(
        default=1000, ge=1, description="Rows per batch read from a table."
    )

    @field_validator("databricks_tables", mode="before")
    @classmethod
    def _parse_tables(cls, value: Any) -> Any:
        value = split_csv(value)
        if not isinstance(value, list):
            return value
        specs = [v if isinstance(v, TableSpec) else parse_table_spec(str(v)) for v in value]
        names = [s.name for s in specs]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate tables: {', '.join(duplicates)}")
        return specs

    @model_validator(mode="after")
    def _check_backend(self) -> DatabricksSettings:
        if self.databricks_backend == "delta" and self.databricks_delta_root is None:
            raise ValueError("DATABRICKS_DELTA_ROOT is required when DATABRICKS_BACKEND=delta")
        return self
