# Graph sink

`SINK=arango_graph` writes table rows as the vertices and edges of an Arango named graph,
following a declarative mapping file. It is the counterpart of the default
`SINK=arango`, which stores every envelope as one document in `ARANGO_COLLECTION`.

Today it maps `row` envelopes, which the [Databricks source](sources/databricks.md)
produces. It writes only the graph: envelopes are not stored as well. The source tables
are the replay log; clearing a table's cursor re-snapshots it into the graph.

```
Delta tables ──► DatabricksPollingAdapter ──► Envelope(kind=row) ──► Pipeline ──► ArangoGraphSink ──► vertex + edge collections, named graph
                                                                                      ▲ GRAPH_MAPPING_FILE
```

## Configuration

<!-- BEGIN GENERATED: config:arango_graph -->
| Variable | Default | Values | Description |
|---|---|---|---|
| `GRAPH_MAPPING_FILE` | **required** | path | `SINK=arango_graph`: the YAML file mapping table rows to vertices and edges. Format: docs/graph-sink.md. |
<!-- END GENERATED: config:arango_graph -->

The sink uses `ARANGO_HOSTS`, `ARANGO_USERNAME`, `ARANGO_PASSWORD` and `ARANGO_DB` from
the [core settings](../README.md#configuration). `ARANGO_COLLECTION` and retention do
not apply: graph documents never expire. Run with `RETENTION_DAYS=0`.

## Mapping file

A YAML file. The sample for the Databricks quick start is
[`examples/graph/retail.yaml`](../examples/graph/retail.yaml); a real deployment
supplies its own.

```yaml
version: 1
graph: retail
vertices:
  - collection: Order
    source: databricks
    table: main.retail.orders
    attributes: {status: status, ordered_at: ordered_at}   # target: column
    schema: {type: object, required: [status]}             # optional JSON Schema
edges:
  - collection: PLACED_BY
    source: databricks
    table: main.retail.orders
    from: {collection: Order, columns: [order_id]}
    to: {collection: Customer, columns: [customer_id]}
  - collection: CONTAINS
    source: databricks
    table: main.retail.order_items
    from: {collection: Order, columns: [order_id]}
    to: {collection: Product, columns: [product_id]}
    attributes: {quantity: quantity}
```

- `version` must be `1`; `graph` is the named graph to create.
- A rule applies to rows whose envelope `source` and `table` (three-part
  `catalog.schema.table`) match. A table may have a vertex rule, edge rules, or both:
  `orders` above yields an `Order` vertex and a `PLACED_BY` edge.
- **Vertex `attributes`** map target attribute to source column, renaming as they go.
  Omitted, every column is copied under its own name.
- **Edge `from` / `to`** name the vertex collection at each end and the row's columns
  holding that vertex's primary key values, in the vertex table's key order.
  **Edge `attributes`** work like vertex attributes; omitted, the edge has none.
- **`schema`** (vertex rules, optional) is a JSON Schema that Arango enforces on the
  collection with `level: strict`.
- Only column renames: no expressions, type casts or code hooks.

The file is checked at startup, and the connector refuses to start (naming the problem)
if `version` is not 1, a collection or graph name is not a valid Arango name, a
collection has more than one rule, a collection is used as both vertex and edge, an edge
points at a collection that has no vertex rule, a table name is not three-part, or a key
is misspelled.

## Keys

Every document is keyed by the primary key of the row it came from, through the same
function the source uses (see the Databricks [document shape](sources/databricks.md#document-shape)):
one value as is, a composite key joined with `:`, anything else hashed to `h-` and 40
hex characters. A foreign key holding the same value as a primary key therefore always
points at the right vertex: `_from` and `_to` are `{collection}/{key}`.

**An edge is keyed by its row, not its endpoints.** The `PLACED_BY` edge of order 100
has key `100`. When the order's `customer_id` changes, the edge is overwritten under the
same key and now points at the new customer: it moves rather than leaving the old one
behind. When a `from` or `to` column becomes null, the edge is removed. Re-reading any
row overwrites its vertices and edges, never duplicates them.

Key and foreign-key columns should be integer or string columns. Values are made
JSON-safe before keying, so a decimal key with a scale keys as `"10.00"`.

## What setup does

On every start, idempotently:

1. Creates the database if missing.
2. Creates each vertex collection and each edge collection (as an edge collection).
3. Sets each `schema` as its collection's validation rule (`level: strict`), logging
   `arango_schema_created` or `arango_schema_changed` when it does.
4. Creates the named graph with one edge definition per edge rule; vertex collections in
   no edge definition become orphan collections. If the graph already exists with
   different definitions, it logs `arango_graph_definition_differs` at WARNING and
   leaves the graph as it is.
5. Logs `arango_graph_sink_ready`.

## Writes

For each envelope: vertices, then edges, then edge removals. Upserts replace the whole
document; removals ignore a document that is already gone. Transient failures are
retried three times with backoff, like the default sink.

- Edges are written through the collection API, so an edge may briefly point at a
  vertex that has not arrived yet (a child table synced before its parent). The vertex
  appears when its own row is written.
- A document that violates its collection's schema is a client error: it is not
  retried, the envelope goes to the [dead-letter file](../README.md#dead-letter-file) and
  the connector carries on.
- Rows from a table with no rule, and envelopes of other kinds, are skipped and logged
  once per table as `graph_unmapped_table` at WARNING.

## Limitations

Each has a `TODO(future):` at its extension point.

- **Deletes.** A row deleted in its table leaves its vertex and edges in place.
- **Changing the mapping** does not migrate existing documents, drop collections, or
  update an existing graph's definitions; removing a `schema` leaves the old rule on the
  collection. Drop the graph or the affected collections and clear the cursors to
  rebuild.
- Kinds other than `row` (Slack, Gmail, Jira into the graph), identity resolution
  across sources, mapping expressions and type casts.
- Envelopes are not stored next to the graph, and there is no reproject command.
- SmartGraphs, ArangoSearch views and batched writes.
