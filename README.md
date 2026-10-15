# Arango Connector

A Python service that streams messages from Slack channels (in real time, over Socket
Mode), emails from Gmail mailboxes and issues and comments from Jira Cloud projects
(both polled incrementally) into Arango as JSON documents.

This is the MVP of a **universal connector**: sources (Slack, Gmail and Jira today; Teams,
SharePoint, … later) are normalized into a source-agnostic [`Envelope`](#document-shape)
and written by a `Sink` (Arango today). Each connector process runs one source.

```
Slack (Socket Mode) ──► SlackSocketModeAdapter ──► normalize() ──┐
Gmail (history API) ──► GmailPollingAdapter ────► normalize() ──┼─► Envelope ──► Pipeline ──► ArangoSink ──► connector/<collection>
Jira (JQL search) ────► JiraPollingAdapter ─────► normalize() ──┘                  │  (stamps expires_at)
        ▲ cursor per mailbox / project in connector_state                           └─ on final failure ──► dead_letter.jsonl
```

## Sources

Each source has a page with setup, configuration, how sync works, its document shape,
privacy notes and troubleshooting.

<!-- BEGIN GENERATED: catalogue -->
| Source | `SOURCE=` | Kinds | Delivery | What it ingests | Docs |
|---|---|---|---|---|---|
| Slack | `slack` | `message` | push (Socket Mode) | Messages, thread replies and edits from allowlisted channels, in real time. | [Slack docs](docs/sources/slack.md) |
| Gmail | `gmail` | `email`, `attachment` | poll (Gmail history API) | Email from selected mailboxes and labels, incrementally, with optional attachments and text extraction. | [Gmail docs](docs/sources/gmail.md) |
| Jira | `jira` | `issue`, `comment` | poll (incremental JQL) | Issues and comments from selected Jira Cloud projects, incrementally. | [Jira docs](docs/sources/jira.md) |
<!-- END GENERATED: catalogue -->

## What it does

Whatever the source, the connector:

- Writes one document per item, keyed deterministically per source, with upsert
  semantics, so a re-delivered or re-read item never creates a duplicate.
- Creates the database, collections and indexes on startup (idempotent), including a
  `connector_state` collection where polling sources keep their sync cursors.
- Deletes documents `RETENTION_DAYS` days after they were posted, 30 by default, from
  both Arango and the dead-letter file. See [Retention](#retention).
- Retries failed writes (3 attempts, exponential backoff). If a write still fails, the
  document is appended to a dead-letter JSONL file instead of being lost.

## Quick start

You need Docker (with Compose), and [`uv`](https://docs.astral.sh/uv/) if you want to
run it outside Docker.

1. Set up credentials for your source by following its page under
   [Sources](#sources).
2. Configure:

   ```bash
   cp .env.example .env
   ```

   Set `ARANGO_PASSWORD` and the source's variables.
3. Run Arango and the connector for each source you want, one profile per source:

   ```bash
   docker compose --profile slack up --build    # the Slack connector service
   docker compose --profile gmail up --build    # the Gmail connector service
   docker compose --profile jira up --build     # the Jira connector service
   ```

   Profiles combine, e.g. `--profile slack --profile jira`. Each service sets its own
   `SOURCE`; the `SOURCE` in `.env` only applies to local runs (`make run`).

4. Open the Arango web UI at <http://localhost:8529> (user `root`, your
   `ARANGO_PASSWORD`). Choose database `connector` on the login screen; it appears
   after the connector's first start.

To run the connector outside Docker: `docker compose up -d arangodb`, then
`uv run python -m arango_connector_standalone.main`. Press Ctrl+C to shut down cleanly.

## Configuration

All settings come from environment variables or `.env`.

<!-- BEGIN GENERATED: config:core -->
| Variable | Default | Values | Description |
|---|---|---|---|
| `SOURCE` | `slack` | text | Source adapter to run (one source per process). |
| `SINK` | `arango` | text | Sink to write to. |
| `ARANGO_HOSTS` | `http://localhost:8529` | comma-separated | Arango endpoint(s), comma-separated for multiple coordinators. |
| `ARANGO_USERNAME` | `root` | text | Arango user. |
| `ARANGO_PASSWORD` | **required** | secret | Arango password. Also the root password of the Compose Arango. |
| `ARANGO_DB` | `connector` | text | Database, created on first start if missing. |
| `ARANGO_COLLECTION` | `messages` | text | Collection envelopes are written to. |
| `STATE_COLLECTION` | `connector_state` | text | Collection holding source sync cursors (e.g. Gmail historyId). Never subject to retention. |
| `RETENTION_DAYS` | `30` | integer ≥ 0, ≤ 36500 | Delete envelopes this many days after they were posted, in Arango and the dead-letter file. `0` disables retention. |
| `RETENTION_MODE` | `expiry` | `expiry` \| `age` | `expiry`: each document stores its own `expires_at`, fixed at write time. `age`: documents expire `RETENTION_DAYS` after `RETENTION_AGE_FIELD`. |
| `RETENTION_AGE_FIELD` | `created_at` | `created_at` \| `ingested_at` | `age` mode only: the date retention counts from. |
| `DEAD_LETTER_PATH` | `./dead_letter.jsonl` | path | JSONL file for envelopes that still fail after retries (`/data/dead_letter.jsonl` in Docker). |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` | Python log level (case-insensitive). |
<!-- END GENERATED: config:core -->

<!-- BEGIN GENERATED: source-settings -->
- `SLACK_*`: required only when `SOURCE=slack`. See [Slack configuration](docs/sources/slack.md#configuration).
- `GMAIL_*`: required only when `SOURCE=gmail`. See [Gmail configuration](docs/sources/gmail.md#configuration).
- `JIRA_*`: required only when `SOURCE=jira`. See [Jira configuration](docs/sources/jira.md#configuration).
<!-- END GENERATED: source-settings -->

Each source loads its own settings class, so a deployment only needs the variables of
the source it runs.
The connector exits immediately with a clear message if a required variable is
missing. Tokens and passwords are held as `SecretStr` and are never logged.

> **Note:** `ARANGO_ROOT_PASSWORD` is only applied the first time the Arango
> volume is initialized. If you change `ARANGO_PASSWORD` later, either change the
> password in Arango too or reset the volume with `docker compose down -v`
> (this deletes all stored data).

## Retention

Envelopes are deleted `RETENTION_DAYS` days after they were **posted**, 30 by default.
Set `RETENTION_DAYS=0` to keep them forever. Deletion is enforced by an Arango TTL
index, and the same rule is applied to the dead-letter file.

Because the window counts from creation, it suits sources whose items are not edited
long after they are posted. Jira issues are, so the Jira Compose service runs with
retention off; see [Jira retention](docs/sources/jira.md#retention).

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
that stored documents keep their old deadlines. Switching mode is safe: Arango allows
only one TTL index per collection, so the connector drops the old one first.

Arango sweeps expired documents in the background rather than at the exact moment
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
> deadline based on when they were posted, and Arango then deletes any that are
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
honouring a deletion. A message you delete in Slack still sits in Arango until its
window elapses.

## Document shape

Every source produces an `Envelope` (schema version 4). The top level is the same for all
sources; `payload` is typed by the envelope's **kind**, so sources of the same kind share
a shape (Slack, Teams and Discord would all produce `message` payloads). Each source's
page shows a full example and its payload fields.

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
  "payload": { "kind": "message", "...": "fields for this kind" },
  "raw": { "...": "the original source item" }
}
```

- `source` is the connector that produced the envelope, `kind` the entity kind, and
  `source_id` the item's own id in its source.
- `scope` says where the envelope lives, outermost first:

  | source | `tenant_id` | `container_id` | `path` |
  |---|---|---|---|
  | [slack](docs/sources/slack.md#document-shape) | team | channel | `[]` |
  | [gmail](docs/sources/gmail.md#document-shape) | mailbox domain | mailbox key | `[]` |
  | [jira](docs/sources/jira.md#document-shape) | site host | project id | `[]` (issue), `[issue id]` (comment) |

- `updated_at` is when the content last changed in the source (a Slack edit, a Gmail
  label change, a Jira issue's or comment's `updated`), or `null`.
- `expires_at` is when retention deletes the document. It is `null` when retention is
  off or `RETENTION_MODE=age`.

| Kind | Payload model | Sources |
|---|---|---|
| `message` | `MessagePayload`: author, text, thread | Slack |
| `email` | `EmailPayload`: subject, addresses, threading headers, labels, body | Gmail |
| `attachment` | `AttachmentPayload`: filename, type, size, optional file and extracted text | Gmail (`GMAIL_ATTACHMENTS`) |
| `issue` | `IssuePayload`: key, summary, description, status, type, people, labels, parent | Jira |
| `comment` | `CommentPayload`: author, text, parent item, visibility | Jira |

Persistent indexes are created on `[scope.container_id, created_at]`,
`[source, scope.tenant_id, scope.container_id]` and `[kind]`.

### Upgrading from schema version 3

Version 4 renamed and nested fields: `channel_id` → `scope.container_id`,
`workspace_id` → `scope.tenant_id`, `source_ts` → `source_id`, `edited_at` →
`updated_at`, `thread_parent` → `payload.parent_id`, and `text` / `author_*` moved under
`payload`. Existing documents are not migrated; they keep the old shape until retention
removes them, so queries that span the upgrade should filter on `schema_version`.

## Dead-letter file

If a write still fails after retries (for example, because Arango is unreachable),
the document is appended as one JSON line to `DEAD_LETTER_PATH` and an
`envelope_write_failed` error is logged. The source keeps running. Because writes
are idempotent, dead-lettered documents can be safely replayed later. There is no
replay tool yet.

This file holds full item content, so it is covered by `RETENTION_DAYS` too. See
[Retention](#retention).

## Development

The Makefile wraps the common tasks; run `make` (or `make help`) to list them.

```bash
make install            # uv sync
make check              # lint, format check, mypy, unit tests (no Arango needed)
make test-integration   # starts a disposable Arango on :18529, then runs integration tests
make test-db-down       # remove that database when you're done
make docs-gen           # regenerate config tables / source catalogue after changing settings
```

CI (`.github/workflows/ci.yml`) runs `make check` and the integration tests (against an
Arango service container) on every PR and push to `main`. Docs are published to the
[wiki](https://github.com/arango-solutions/arango-universal-connector/wiki) on merge; see
[how the docs are built](docs/README.md#how-these-docs-are-built).

Container targets work with Docker or Podman (auto-detected; override with
`CONTAINER=podman`). `make up` / `down` / `logs` need a Compose provider (Docker
Compose, or `podman compose` with one installed).

Without make:

```bash
uv sync
uv run pytest                  # unit tests (no Arango needed)
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Integration tests need a running Arango. They read `ARANGO_*` from the environment
or `.env`, and use a throwaway database that is dropped afterwards:

```bash
docker compose up -d arangodb
uv run pytest -m integration
```

### Layout

The repo is a uv workspace. Source logic lives once in **core**; each runtime builds on
it. See [the Links design](docs/design/links.md) for why.

```
packages/
├── core/src/arango_connector/              arango-connector-core: shared by every runtime
│   ├── envelope.py        # source-agnostic Envelope, Scope, make_key()
│   ├── payloads.py        # typed payloads per entity kind (MessagePayload, ...)
│   ├── extract.py         # text from files (PDF, DOCX, HTML, text)
│   ├── config.py          # settings base, load_settings(), ConfigError
│   ├── catalog.py         # SOURCE_INFO: what each source is (docs, tooling)
│   └── sources/
│       ├── slack/         # config.py (SlackSettings), normalize.py
│       ├── gmail/         # config.py, auth.py (+ OAuth helper), client.py, normalize.py
│       └── jira/          # config.py, client.py, adf.py, normalize.py
└── standalone/src/arango_connector_standalone/   arango-connector: the continuous service
    ├── main.py            # entrypoint: config → registry → pipeline, signal handling
    ├── config.py          # CoreSettings: ArangoDB, retention, dead-letter, logging
    ├── registry.py        # name → factory maps
    ├── pipeline.py        # source → sink, retention stamping, dead-lettering
    ├── retention.py       # RetentionPolicy: expiry / age modes
    ├── maintenance.py     # arango-connector-reconcile-expiry (operator command)
    ├── arango_common.py   # connection, retry policy shared by sink and state store
    ├── state/             # StateStore: base, arango, memory (tests)
    ├── sinks/             # Sink: base, arango
    └── sources/
        ├── base.py        # SourceAdapter ABC
        ├── polling.py     # PollingSourceAdapter: interval loop, backoff, clean stop
        └── slack/ gmail/ jira/   # adapter.py: Socket Mode listener / pollers with cursors
tools/doctools.py          # docs generation and checks, for every package
dev/platform.sh            # local ArangoDB Platform for testing Links
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
5. **Register a factory** in the standalone `registry.py` that loads your settings with
   `load_settings(YourSettings)`, and select it with `SOURCE=`.
6. **Document it.** Give every setting a `description`, and add the source to
   `SOURCE_INFO` in core's `catalog.py`. Copy [`docs/sources/_template.md`](docs/sources/_template.md)
   to `docs/sources/<name>.md`, fill in every section (setup, sync, document shape,
   privacy, troubleshooting), add your variables to `.env.example`, and run
   `make docs-gen`. That fills in the configuration table and adds the source to the
   catalogues. See [how the docs are built](docs/README.md#how-these-docs-are-built).

A sink implements `Sink` (`setup`, `write(list[Envelope])`, `close`) and is registered
in `SINKS`.

## Not yet supported

These are deliberately out of scope for the MVP. Look for `TODO(future):` comments at
the extension points, and the "Not supported yet" section of each source's page.

- Deletes. An item deleted in its source stays in Arango until `RETENTION_DAYS`
  elapses. Handling them needs a delete operation on `Envelope` and `Sink`, which
  today only express "write this document".
- Graph modeling (users/channels collections, edges) and ArangoSearch.
- Queues, batching, and a dead-letter collection.
- More sources: IMAP (SE-136), webhook / HTTP ingest (SE-137), Teams, SharePoint, …
- Running more than one source per process (SE-132); run one container per source for
  now.
