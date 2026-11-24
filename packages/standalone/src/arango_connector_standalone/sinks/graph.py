"""Graph sink: project row envelopes onto the vertex and edge collections of a named graph."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from arango import ArangoClient
from arango.database import StandardDatabase
from pydantic import Field

from arango_connector.config import ConnectorSettings
from arango_connector.envelope import Envelope
from arango_connector.graph.mapping import GraphMapping
from arango_connector.graph.project import project
from arango_connector_standalone.arango_common import connect, ensure_collection, retrying
from arango_connector_standalone.sinks.base import Sink

logger = logging.getLogger(__name__)


class GraphSinkSettings(ConnectorSettings):
    """Settings for ``SINK=arango_graph``. ArangoDB access comes from the core settings;
    ``ARANGO_COLLECTION`` and retention do not apply."""

    graph_mapping_file: Path = Field(
        description="`SINK=arango_graph`: the YAML file mapping table rows to vertices and "
        "edges. Format: docs/graph-sink.md.",
    )


def edge_definitions(mapping: GraphMapping) -> list[dict[str, Any]]:
    return [
        {
            "edge_collection": rule.collection,
            "from_vertex_collections": [rule.from_.collection],
            "to_vertex_collections": [rule.to.collection],
        }
        for rule in mapping.edges
    ]


def _definition_set(definitions: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
    return {
        (
            d["edge_collection"],
            tuple(sorted(d["from_vertex_collections"])),
            tuple(sorted(d["to_vertex_collections"])),
        )
        for d in definitions
    }


class ArangoGraphSink(Sink):
    """Writes each envelope's projection: vertices, then edges, then edge removals.

    Every document is keyed deterministically from its row, and upserts replace, so
    re-delivery overwrites. Edges go through the collection API, so an edge may briefly
    point at a vertex that has not arrived yet.

    TODO(future): store envelopes alongside the graph and add a reproject command.
    TODO(future): reconcile a changed graph definition or schema with existing data.
    TODO(future): SmartGraphs, ArangoSearch views, batched writes.
    """

    def __init__(
        self,
        hosts: str | list[str],
        username: str,
        password: str,
        db_name: str,
        mapping: GraphMapping,
        *,
        retry_attempts: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 5.0,
    ) -> None:
        self.hosts = hosts
        self.db_name = db_name
        self.mapping = mapping
        self._username = username
        self._password = password
        self._retry_attempts = retry_attempts
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        self._client: ArangoClient | None = None
        self._db: StandardDatabase | None = None
        self._unmapped: set[tuple[str, str]] = set()

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        """Create the collections, schema rules and named graph if missing. Idempotent."""
        self._client, db = connect(self.hosts, self._username, self._password, self.db_name)
        for vertex in self.mapping.vertices:
            ensure_collection(db, vertex.collection)
            if vertex.schema_ is not None:
                self._ensure_schema(db, vertex.collection, vertex.schema_)
        for edge in self.mapping.edges:
            ensure_collection(db, edge.collection, edge=True)
        self._ensure_graph(db)
        self._db = db
        logger.info(
            "arango_graph_sink_ready hosts=%s db=%s graph=%s vertices=%s edges=%s",
            self.hosts,
            self.db_name,
            self.mapping.graph,
            len(self.mapping.vertices),
            len(self.mapping.edges),
        )

    def _ensure_schema(self, db: StandardDatabase, name: str, rule: dict[str, Any]) -> None:
        collection = db.collection(name)
        # The sync API returns plain values; the union covers async and batch execution.
        current = cast(dict[str, Any], collection.properties()).get("schema") or {}
        if current.get("rule") == rule and current.get("level") == "strict":
            return
        collection.configure(
            schema={
                "rule": rule,
                "level": "strict",
                "message": f"document does not match the {name} schema in the graph mapping",
            }
        )
        logger.info("arango_schema_%s collection=%s", "changed" if current else "created", name)

    def _ensure_graph(self, db: StandardDatabase) -> None:
        definitions = edge_definitions(self.mapping)
        in_edges = {r.from_.collection for r in self.mapping.edges}
        in_edges |= {r.to.collection for r in self.mapping.edges}
        orphans = [v.collection for v in self.mapping.vertices if v.collection not in in_edges]
        name = self.mapping.graph
        if not db.has_graph(name):
            db.create_graph(name, edge_definitions=definitions, orphan_collections=orphans)
            logger.info("arango_graph_created graph=%s", name)
            return
        graph = db.graph(name)
        existing = cast(list[dict[str, Any]], graph.edge_definitions())
        vertices = cast(list[str], graph.vertex_collections())
        same_edges = _definition_set(existing) == _definition_set(definitions)
        if not same_edges or not set(orphans) <= set(vertices):
            logger.warning(
                "arango_graph_definition_differs graph=%s the existing graph is left "
                "unchanged; update or drop it to apply the mapping",
                name,
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = self._db = None

    # -- writes ------------------------------------------------------------

    def write(self, envelopes: list[Envelope[Any]]) -> None:
        """Project and write each envelope, retrying transient failures. A schema
        violation is a client error: it is not retried and propagates."""
        if self._db is None:
            raise RuntimeError("ArangoGraphSink.setup() must be called before write()")
        db = self._db
        retry = retrying(self._retry_attempts, self._retry_wait_initial, self._retry_wait_max)
        for envelope in envelopes:
            batch = project(envelope, self.mapping)
            if not batch:
                self._log_unmapped(envelope)
                continue
            for doc in (*batch.vertices, *batch.edges):
                retry(
                    db.collection(doc.collection).insert,
                    doc.document,
                    overwrite_mode="replace",
                    silent=True,
                )
            for removal in batch.edge_removals:
                retry(db.collection(removal.collection).delete, removal.key, ignore_missing=True)

    def _log_unmapped(self, envelope: Envelope[Any]) -> None:
        table = str(getattr(envelope.payload, "table", f"kind:{envelope.kind}"))
        if (envelope.source, table) not in self._unmapped:
            self._unmapped.add((envelope.source, table))
            logger.warning("graph_unmapped_table source=%s table=%s", envelope.source, table)
