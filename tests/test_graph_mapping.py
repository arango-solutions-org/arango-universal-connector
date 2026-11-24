"""Graph mapping file: the example loads and each validation rule rejects a bad file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from arango_connector.config import ConfigError
from arango_connector.graph.mapping import load_mapping

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/graph/retail.yaml"


def test_example_loads() -> None:
    mapping = load_mapping(EXAMPLE)
    assert mapping.graph == "retail"
    assert [v.collection for v in mapping.vertices] == ["Customer", "Product", "Order"]
    assert [e.collection for e in mapping.edges] == ["PLACED_BY", "CONTAINS"]
    placed_by = mapping.edges[0]
    assert (placed_by.from_.collection, placed_by.to.columns) == ("Order", ["customer_id"])
    assert mapping.vertices[0].attributes is None
    assert mapping.vertices[2].schema_ is not None


def base() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text())
    return data


def write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def _set(data: dict[str, Any], change: str) -> dict[str, Any]:
    if change == "version":
        data["version"] = 2
    elif change == "collection_name":
        data["vertices"][0]["collection"] = "1-bad name"
    elif change == "graph_name":
        data["graph"] = "bad graph"
    elif change == "duplicate_vertex":
        data["vertices"].append(dict(data["vertices"][0]))
    elif change == "duplicate_edge":
        data["edges"].append(dict(data["edges"][0]))
    elif change == "undeclared_endpoint":
        data["edges"][0]["to"]["collection"] = "Shop"
    elif change == "table_name":
        data["vertices"][0]["table"] = "retail.customers"
    elif change == "vertex_and_edge":
        data["edges"][1]["collection"] = "Customer"
    elif change == "unknown_field":
        data["vertices"][0]["colour"] = "red"
    elif change == "empty_columns":
        data["edges"][0]["from"]["columns"] = []
    return data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("version", "version: Input should be 1"),
        ("collection_name", "not a valid ArangoDB collection or graph name: '1-bad name'"),
        ("graph_name", "graph: not a valid ArangoDB"),
        ("duplicate_vertex", "more than one vertex rule for Customer"),
        ("duplicate_edge", "more than one edge rule for PLACED_BY"),
        ("undeclared_endpoint", "points at Shop, which is not a declared vertex collection"),
        ("table_name", "not a catalog.schema.table name: 'retail.customers'"),
        ("vertex_and_edge", "used as both vertex and edge collection: Customer"),
        ("unknown_field", "vertices.0.colour: Extra inputs are not permitted"),
        ("empty_columns", "edges.0.from.columns"),
    ],
)
def test_rejects_bad_files(tmp_path: Path, change: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")) as info:
        load_mapping(write(tmp_path, _set(base(), change)))
    assert str(tmp_path) in str(info.value)


def test_rejects_unreadable_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_mapping(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_mapping(write(tmp_path, "vertices: [unclosed"))
    with pytest.raises(ConfigError, match="invalid"):
        load_mapping(write(tmp_path, "just a string"))
