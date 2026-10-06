# Gmail source

`SOURCE=gmail`. Polls each mailbox in `GMAIL_MAILBOXES` every
`GMAIL_POLL_INTERVAL_SECONDS` (60 by default) through the Gmail API's history feed and
writes one document per email, so new mail lands within about a minute.

| | |
|---|---|
| Kind | `email` |
| Delivery | poll, incremental via Gmail `historyId` |
| Keys | `gmail_{mailbox key}_{message id}`, deterministic, so a re-read overwrites |
| Cursor | per mailbox in `connector_state`: `{history_id, last_synced_at}` |
| Access | `https://www.googleapis.com/auth/gmail.readonly` only; it can never send, change or delete mail |

## Setup

Every program that reads Gmail through the API has to be registered with Google as an
OAuth client in a Google Cloud project. Gmail has no in-app switch for this. The mailbox
owner (or a Workspace admin) then approves that client. There are two ways to do this,
chosen with `GMAIL_AUTH_MODE`.

### Option A: service account with domain-wide delegation (`service_account`, default)

Best for Google Workspace and for many mailboxes: one key reads every listed mailbox and
nobody has to sign in. Needs a Workspace admin.

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a
   project, then **APIs & Services → Library → Gmail API → Enable**.
2. **IAM & Admin → Service accounts → Create service account**, then **Keys → Add key →
   JSON**.
3. In the Workspace Admin console, go to **Security → Access and data control → API
   controls → Manage domain-wide delegation**. Add the service account's client ID and
   grant exactly `https://www.googleapis.com/auth/gmail.readonly`.
4. Save the key as `secrets/gmail/service-account.json` and set
   `GMAIL_SERVICE_ACCOUNT_FILE` to it.

### Option B: per-mailbox OAuth (`oauth`)

For personal `@gmail.com` accounts, or when you are not a Workspace admin. Each mailbox
owner signs in once.

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a
   project, then **APIs & Services → Library → Gmail API → Enable**.
2. **APIs & Services → OAuth consent screen** (called **Google Auth Platform** in the
   newer console):
   - **Internal**, if offered (the project belongs to your Workspace org). There is no
     verification and no test-user list.
   - Otherwise **External**, left in *Testing*, with each mailbox added as a test user.
     In Testing, refresh tokens expire after 7 days, so re-run step 4 weekly or publish
     the app.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop
   app** (credentials are created at project level, not on the Gmail API page). If
   the wizard asks what data you'll access, choose **User data**. Download the JSON as
   `secrets/gmail/client.json`.
4. For each mailbox, run the helper. It opens a browser, asks for read-only access,
   checks you signed in as that mailbox, and writes `<mailbox>.json` (mode `0600`):

   ```bash
   make gmail-auth MAILBOX=ada@example.com
   # or: uv run arango-connector-gmail-auth --client-secrets secrets/gmail/client.json \
   #         --mailbox ada@example.com --out-dir secrets/gmail
   ```

   On a headless machine, add `ARGS=--no-browser` (or `--no-browser`) to print the
   consent URL instead.
5. Set `GMAIL_AUTH_MODE=oauth` and `GMAIL_OAUTH_TOKEN_DIR=secrets/gmail`.

The connector refuses to start if the key file, or any mailbox's token file, is missing.
`secrets/` is git-ignored.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GMAIL_MAILBOXES` | required | Comma-separated addresses |
| `GMAIL_AUTH_MODE` | `service_account` | or `oauth` |
| `GMAIL_SERVICE_ACCOUNT_FILE` | required for `service_account` | JSON key with domain-wide delegation |
| `GMAIL_OAUTH_TOKEN_DIR` | required for `oauth` | One `<mailbox>.json` per mailbox |
| `GMAIL_LABELS` | `INBOX,SENT` | Ingest mail with **any** of these. Label IDs or names (names are resolved per mailbox, case-insensitive). The default captures both sides of conversations |
| `GMAIL_EXCLUDE_LABELS` | `SPAM,TRASH,DRAFT` | ... and **none** of these |
| `GMAIL_POLL_INTERVAL_SECONDS` | `60` | |
| `GMAIL_INITIAL_SYNC` | `now` | First start for a mailbox: `now` takes only new mail; `lookback` also takes recent mail |
| `GMAIL_INITIAL_LOOKBACK` | `24h` | For `lookback`: e.g. `90m`, `24h`, `7d`. Bounded; this is not a full backfill |
| `GMAIL_KEY_MODE` | `hashed` | `hashed`: keys, `scope.container_id`, cursors and logs use a 16-character SHA-256 of the address. `plain`: the address itself |
| `GMAIL_STORE_BODY` | `true` | `false` stores headers only: no `body_text`, no `snippet`, no body data in `raw` |
| `GMAIL_STORE_BCC` | `true` | `false` drops Bcc from the payload and from `raw` headers |
| `GMAIL_RAW_MODE` | `stripped` | `full` (the API message as-is), `stripped` (no base64 body data) or `none` |

Redaction always wins over `GMAIL_RAW_MODE`: with `GMAIL_STORE_BODY=false` or
`GMAIL_STORE_BCC=false`, even `full` raw omits that content.

## Running

**Locally**, against ArangoDB from `docker compose up -d arangodb`, or the disposable
one from `make test-db-up` (<http://localhost:18529>, `root` / `test-password`):

```bash
SOURCE=gmail make run      # or: SOURCE=gmail uv run python -m arango_connector.main
```

The `connector` database and its collections are created on the first start, so they
appear in the ArangoDB UI only after the connector has run once. In the UI, pick the
database on the login screen.

**In Docker**, the `connector-gmail` Compose service runs as a separate process next to
Slack. It writes to the `emails` collection (`GMAIL_ARANGO_COLLECTION`) and mounts
`./secrets/gmail` read-only at `/secrets/gmail`:

```bash
docker compose --profile gmail up -d --build
```

Put the key at `secrets/gmail/service-account.json`, or the token files in
`secrets/gmail/`; the service sets the in-container paths. The container runs as uid
10001, so those files must be readable by it. On Linux hosts that may mean `chmod 644`,
or changing their owner to that uid.

### Trying it against your own mailbox

1. In Gmail, create a label `test` and a filter: **To**
   `you+test@yourdomain.com` → **Apply the label: test**.
2. Set `GMAIL_LABELS=test`, `GMAIL_KEY_MODE=plain` (readable keys) and
   `GMAIL_POLL_INTERVAL_SECONDS=15`, then start the connector.
3. Send mail to `you+test@yourdomain.com` and query:

   ```aql
   FOR e IN emails SORT e.created_at DESC
     RETURN { key: e._key, subject: e.payload.subject, labels: e.payload.label_ids,
              body: e.payload.body_text, updated_at: e.updated_at }
   ```

4. Also try: applying `test` to an older email (ingested with `updated_at` set),
   toggling read/unread (same document, `label_ids` changes), restarting (no
   duplicates), and HTML or attachment or non-ASCII subjects.

## How sync works

- **Cursor per mailbox.** Each mailbox's Gmail `historyId` and last sync time are kept in
  `connector_state`, so a restart resumes where it stopped. The cursor only advances
  after every message up to it has been handed to the sink, so delivery is
  at-least-once; deterministic keys make repeats harmless.
- **First start.** `GMAIL_INITIAL_SYNC=now` records the current position and ingests
  only later mail. `lookback` also ingests mail received within
  `GMAIL_INITIAL_LOOKBACK`.
- **What is read.** New messages, and messages whose labels changed. A message that
  gains a watched label later (for example, moved back to the inbox) is ingested then.
  A label change rewrites the stored document with the current `label_ids` and sets
  `updated_at`, so toggling read/unread rewrites it too.
- **What is not removed.** Mail that is deleted, trashed or moved out of watched labels
  stays stored until retention removes it, as with Slack deletes.
- **History gaps.** Gmail keeps history for about a week. If the connector was down
  longer, the next poll logs `gmail_history_gap`, catches up by date from an hour
  before the last sync, and then resumes history sync.
- **Failures.** A mailbox that fails is logged as `gmail_mailbox_sync_failed` and
  retried on the next poll without holding up the others. If every mailbox fails,
  polling backs off exponentially, up to 5 minutes. Transient API errors (429, 5xx)
  are retried inside each call.

## Document shape

`scope`: `tenant_id` = the mailbox's domain, `container_id` = the mailbox key (hashed
or the address, per `GMAIL_KEY_MODE`), `path` = `[]`.

```json
{
  "_key": "gmail_3f9a1c2b7d4e5f60_18c2a1b2c3d4e5f6",
  "schema_version": 4,
  "source": "gmail",
  "kind": "email",
  "source_id": "18c2a1b2c3d4e5f6",
  "scope": { "tenant_id": "example.com", "container_id": "3f9a1c2b7d4e5f60", "path": [] },
  "created_at": "2024-05-23T21:33:20Z",
  "updated_at": null,
  "ingested_at": "2024-05-23T21:34:02.512000Z",
  "expires_at": "2024-06-22T21:33:20Z",
  "payload": {
    "kind": "email",
    "subject": "Q2 numbers",
    "from": { "name": "Grace Hopper", "address": "grace@example.com" },
    "to": [{ "name": "Ada Lovelace", "address": "ada@example.com" }],
    "cc": [], "bcc": [], "reply_to": [],
    "message_id_header": "<CAF=abc123@mail.gmail.com>",
    "in_reply_to": null,
    "references": [],
    "thread_id": "18c2a1b2c3d4e5f6",
    "label_ids": ["INBOX", "UNREAD"],
    "snippet": "Hi Ada, the numbers for Q2 & Q3 are attached",
    "body_text": "Hi Ada,\nSee Q2.",
    "has_attachments": true,
    "attachment_names": ["q2.pdf"]
  },
  "raw": { "...": "the Gmail API message, per GMAIL_RAW_MODE" }
}
```

- `created_at` is Gmail's `internalDate`, when Google received the message.
- `body_text` is the first inline `text/plain` part, or else the `text/html` part
  converted to text. Encoded subjects and names (RFC 2047) are decoded.
- `thread_id` is Gmail's thread. `message_id_header`, `in_reply_to` and `references`
  allow threading across mailboxes.
- `label_ids` are Gmail label IDs (`INBOX`, `Label_123`, …), not names.

## Privacy and security

Email holds personal and sensitive data. Beyond the read-only scope:

- Write Gmail to its own collection (the Compose service uses `emails`), and give
  readers a dedicated ArangoDB user with access to that collection only.
- `GMAIL_KEY_MODE=hashed` (the default) keeps mailbox addresses out of document keys,
  cursors and logs. The address is still present in the payload headers.
- Use `GMAIL_STORE_BODY=false`, `GMAIL_STORE_BCC=false` and `GMAIL_RAW_MODE=none` for
  metadata-only deployments.
- Retention applies as for any source; see [Retention](../../README.md#retention).
- OAuth token files are written `0600`. Keep `secrets/` out of version control.

## Troubleshooting

| Symptom / log event | Cause | Fix |
|---|---|---|
| No `connector` database in the UI | The connector hasn't started successfully yet | Fix the startup error; the database is created on first start |
| `GMAIL_SERVICE_ACCOUNT_FILE does not exist` / `no token file … for: <mailbox>` | Missing credentials | Complete [Setup](#setup) for every mailbox |
| Consent screen: "access blocked" / `admin_policy_enforced` | Workspace blocks this OAuth client | Ask a Workspace admin to allow the app, or use an Internal consent screen |
| `Signed in as '…', not '…'; token not saved` | Wrong Google account in the browser | Re-run and choose the right account |
| `gmail_mailbox_sync_failed` with `unauthorized_client` | Domain-wide delegation missing or wrong scope | Grant exactly `gmail.readonly` to the service account's client ID |
| `gmail_mailbox_sync_failed` with `invalid_grant` | OAuth token expired or revoked (7 days in Testing) | Re-run `arango-connector-gmail-auth` |
| `gmail_label_unknown` / `gmail_no_watched_labels` | A configured label doesn't exist in that mailbox | Check the name or ID; names are matched per mailbox |
| `gmail_history_gap` | Down longer than Gmail keeps history | Nothing; the connector catches up by date |

## Not supported yet

Attachment content, sending or modifying mail, Pub/Sub push notifications, full mailbox
backfill, removing messages that are deleted or moved out of watched labels, and batch
fetching. See the `TODO(future)` comments in `sources/gmail/`. For non-Gmail providers,
see the planned IMAP source (SE-136).
