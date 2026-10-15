# Slack source

`SOURCE=slack`. Listens to an allowlist of Slack channels in real time over Socket Mode
and writes every new message, thread reply and edit to Arango within seconds.

| | |
|---|---|
| Kind | `message` |
| Delivery | push (Socket Mode, no public URL needed) |
| Keys | `slack_{channel_id}_{ts}`, deterministic, so re-delivery overwrites |
| Cursor | none; Slack pushes events as they happen |
| Access | Bot scopes `channels:history`, `groups:history`, `channels:read`, `groups:read`, `users:read`; app token with `connections:write` (see `slack_manifest.yaml`) |

## Setup

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**,
   pick your workspace, and paste the contents of
   [`slack_manifest.yaml`](../../slack_manifest.yaml).
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**. Add the
   `connections:write` scope and generate. Copy the token (`xapp-…`). This is
   `SLACK_APP_TOKEN`.
3. **Install App → Install to Workspace**, then copy the **Bot User OAuth Token**
   (`xoxb-…`). This is `SLACK_BOT_TOKEN`.
4. In Slack, invite the bot to each channel you want to capture:
   `/invite @arango-connector`
5. Get each channel's ID: open the channel → click the channel name → the
   **About** tab → the ID (`C…`) is at the bottom. These go in `SLACK_CHANNEL_IDS`,
   comma-separated.

## Configuration

<!-- BEGIN GENERATED: config:slack -->
| Variable | Default | Values | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | **required** | secret | Bot User OAuth Token (`xoxb-…`). |
| `SLACK_APP_TOKEN` | **required** | secret | App-level token (`xapp-…`) with the `connections:write` scope. |
| `SLACK_CHANNEL_IDS` | **required** | comma-separated | Channel IDs to watch, comma-separated, e.g. `C012,C034`. |
<!-- END GENERATED: config:slack -->

Messages go to `ARANGO_COLLECTION` (default `messages`).

## Running

```bash
cp .env.example .env      # set SLACK_* and ARANGO_PASSWORD
docker compose --profile slack up --build
```

Post a message in a watched channel. Within a few seconds it appears in the Arango web
UI at <http://localhost:8529> (user `root`, your `ARANGO_PASSWORD`) under database
`connector` → collection `messages`:

```aql
FOR m IN messages SORT m.created_at DESC LIMIT 10 RETURN m
```

To run the connector outside Docker: `docker compose up -d arangodb`, then
`uv run python -m arango_connector.main`. Press Ctrl+C to shut down cleanly.

## How sync works

- **What is read:** new messages, thread replies, bot messages, thread broadcasts and
  messages with uploads (their text; the file itself is not stored) in the watched
  channels. Other channels are ignored.
- **Edits** overwrite the stored document. An edit is keyed on the original message's
  `ts`, so it lands on the existing document, and `updated_at` is set. Because the
  whole document is replaced, content the edit removed does not survive anywhere,
  including in `raw`, which holds only the edited message. Slack's `previous_message`
  (the pre-edit text) is deliberately discarded.
- **What is not removed:** deletes, joins, leaves and other subtypes are skipped. A
  deleted message stays stored until retention removes it.
- **Gaps:** Socket Mode only delivers live events, so messages posted while the
  connector is down are not picked up (backfill is not supported yet).
- **Failures:** a handler error is logged (`slack_event_handler_failed`) and never stops
  the listener. Write failures go to the dead-letter file.

## Document shape

`scope`: `tenant_id` = team ID, `container_id` = channel ID, `path` = `[]`.

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

- `author_type` is `"bot"` for `bot_message` events and for app-bot posts that carry a
  `bot_id`; `author_id` is then the `bot_id`.
- `author_name` is the user's `real_name`, looked up with `users.info` and cached per
  user until the connector restarts, so a rename is picked up only after a restart. For
  bots it is the bot's `username` or `bot_profile.name`. It is `null` if the lookup
  fails; the message is still stored.
- `thread_id` is the thread's `ts` for any message in a thread (including its root once
  Slack reports it as one), and `null` otherwise.
- `parent_id` is the parent's `ts` for thread replies, and `null` for top-level
  messages and thread roots.
- `updated_at` is the time of the most recent edit.

## Privacy and security

Messages are stored with full text and the original event in `raw`. Tokens are held as
secrets and never logged. Use a dedicated Arango user with access to the messages
collection only for anyone reading the data.

## Troubleshooting

| Symptom / log event | Cause | Fix |
|---|---|---|
| No documents, `slack_event_ignored reason=unwatched_channel` at DEBUG | Channel not in `SLACK_CHANNEL_IDS` | Add its `C…` ID |
| No events at all | Bot not in the channel | `/invite @arango-connector` |
| `slack_user_lookup_failed error=missing_scope` | App lacks `users:read` | Reinstall from the manifest |
| Exits with `SLACK_…: is required but not set` | Missing variable | Set it in `.env` |

## Not supported yet

Deletes, historical backfill (`conversations.history`), reactions and files, an OAuth
install flow and multi-workspace support. See the `TODO(future)` comments in
`sources/slack/`.
