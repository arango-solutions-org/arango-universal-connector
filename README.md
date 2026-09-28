# Arango Connector

A Python service that listens to Slack channels in real time (Socket Mode) and stores
every new message in ArangoDB as a JSON document.

This is the MVP of a **universal connector**: sources (Slack today; Discord, Teams, …
later) are normalized into a source-agnostic `Envelope` and written by a `Sink`
(ArangoDB today).

```
Slack (Socket Mode) ──► SlackSocketModeAdapter ──► normalize() ──► Envelope ──► Pipeline ──► ArangoSink ──► connector/messages
                                                                                   │
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
- Creates the database, collection and indexes on startup (idempotent).
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
| `SLACK_BOT_TOKEN`   | yes      | —                       | `xoxb-…`                                 |
| `SLACK_APP_TOKEN`   | yes      | —                       | `xapp-…`, needs `connections:write`      |
| `SLACK_CHANNEL_IDS` | yes      | —                       | Comma-separated, e.g. `C012,C034`        |
| `ARANGO_HOSTS`      | no       | `http://localhost:8529` | Comma-separated for multiple coordinators |
| `ARANGO_USERNAME`   | no       | `root`                  |                                          |
| `ARANGO_PASSWORD`   | yes      | —                       | Also the ArangoDB root password in Compose |
| `ARANGO_DB`         | no       | `connector`             |                                          |
| `ARANGO_COLLECTION` | no       | `messages`              |                                          |
| `RETENTION_DAYS`    | no       | `30`                    | Delete messages this many days after they were posted, in ArangoDB and the dead-letter file; `0` disables |
| `DEAD_LETTER_PATH`  | no       | `./dead_letter.jsonl`   | `/data/dead_letter.jsonl` in Docker      |
| `LOG_LEVEL`         | no       | `INFO`                  |                                          |

The connector exits immediately with a clear message if a required variable is
missing. Tokens and passwords are held as `SecretStr` and are never logged.

> **Note:** `ARANGO_ROOT_PASSWORD` is only applied the first time the ArangoDB
> volume is initialized. If you change `ARANGO_PASSWORD` later, either change the
> password in ArangoDB too or reset the volume with `docker compose down -v`
> (this deletes all stored data).

## Retention

Messages are deleted `RETENTION_DAYS` days after they were **posted** (not after they
were ingested), enforced by an ArangoDB TTL index on `created_at`. The default is 30
days. Set `RETENTION_DAYS=0` to keep messages forever.

> **Warning:** retention is on by default. If you are upgrading an existing deployment,
> the first start after this change deletes everything already older than
> `RETENTION_DAYS`, in both the collection and the dead-letter file. This is not
> reversible. Set `RETENTION_DAYS=0` before starting if you want to keep that data.

Startup logs `arango_ttl_index_created` at WARNING whenever it creates or changes the
index, so a surprising retention window is visible in the logs.

Deletion is swept by a background thread rather than happening at the exact moment a
message ages out, so treat the window as "removed within `RETENTION_DAYS` days".

Changing `RETENTION_DAYS` is safe: ArangoDB allows only one TTL index per collection,
so the connector drops the old index and creates the new one on startup. An unchanged
window leaves the existing index alone.

The same window applies to the dead-letter file, which the TTL index cannot reach.
The connector prunes it once at startup and hourly thereafter, dropping entries whose
`created_at` is past the window. Pruning rewrites the file through a temporary file, so
an interrupted prune cannot truncate it, and a line too corrupt to parse is kept rather
than discarded (with a `dead_letter_prune_unreadable` warning) so a bad write surfaces
instead of vanishing.

One caveat worth repeating: retention is time-based, so it is not a substitute for
honouring a deletion. A message you delete in Slack still sits in ArangoDB until its
window elapses.

## Document shape

```json
{
  "_key": "slack_C0123ABC_1716500000.123456",
  "schema_version": 3,
  "source": "slack",
  "workspace_id": "T0123ABC",
  "channel_id": "C0123ABC",
  "author_id": "U0123ABC",
  "author_name": "Ada Lovelace",
  "author_type": "user",
  "text": "Hello!",
  "source_ts": "1716500000.123456",
  "created_at": "2024-05-23T21:33:20.123456Z",
  "edited_at": null,
  "thread_parent": null,
  "ingested_at": "2026-09-23T19:30:00.000000Z",
  "raw": { "...": "the original Slack event, unmodified" }
}
```

- `author_type` is `"bot"` for `bot_message` events and for app-bot posts that
  carry a `bot_id`; `author_id` is then the `bot_id`.
- `author_name` is the Slack user's `real_name`, looked up with `users.info` (needs
  the `users:read` scope) and cached per user until the connector restarts, so a
  rename is picked up only after a restart. For bots it is the bot's `username` or
  `bot_profile.name`. It is `null` if the lookup fails; the message is still stored.
- `thread_parent` is the parent's `ts` for thread replies, and `null` for top-level
  messages (including a thread's root message).
- `edited_at` is the time of the most recent edit, or `null` if the message has never
  been edited.

Persistent indexes are created on `[channel_id, created_at]` and
`[source, workspace_id, channel_id]`.

## Edits

Editing a message in Slack overwrites the stored document. The edit is keyed on the
original message's `ts`, so it lands on the existing document rather than creating a
new one, and `edited_at` is set.

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
├── config.py          # pydantic-settings Settings
├── envelope.py        # source-agnostic Envelope
├── pipeline.py        # source → sink, dead-lettering
├── registry.py        # name → factory maps (future plugin point)
├── sources/
│   ├── base.py        # SourceAdapter ABC
│   └── slack/
│       ├── adapter.py # SlackSocketModeAdapter (Bolt, Socket Mode)
│       └── normalize.py  # pure: Slack event → Envelope | None
└── sinks/
    ├── base.py        # Sink ABC
    └── arango.py      # ArangoSink
```

### Adding a new source or sink

1. Implement `SourceAdapter` (`start(emit)` blocks and calls `emit(envelope)`;
   `stop()` unblocks it) or `Sink` (`setup`, `write(list[Envelope])`, `close`).
2. Map your source's native events into `Envelope`, ideally with a pure
   `normalize` function like the Slack one.
3. Register a factory in `registry.py` and select it with `SOURCE=` / `SINK=`.

## Not yet supported

These are deliberately out of scope for the MVP. Look for `TODO(future):` comments at
the extension points.

- Deletes. A message deleted in Slack stays in ArangoDB until `RETENTION_DAYS`
  elapses. Handling them needs a delete operation on `Envelope` and `Sink`, which
  today only express "write this document".
- Historical backfill (`conversations.history`), reactions and files.
- Graph modeling (users/channels collections, edges) and ArangoSearch.
- Queues, batching, and a dead-letter collection.
- Other sources, OAuth install flow, multi-workspace support, and an admin UI.
