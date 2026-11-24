"""DatabricksSettings: DATABRICKS_TABLES parsing and backend checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from arango_connector.config import ConfigError, load_settings
from arango_connector.sources.databricks.config import DatabricksSettings, TableSpec


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in ("DATABRICKS_TABLES", "DATABRICKS_DELTA_ROOT", "DATABRICKS_BACKEND"):
        monkeypatch.delenv(var, raising=False)


def load(**overrides: object) -> DatabricksSettings:
    return load_settings(DatabricksSettings, **{"databricks_delta_root": "/data", **overrides})


def test_parses_tables_and_composite_keys() -> None:
    settings = load(
        databricks_tables="main.retail.orders:order_id, main.retail.order_items:order_id+product_id"
    )
    assert settings.databricks_tables == [
        TableSpec("main.retail.orders", "main", "retail", "orders", ("order_id",)),
        TableSpec(
            "main.retail.order_items", "main", "retail", "order_items", ("order_id", "product_id")
        ),
    ]
    assert settings.databricks_workspace == "local"
    assert settings.databricks_delta_root == Path("/data")


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_TABLES", "a.b.c:id")
    monkeypatch.setenv("DATABRICKS_DELTA_ROOT", "/tables")
    settings = load_settings(DatabricksSettings)
    assert settings.databricks_tables[0].primary_key == ("id",)


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        ("retail.orders:id", "not a catalog.schema.table name"),
        ("a.b.c.d:id", "not a catalog.schema.table name"),
        ("a.b-x.c:id", "not a catalog.schema.table name"),
        ("a.b.c", "needs a primary key"),
        ("a.b.c:", "needs a primary key"),
        ("a.b.c:id+bad-col", "not a column name"),
        ("a.b.c:id,a.b.c:other", "duplicate tables: a.b.c"),
        ("", "DATABRICKS_TABLES"),
    ],
)
def test_rejects_bad_tables(tables: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(databricks_tables=tables)


def test_delta_root_required_for_delta_backend() -> None:
    with pytest.raises(ConfigError, match="DATABRICKS_DELTA_ROOT is required"):
        load_settings(DatabricksSettings, databricks_tables="a.b.c:id")
