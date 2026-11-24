# Databricks source

`SOURCE=databricks`. Polls each Delta table in `DATABRICKS_TABLES` every
`DATABRICKS_POLL_INTERVAL_SECONDS` (60 by default). The first poll of a table reads a
snapshot; later polls read only the commits since, from the table's change data feed.
Inserts and updates arrive within about one poll interval.

> **Backend: local Delta Lake for now.** There is no Databricks access yet, so the only
> backend (`DATABRICKS_BACKEND=delta`) reads open-source Delta Lake tables from a local
> directory with [delta-rs](https://delta-io.github.io/delta-rs/). Everything else
> (settings names, keys, cursors, document shape) is already named and shaped for
> Databricks, so moving to real Databricks adds one client class and some settings. See
> [Not supported yet](#not-supported-yet).

Pair it with [`SINK=arango_graph`](../graph-sink.md) to write rows as vertices and edges
of a named graph, or with the default `SINK=arango` to store each row as an envelope.

| | |
|---|---|
| Kind | `row` |
| Delivery | poll, a snapshot then the Delta change data feed (CDF) |
| Keys | `databricks_{catalog.schema.table}_{primary key}`, deterministic, so a re-read overwrites |
| Cursor | per table in `connector_state`, key `{workspace}:{table}`: `{version, last_synced_at}` |
| Access | read-only: the connector only reads table data, history and properties |

## Setup

1. **Turn on the change data feed** for every table to sync. The connector refuses a
   table without it. For a new Delta table, set the property when creating it
   (`delta.enableChangeDataFeed = true`); on Databricks,
   `ALTER TABLE t SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`. Changes made
   before the property was set are not in the feed, so the first sync takes a snapshot.
2. **Lay out the tables** under one directory, `{root}/{catalog}/{schema}/{table}`, and
   point `DATABRICKS_DELTA_ROOT` at it. Names are always three-part
   (`catalog.schema.table`) so they stay the same on Databricks.
3. **List the tables and their primary keys** in `DATABRICKS_TABLES`, e.g.
   `main.retail.orders:order_id,main.retail.order_items:order_id+product_id`. Key and
   foreign-key columns should be integer or string columns: values are made JSON-safe
   before keying, so a decimal key with a scale keys as `"10.00"`.

For a quick start, `make delta-seed` creates sample retail tables under `dev/delta`
(customers, products, orders, order items) and `make delta-mutate` inserts, updates and
deletes a row in them.

## Configuration

<!-- BEGIN GENERATED: config:databricks -->
| Variable | Default | Values | Description |
|---|---|---|---|
| `DATABRICKS_BACKEND` | `delta` | `delta` | Where tables are read from. `delta`: local Delta Lake tables under `DATABRICKS_DELTA_ROOT` (a stand-in until Databricks access exists). |
| `DATABRICKS_WORKSPACE` | `local` | text | Label for where the tables live, used as `scope.tenant_id` and in cursor keys. Changing it re-snapshots every table. |
| `DATABRICKS_TABLES` | **required** | comma-separated | Tables to sync, comma-separated `catalog.schema.table:pk` entries; composite keys as `pk1+pk2`, e.g. `main.retail.orders:order_id,main.retail.order_items:order_id+product_id`. |
| `DATABRICKS_DELTA_ROOT` | — | path | `delta` backend: local directory holding the tables; `catalog.schema.table` resolves to `{root}/{catalog}/{schema}/{table}`. |
| `DATABRICKS_POLL_INTERVAL_SECONDS` | `60` | number > 0 | Seconds between polls. |
| `DATABRICKS_BATCH_SIZE` | `1000` | integer ≥ 1 | Rows per batch read from a table. |
<!-- END GENERATED: config:databricks -->

The connector refuses to start if a table name is not three parts of letters, digits and
underscores, a table lacks a primary key, a table is listed twice, or
`DATABRICKS_DELTA_ROOT` is missing with the `delta` backend.

## Running

**Locally**, against Arango from `docker compose up -d arangodb` (or the disposable one
from `make test-db-up`), with the sample tables:

```bash
make delta-seed
SOURCE=databricks SINK=arango_graph RETENTION_DAYS=0 \
  DATABRICKS_DELTA_ROOT=./dev/delta GRAPH_MAPPING_FILE=./examples/graph/retail.yaml make run
```

**In Docker**, the `connector-databricks` Compose service runs `SINK=arango_graph` with
retention off. It mounts `./dev/delta` (override with `DATABRICKS_HOST_DELTA_ROOT`) and
`./examples/graph/retail.yaml` (override with `HOST_GRAPH_MAPPING_FILE`) read-only;
`DATABRICKS_TABLES` defaults to the sample tables:

```bash
make delta-seed
docker compose --profile databricks up -d --build
```

Then open the `retail` graph in the Arango web UI (<http://localhost:8529>, database
`connector`), run `make delta-mutate`, and watch the next poll apply it.

## How sync works

- **First sync of a table:** a snapshot at the table's latest version, every row
  emitted with `raw._change_type = "snapshot"`. The cursor is then set to that version.
- **Later syncs:** if the table has new versions, the change feed from the cursor + 1 to
  the latest version is read in commit order. `insert` and `update_postimage` rows are
  emitted; `update_preimage` rows are skipped; `delete` rows are counted and logged
  (see below). A table with no new version is not read at all.
- **Cursor / resume:** the cursor is saved only after the whole snapshot or range has
  been emitted, so delivery is at-least-once; deterministic keys make repeats
  harmless. A shutdown mid-table leaves that table's cursor where it was.
- **Fallback to a snapshot:** when the change feed cannot serve the range (history
  vacuumed, the feed turned off and on again) or the cursor is ahead of the table (it
  was recreated or restored), the connector logs `databricks_change_feed_unavailable`
  and takes a fresh snapshot. To force one, delete the table's document from
  `connector_state`; changing `DATABRICKS_WORKSPACE` re-snapshots every table.
- **What is not removed:** deleted rows. Their documents stay; each poll that sees
  deletes logs `databricks_deletes_skipped table=... count=...` at WARNING.
- **Failures:** tables are synced in configured order and one failing table does not
  stop the others (`databricks_table_unusable` for a table without the change feed or
  missing a key column, `databricks_table_sync_failed` otherwise). If every table fails,
  polling backs off exponentially, up to 5 minutes. Each table that had work logs
  `databricks_table_synced table=... mode=snapshot|changes rows=... skipped=...
  deletes_skipped=... version=...`. Rows with a null or unusable primary key are
  skipped and counted in `skipped`.

## Document shape

`scope`: `tenant_id` = `DATABRICKS_WORKSPACE`, `container_id` = the catalog,
`path` = `[schema, table]`.

```json
{
  "_key": "databricks_main.retail.orders_100",
  "schema_version": 4,
  "source": "databricks",
  "kind": "row",
  "source_id": "100",
  "scope": { "tenant_id": "local", "container_id": "main", "path": ["retail", "orders"] },
  "created_at": null,
  "updated_at": "2026-11-23T10:02:11.214000Z",
  "ingested_at": "2026-11-23T10:02:12.001000Z",
  "expires_at": null,
  "payload": {
    "kind": "row",
    "table": "main.retail.orders",
    "primary_key": { "order_id": 100 },
    "columns": {
      "order_id": 100,
      "customer_id": 1,
      "status": "shipped",
      "ordered_at": "2024-05-01T09:30:00+00:00"
    }
  },
  "raw": {
    "_change_type": "update_postimage",
    "_commit_version": 4,
    "_commit_timestamp": "2026-11-23T10:02:11.214000+00:00"
  }
}
```

- `source_id` (and the key's last part) is the primary key: one value as is, a
  composite key joined with `:` (`100:10`). A key that contains `:`, characters Arango
  rejects in keys, or more than 128 characters becomes `h-` and a 40-character SHA-256
  prefix instead.
- `updated_at` is the commit time of the version the row was read at. `created_at` is
  always `null`: Delta does not record when a row was first written.
- Column values are JSON-safe: decimals as exact strings (`"49.90"`), timestamps as
  ISO 8601 in UTC (timestamps without a time zone are taken as UTC), dates as ISO dates,
  binary as base64, NaN and infinities as `null`.

## Retention

Rows have no `created_at`, so `RETENTION_MODE=expiry` measures from `ingested_at`: a row
would be removed `RETENTION_DAYS` after it was last read, even if it is still in the
table. Run this source with `RETENTION_DAYS=0`, as the Compose service does. The graph
sink ignores retention altogether.

## Privacy and security

The connector copies whatever columns the tables hold (all of them for envelopes, those
the mapping names for the graph). Sync only tables whose content may live in Arango, or
leave sensitive columns out of the graph mapping. The `delta` backend needs only read
access to the directory; Compose mounts it read-only. No credentials are needed today.

## Troubleshooting

| Symptom / log event | Cause | Fix |
|---|---|---|
| `databricks_table_unusable ... change data feed is not enabled` | The table lacks `delta.enableChangeDataFeed` | Set the table property (see [Setup](#setup)) |
| `databricks_table_unusable ... primary key column(s) not in table` | A key in `DATABRICKS_TABLES` is misspelled or was dropped | Fix the key columns |
| `databricks_table_sync_failed` with `DeltaTableNotFound` | No table at `{root}/{catalog}/{schema}/{table}` | Check `DATABRICKS_DELTA_ROOT` and the name |
| `databricks_change_feed_unavailable` | History needed for the range is gone, or the table was replaced | Nothing: a snapshot follows. Keep CDF history longer than the connector's downtime |
| `databricks_deletes_skipped` | Rows were deleted in the table | Expected: deletes are not applied yet |
| `skipped=N` in `databricks_table_synced` | Rows with a null, float or empty primary key | Fix the data or the key columns |

## Not supported yet

Each has a `TODO(future):` at its extension point.

- **Real Databricks.** The planned change, and nothing else changes with it:
  1. Add `sources/databricks/databricks_sql.py` with a `DatabricksSqlClient`
     implementing the `DeltaTableClient` protocol (`client.py`) on
     `databricks-sql-connector`.
  2. Widen `DATABRICKS_BACKEND` to `delta | sql` and add `DATABRICKS_HOST`,
     `DATABRICKS_HTTP_PATH` and credentials as secrets.
  3. Branch on the backend in `build_databricks_adapter`.
  4. Run the client contract suite (`tests/test_databricks_client_contract.py`) against
     the new client, gated on credentials.

  The adapter, normalizer, payload, keys, mapping, projection, sink and cursor shape
  stay as they are. Setting `DATABRICKS_WORKSPACE` to the workspace changes the cursor
  keys, which triggers a fresh snapshot against the real tables.

  | Contract method | Expected Databricks SQL |
  |---|---|
  | `describe` | `DESCRIBE HISTORY t LIMIT 1`, `SHOW TBLPROPERTIES t`, `DESCRIBE TABLE t` |
  | `read_snapshot` | `SELECT * FROM t VERSION AS OF v` |
  | `read_changes` | `SELECT * FROM table_changes('t', start, end) ORDER BY _commit_version` |

  That client must absorb the differences so the contract holds: errors for
  out-of-range versions, timestamp time zones, and Arrow-to-Python value conversion.
  Authentication and Unity Catalog permissions come with it.
- **Deletes.** Applying them needs a delete operation on `Envelope` and `Sink`.
- **Tables without the change feed** (watermark polling), views, and Delta roots in
  object storage.
- **Large change ranges** are read into memory and sorted in one go; streaming them
  per version would bound memory.
- **A Platform Link** for this source.
