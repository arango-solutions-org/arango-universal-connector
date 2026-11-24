"""The graph mapping file: which table rows become which vertices and edges.

TODO(future): mapping expressions, type casts and code hooks; kinds other than ``row``
(Slack, Gmail, Jira into the graph); identity resolution across sources.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from arango_connector.config import ConfigError
from arango_connector.sources.databricks.config import parse_table_name

# ArangoDB's traditional collection and graph names.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,255}$")


def _check_name(value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError(f"not a valid ArangoDB collection or graph name: {value!r}")
    return value


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    collection: str
    source: str
    # Three-part ``catalog.schema.table``, the same on every backend.
    table: str

    @field_validator("collection")
    @classmethod
    def _valid_collection(cls, value: str) -> str:
        return _check_name(value)

    @field_validator("table")
    @classmethod
    def _valid_table(cls, value: str) -> str:
        parse_table_name(value)
        return value


class Endpoint(BaseModel):
    """One end of an edge: the vertex collection, and the row's columns holding that
    vertex's primary key values, in key order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: str
    columns: list[str] = Field(min_length=1)


class VertexRule(_Rule):
    # target attribute -> source column. ``None``: every column under its own name.
    attributes: dict[str, str] | None = None
    # Optional JSON Schema, enforced by ArangoDB with ``level: strict``.
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class EdgeRule(_Rule):
    from_: Endpoint = Field(alias="from")
    to: Endpoint
    # target attribute -> source column. ``None``: no attributes.
    attributes: dict[str, str] | None = None


class GraphMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    graph: str
    vertices: list[VertexRule] = Field(default_factory=list)
    edges: list[EdgeRule] = Field(default_factory=list)

    @field_validator("graph")
    @classmethod
    def _valid_graph(cls, value: str) -> str:
        return _check_name(value)

    @model_validator(mode="after")
    def _check_rules(self) -> GraphMapping:
        vertex_names = [r.collection for r in self.vertices]
        edge_names = [r.collection for r in self.edges]
        for kind, names in (("vertex", vertex_names), ("edge", edge_names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            if duplicates:
                raise ValueError(f"more than one {kind} rule for {', '.join(duplicates)}")
        both = sorted(set(vertex_names) & set(edge_names))
        if both:
            raise ValueError(f"used as both vertex and edge collection: {', '.join(both)}")
        for rule in self.edges:
            for end in (rule.from_, rule.to):
                if end.collection not in vertex_names:
                    raise ValueError(
                        f"edge {rule.collection} points at {end.collection}, "
                        "which is not a declared vertex collection"
                    )
        return self


def load_mapping(path: Path | str) -> GraphMapping:
    """Read and validate a mapping file. Raises ``ConfigError`` naming the problem."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"graph mapping {path}: cannot read: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"graph mapping {path}: not valid YAML: {exc}") from None
    try:
        return GraphMapping.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            reason = err["msg"].removeprefix("Value error, ")
            problems.append(f"  - {where}: {reason}" if where else f"  - {reason}")
        raise ConfigError(f"graph mapping {path} is invalid:\n" + "\n".join(problems)) from None
