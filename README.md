# Arango Connector

A Python service that listens to Slack channels in real time (Socket Mode) and stores
every new message in ArangoDB as a JSON document.

This is the MVP of a **universal connector**: sources (Slack today; Gmail, Teams,
SharePoint, … later) are normalized into a source-agnostic [`Envelope`](#document-shape)
and written by a `Sink` (ArangoDB today).

```
Slack (Socket Mode) ──► SlackSocketModeAdapter ──► normalize() ──► Envelope ──► Pipeline ──► ArangoSink ──► connector/messages
                                                                                 │  (stamps expires_at)
                                                                                 └─ on final failure ──► dead_letter.jsonl
```

## What it does

- Ingests new messages, including thread replies, bot messages and thread
  broadcasts, from an allowlist of channel IDs.
- Applies edits over the stored message, so an edited message is not left showing
  its original text. See [Edits](#edits).
- Skips deletes, joins and other subtypes (these are planned for later).
- Writes one document per message into `connector/messages`. Documents are keyed
  `slack_{channel_id}_{ts}`, and writes use upsert semantics, so Slack re-delivering
  a message never creates a duplicate.
- Deletes messages `RETENTION_DAYS` days after they were posted, 30 by default, from
  both ArangoDB and the dead-letter file. See [Retention](#retention).
- Creates the database, collections and indexes on startup (idempotent), including a
  `connector_state` collection where polling sources keep their sync cursors.
- Retries failed writes (3 attempts, exponential backoff). If a write still fails,
  the message is appended to a dead-letter JSONL file instead of being lost.

## Quick start

You need Docker (with Compose), and [`uv`](https://docs.astral.sh/uv/) if you want to
run it outside Docker.

### 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**,
   pick your workspace, and paste the contents of [`slack_manifest.yaml`](slack_manifest.yaml).
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**. Add the
   `connections:write` scope and generate. Copy the token (`xapp-…`). This is
   `SLACK_APP_TOKEN`.
3. **Install App → Install to Workspace**, then copy the **Bot User OAuth Token**
   (`xoxb-…`). This is `SLACK_BOT_TOKEN`.
4. In Slack, invite the bot to each channel you want to capture:
   `/invite @arango-connector`
5. Get each channel's ID: open the channel → click the channel name → the
   **About** tab → the ID (`C…`) is at the bottom. These go in
   `SLACK_CHANNEL_IDS`, comma-separated.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set at least `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`SLACK_CHANNEL_IDS` and `ARANGO_PASSWORD`.

### 3. Run the full stack

```bash
docker compose up --build
```

Post a message in a watched channel. Within a few seconds it appears in the ArangoDB
web UI at <http://localhost:8529> (user `root`, your `ARANGO_PASSWORD`) under
database `connector` → collection `messages`. Or query it with AQL:

```aql
FOR m IN messages SORT m.created_at DESC LIMIT 10 RETURN m
```

### Running locally without Docker for the connector

```bash
docker compose up -d arangodb
uv sync
uv run python -m arango_connector.main
```

Press Ctrl+C to shut down cleanly.

## Configuration

All settings come from environment variables or `.env`.

| Variable            | Required | Default                 | Notes                                    |
|---------------------|----------|-------------------------|------------------------------------------|
| `SOURCE`            | no       | `slack`                 | Source adapter name (see `registry.py`)  |
| `SINK`              | no       | `arango`                | Sink name (see `registry.py`)            |
| `SLACK_BOT_TOKEN`   | slack    | —                       | `xoxb-…`                                 |
| `SLACK_APP_TOKEN`   | slack    | —                       | `xapp-…`, needs `connections:write`      |
| `SLACK_CHANNEL_IDS` | slack    | —                       | Comma-separated, e.g. `C012,C034`        |
| `ARANGO_HOSTS`      | no       | `http://localhost:8529` | Comma-separated for multiple coordinators |
| `ARANGO_USERNAME`   | no       | `root`                  |                                          |
| `ARANGO_PASSWORD`   | yes      | —                       | Also the ArangoDB root password in Compose |
| `ARANGO_DB`         | no       | `connector`             |                                          |
| `ARANGO_COLLECTION` | no       | `messages`              |                                          |
| `STATE_COLLECTION`  | no       | `connector_state`       | Source sync cursors; never expired       |
| `RETENTION_DAYS`    | no       | `30`                    | Delete envelopes this many days after they were posted, in ArangoDB and the dead-letter file; `0` disables |
| `RETENTION_MODE`    | no       | `expiry`                | `expiry` (per-document deadline) or `age`. See [Retention](#retention) |
| `RETENTION_AGE_FIELD` | no     | `created_at`            | `age` mode only: `created_at` or `ingested_at` |
| `DEAD_LETTER_PATH`  | no       | `./dead_letter.jsonl`   | `/data/dead_letter.jsonl` in Docker      |
| `LOG_LEVEL`         | no       | `INFO`                  |                                          |

Variables marked *slack* are required only when `SOURCE=slack`: each source loads its
own settings class, so a deployment only needs the variables of the source it runs.
The connector exits immediately with a clear message if a required variable is
missing. Tokens and passwords are held as `SecretStr` and are never logged.

> **Note:** `ARANGO_ROOT_PASSWORD` is only applied the first time the ArangoDB
> volume is initialized. If you change `ARANGO_PASSWORD` later, either change the
> password in ArangoDB too or reset the volume with `docker compose down -v`
> (this deletes all stored data).

## Retention

Envelopes are deleted `RETENTION_DAYS` days after they were **posted**, 30 by default.
Set `RETENTION_DAYS=0` to keep them forever. Deletion is enforced by an ArangoDB TTL
index, and the same rule is applied to the dead-letter file.

There are two modes, chosen with `RETENTION_MODE`. They mirror MongoDB's two kinds of
TTL index.

| | `expiry` (default) | `age` |
|---|---|---|
| What each document stores | its own deadline, `expires_at` | nothing extra |
| TTL index | `expires_at`, `expireAfter: 0` | `RETENTION_AGE_FIELD`, `expireAfter: RETENTION_DAYS` |
| Counted from | `created_at`, else `ingested_at` | `RETENTION_AGE_FIELD` (`created_at` or `ingested_at`) |
| Changing `RETENTION_DAYS` | applies to **new** documents; run `reconcile-expiry` for existing ones | applies to every document immediately |
| Documents without the date | still expire (fall back to `ingested_at`) | never expire |
| Per-document deadlines (per-source windows, legal holds) | possible | not possible |

**`expiry`** fixes each document's deadline when it is written, like MongoDB's
`expireAt` pattern, DynamoDB TTL or Cassandra's per-write TTLs. The connector never
rewrites stored documents on its own, so restarts cost nothing however large the
collection is. To apply a new window to documents already stored, run the
[maintenance command](#applying-a-new-window-to-stored-documents).

**`age`** keeps the window in the index, so a change applies everywhere at once, at the
cost of one global window per collection.

Startup logs `arango_ttl_index_created` at WARNING whenever it creates or changes the
index. In `expiry` mode it also logs `arango_existing_expiry_unchanged`, as a reminder
that stored documents keep their old deadlines. Switching mode is safe: ArangoDB allows
only one TTL index per collection, so the connector drops the old one first.

ArangoDB sweeps expired documents in the background rather than at the exact moment
they expire, so treat the window as "removed within `RETENTION_DAYS` days".

### Applying a new window to stored documents

```bash
uv run arango-connector-reconcile-expiry --dry-run      # count what would change
uv run arango-connector-reconcile-expiry                # apply RETENTION_DAYS to all
uv run arango-connector-reconcile-expiry --only-missing # only documents with no expires_at
```

It reads the same `ARANGO_*` and `RETENTION_*` settings as the connector, and only runs
in `expiry` mode. It walks the collection in primary-key order in batches
(`--batch-size`, default 5000). Each batch is a bounded index range read that fetches
only the date fields and rewrites only the documents whose `expires_at` differs, so
memory and transaction size stay flat on large collections. Running it twice is safe;
the second run finds nothing to do. In Docker:
`docker compose run --rm connector arango-connector-reconcile-expiry`.

> **Upgrading from schema version 3:** documents written before this version have no
> `expires_at`, so in `expiry` mode they are **never deleted** until you run
> `arango-connector-reconcile-expiry --only-missing` once. That run gives them a
> deadline based on when they were posted, and ArangoDB then deletes any that are
> already past it. This is not reversible.

### Dead-letter file

The TTL index cannot reach the dead-letter file, so the connector prunes it once at
startup and hourly thereafter using the same policy. In `expiry` mode it uses each
line's `expires_at`; lines written before `expires_at` existed fall back to `created_at`
(else `ingested_at`) plus the current window. In `age` mode it uses
`RETENTION_AGE_FIELD`. Pruning rewrites the file through a temporary file, so an
interrupted prune cannot truncate it, and a line too corrupt to parse is kept rather
than discarded (with a `dead_letter_prune_unreadable` warning) so a bad write surfaces
instead of vanishing.

One caveat worth repeating: retention is time-based, so it is not a substitute for
honouring a deletion. A message you delete in Slack still sits in ArangoDB until its
window elapses.

## Document shape

Every source produces an `Envelope` (schema version 4). The top level is the same for all
sources; `payload` is typed by the envelope's **kind**, so sources of the same kind share
a shape (Slack, Teams and Discord all produce `message` payloads).

```json
{
  "_key": "slack_C0123ABC_1716500000.123456",
  "schema_version": 4,
  "source": "slack",
  "kind": "message",
  "source_id": "1716500000.123456",
  "scope": { "tenant_id": "T0123ABC", "container_id": "C0123ABC", "path": [] },
  "created_at": "2024-05-23T21:33:20.123456Z",
  "updated_at": null,
  "ingested_at": "2026-09-23T19:30:00.000000Z",
  "expires_at": "2024-06-22T21:33:20.123456Z",
  "payload": {
    "kind": "message",
    "author_id": "U0123ABC",
    "author_name": "Ada Lovelace",
    "author_type": "user",
    "text": "Hello!",
    "thread_id": null,
    "parent_id": null
  },
  "raw": { "...": "the original Slack event, unmodified" }
}
```

Top-level fields:

- `source` is the connector that produced the envelope, `kind` the entity kind
  (`message` today), and `source_id` the item's own id in its source.
- `scope` says where the envelope lives, outermost first:

  | source  | `tenant_id` | `container_id` | `path` |
  |---------|-------------|----------------|--------|
  | slack   | team        | channel        | `[]`   |

- `updated_at` is when the content last changed in the source (for Slack, the most
  recent edit), or `null`.
- `expires_at` is when retention deletes the document. It is `null` when retention is
  off or `RETENTION_MODE=age`.

Slack `message` payloads:

- `author_type` is `"bot"` for `bot_message` events and for app-bot posts that
  carry a `bot_id`; `author_id` is then the `bot_id`.
- `author_name` is the Slack user's `real_name`, looked up with `users.info` (needs
  the `users:read` scope) and cached per user until the connector restarts, so a
  rename is picked up only after a restart. For bots it is the bot's `username` or
  `bot_profile.name`. It is `null` if the lookup fails; the message is still stored.
- `thread_id` is the thread's `ts` for any message in a thread (including its root
  once Slack reports it as one), and `null` otherwise.
- `parent_id` is the parent's `ts` for thread replies, and `null` for top-level
  messages and thread roots.

Persistent indexes are created on `[scope.container_id, created_at]`,
`[source, scope.tenant_id, scope.container_id]` and `[kind]`.

### Upgrading from schema version 3

Version 4 renamed and nested fields: `channel_id` → `scope.container_id`,
`workspace_id` → `scope.tenant_id`, `source_ts` → `source_id`, `edited_at` →
`updated_at`, `thread_parent` → `payload.parent_id`, and `text` / `author_*` moved under
`payload`. Existing documents are not migrated; they keep the old shape until retention
removes them, so queries that span the upgrade should filter on `schema_version`.

## Edits

Editing a message in Slack overwrites the stored document. The edit is keyed on the
original message's `ts`, so it lands on the existing document rather than creating a
new one, and `updated_at` is set.

Because the whole document is replaced, content the edit removed does not survive
anywhere — including inside `raw`, which holds only the edited message. Slack's
`message_changed` payload also carries a `previous_message` with the pre-edit text;
that is deliberately discarded rather than stored.

Deletes are still not handled: see [Not yet supported](#not-yet-supported).

## Dead-letter file

If a write still fails after retries (for example, because ArangoDB is unreachable),
the document is appended as one JSON line to `DEAD_LETTER_PATH` and an
`envelope_write_failed` error is logged. The listener keeps running. Because writes
are idempotent, dead-lettered documents can be safely replayed later. There is no
replay tool yet.

This file holds full message content, so it is covered by `RETENTION_DAYS` too. See
[Retention](#retention).

## Development

The Makefile wraps the common tasks; run `make` (or `make help`) to list them.

```bash
make install            # uv sync
make check              # lint, format check, mypy, unit tests (no ArangoDB needed)
make test-integration   # starts a disposable ArangoDB on :18529, then runs integration tests
make test-db-down       # remove that database when you're done
```

Container targets work with Docker or Podman (auto-detected; override with
`CONTAINER=podman`). `make up` / `down` / `logs` need a Compose provider (Docker
Compose, or `podman compose` with one installed).

Without make:

```bash
uv sync
uv run pytest                  # unit tests (no ArangoDB needed)
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Integration tests need a running ArangoDB. They read `ARANGO_*` from the environment
or `.env`, and use a throwaway database that is dropped afterwards:

```bash
docker compose up -d arangodb
uv run pytest -m integration
```

### Layout

```
src/arango_connector/
├── main.py            # entrypoint: config → registry → pipeline, signal handling
├── config.py          # CoreSettings, shared settings base and load_settings()
├── envelope.py          # source-agnostic Envelope, Scope, make_key()
├── payloads.py        # typed payloads per entity kind (MessagePayload, ...)
├── pipeline.py        # source → sink, retention stamping, dead-lettering
├── retention.py       # RetentionPolicy: expiry / age modes
├── maintenance.py     # arango-connector-reconcile-expiry (operator command)
├── registry.py        # name → factory maps (future plugin point)
├── arango_common.py   # connection, retry policy shared by sink and state store
├── state/
│   ├── base.py        # StateStore ABC (source cursors)
│   ├── arango.py      # ArangoStateStore
│   └── memory.py      # InMemoryStateStore (tests)
├── sources/
│   ├── base.py        # SourceAdapter ABC
│   ├── polling.py     # PollingSourceAdapter: interval loop, backoff, clean stop
│   └── slack/
│       ├── config.py  # SlackSettings
│       ├── adapter.py # SlackSocketModeAdapter (Bolt, Socket Mode)
│       └── normalize.py  # pure: Slack event → Envelope | None
└── sinks/
    ├── base.py        # Sink ABC
    └── arango.py      # ArangoSink
```

### Adding a new source or sink

1. **Pick a payload kind.** Reuse an existing one in `payloads.py` if your source
   produces the same kind of thing (a chat source is a `message`); otherwise add a
   `Payload` subclass with a new `kind`.
2. **Add a settings class** next to your source, subclassing `ConnectorSettings`, for
   the environment variables only your source needs.
3. **Write the adapter.** Implement `SourceAdapter` (`start(emit)` blocks and calls
   `emit(envelope)`; `stop()` unblocks it), or subclass `PollingSourceAdapter` and
   implement `poll_once(emit)` for an API you poll. Keep the mapping into `Envelope` in
   a pure `normalize` function like the Slack one, and build keys with `make_key()`
   so re-delivery dedupes.
4. **Keep cursors in the `StateStore`** your factory is given (`get`/`put` small JSON
   values under your source's name), so a restart resumes where it left off.
5. **Register a factory** in `registry.py` that loads your settings with
   `load_settings(YourSettings)`, and select it with `SOURCE=`.

A sink implements `Sink` (`setup`, `write(list[Envelope])`, `close`) and is registered
in `SINKS`.

## Not yet supported

These are deliberately out of scope for the MVP. Look for `TODO(future):` comments at
the extension points.

- Deletes. A message deleted in Slack stays in ArangoDB until `RETENTION_DAYS`
  elapses. Handling them needs a delete operation on `Envelope` and `Sink`, which
  today only express "write this document".
- Historical backfill (`conversations.history`), reactions and files.
- Graph modeling (users/channels collections, edges) and ArangoSearch.
- Queues, batching, and a dead-letter collection.
- Other sources (Gmail is in progress), OAuth install flow, multi-workspace support,
  and an admin UI.
- Running more than one source per process; run one container per source for now.
