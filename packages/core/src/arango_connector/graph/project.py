"""Pure projection of one envelope onto the graph its mapping describes. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector.graph.mapping import Endpoint, GraphMapping
from arango_connector.keys import key_part
from arango_connector.payloads import RowPayload


class ProjectionError(ValueError):
    """The row lacks a column the mapping needs, or holds an unusable key value."""


@dataclass(frozen=True)
class GraphDocument:
    collection: str
    document: dict[str, Any]


@dataclass(frozen=True)
class EdgeRemoval:
    collection: str
    key: str


@dataclass
class GraphBatch:
    vertices: list[GraphDocument] = field(default_factory=list)
    edges: list[GraphDocument] = field(default_factory=list)
    edge_removals: list[EdgeRemoval] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.vertices or self.edges or self.edge_removals)


def _column(row: RowPayload, column: str) -> Any:
    if column not in row.columns:
        raise ProjectionError(f"{row.table}: column {column!r} is not in the row")
    return row.columns[column]


def _attributes(row: RowPayload, attributes: dict[str, str]) -> dict[str, Any]:
    return {target: _column(row, column) for target, column in attributes.items()}


def _key(values: list[Any], what: str) -> str:
    try:
        return key_part(values)
    except ValueError as exc:
        raise ProjectionError(f"{what}: {exc}") from None


def _endpoint(row: RowPayload, end: Endpoint) -> str | None:
    """``collection/key`` of the vertex the row points at, or ``None`` for a null key."""
    values = [_column(row, c) for c in end.columns]
    if any(v is None for v in values):
        return None
    return f"{end.collection}/{_key(values, f'{row.table} -> {end.collection}')}"


def project(envelope: Envelope[Any], mapping: GraphMapping) -> GraphBatch:
    """The vertices, edges and edge removals one row envelope implies.

    Every document is keyed by the row's primary key: a foreign-key edge belongs to its
    child row, so when the foreign key changes the edge is overwritten in place (moves)
    rather than duplicated, and when it becomes null the edge is removed. Envelopes of
    other kinds, or from tables with no rule, project to an empty batch.
    """
    batch = GraphBatch()
    row = envelope.payload
    if not isinstance(row, RowPayload):
        return batch
    key = _key(list(row.primary_key.values()), row.table)

    for vertex in mapping.vertices:
        if vertex.source == envelope.source and vertex.table == row.table:
            attributes = (
                row.columns if vertex.attributes is None else _attributes(row, vertex.attributes)
            )
            batch.vertices.append(GraphDocument(vertex.collection, {**attributes, "_key": key}))

    for edge in mapping.edges:
        if edge.source != envelope.source or edge.table != row.table:
            continue
        source, target = _endpoint(row, edge.from_), _endpoint(row, edge.to)
        if source is None or target is None:
            batch.edge_removals.append(EdgeRemoval(edge.collection, key))
            continue
        attributes = _attributes(row, edge.attributes or {})
        batch.edges.append(
            GraphDocument(
                edge.collection, {**attributes, "_key": key, "_from": source, "_to": target}
            )
        )
    return batch
