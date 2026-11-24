"""Local Delta tables for tests: the sample retail tables (from dev/delta_seed.py)."""

from __future__ import annotations

from arango_connector.sources.databricks.config import TableSpec, parse_table_spec
from delta_seed import SEED, TABLES, append, delete, init, table_path, update

__all__ = ["SEED", "SPECS", "TABLES", "append", "delete", "init", "table_path", "update"]

SPECS: dict[str, TableSpec] = {s.name: s for s in map(parse_table_spec, TABLES.split(","))}
