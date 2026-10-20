# Gmail source

`SOURCE=gmail`. Polls each mailbox in `GMAIL_MAILBOXES` every
`GMAIL_POLL_INTERVAL_SECONDS` (60 by default) through the Gmail API's history feed and
writes one document per email, so new mail lands within about a minute.

| | |
|---|---|
| Kind | `email`, plus `attachment` when `GMAIL_ATTACHMENTS` is on |
| Delivery | poll, incremental via Gmail `historyId` |
| Keys | `gmail_{mailbox key}_{message id}`, deterministic, so a re-read overwrites |
| Cursor | per mailbox in `connector_state`: `{history_id, last_synced_at}` |
| Access | `https://www.googleapis.com/auth/gmail.readonly` only; it can never send, change or delete mail |

## Setup

Every program that reads Gmail through the API has to be registered with Google as an
OAuth client in a Google Cloud project; Gmail has no in-app switch for this. The client
is then approved either by a Workspace admin (Option A) or by each mailbox owner signing
in (Option B). `GMAIL_AUTH_MODE` selects which.

### Scopes

The connector needs exactly one scope:

| Scope | Why |
|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | Read messages, labels, history and the profile. Covers everything the connector does |

That is all it requests, in both modes, and it means the connector cannot send, change
or delete mail. Scopes it deliberately does **not** use:

| Scope | Why not |
|---|---|
| `gmail.metadata` | Headers and labels only, and it forbids the `q` search parameter. The connector needs `q` for `lookback` and for catching up after a history gap. Even with `GMAIL_STORE_BODY=false`, it still needs `gmail.readonly` |
| `gmail.modify`, `gmail.send`, `https://mail.google.com/` | Write access; never needed |
| `openid`, `email`, `userinfo.*` | Not needed. The auth helper checks which account signed in through the Gmail profile, which `gmail.readonly` covers |

`gmail.readonly` is one of Google's **restricted** scopes. That matters only for an
External OAuth app published to production, which must then pass Google's verification
and security assessment. Internal apps, and External apps left in Testing, need no
verification.

### Choosing an auth mode

| | Option A: service account + domain-wide delegation | Option B: per-mailbox OAuth |
|---|---|---|
| `GMAIL_AUTH_MODE` | `service_account` (default) | `oauth` |
| Who sets it up | A Google Workspace **admin** | Anyone with a Google Cloud project; each mailbox owner signs in once |
| Works for | Workspace mailboxes in your domain | Workspace **and** personal `@gmail.com` mailboxes |
| Adding a mailbox | Add it to `GMAIL_MAILBOXES` | Add it, and that person runs `make gmail-auth` |
| Credentials | One JSON key for every mailbox | One token file per mailbox |
| Expiry | Key valid until rotated | Lasting refresh tokens (Internal); 7 days in External *Testing* |
| Revoked by | Admin removing the delegation | The user, under Google Account → Security → Third-party access, or an admin |

**Choose A** for production on Google Workspace: one credential, no per-user steps, it
scales to many mailboxes, and access is controlled centrally by the admin.

**Choose B** when you aren't a Workspace admin, for personal Gmail, for a small number
of mailboxes, or for trying the connector against your own mailbox. Each mailbox
explicitly consents, and nobody holds a domain-wide key.

### Option A: service account with domain-wide delegation

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a
   project, then **APIs & Services → Library → Gmail API → Enable**.
2. Create a service account, either way:
   - **Gmail API page → Create credentials**, then choose **Application data**. The
     wizard creates the service account.
   - **IAM & Admin → Service accounts → Create service account**.

   Then open it, go to **Keys → Add key → Create new key → JSON**, and note its
   **Client ID** (a number).
3. In the Workspace Admin console (admin.google.com), go to **Security → Access and
   data control → API controls → Manage domain-wide delegation → Add new**. Enter the
   Client ID and exactly `https://www.googleapis.com/auth/gmail.readonly`. It can take
   a few minutes to apply.
4. Save the key as `secrets/gmail/service-account.json` and set
   `GMAIL_SERVICE_ACCOUNT_FILE` to it. Treat the key like a password: it can read every
   mailbox in the domain.

### Option B: per-mailbox OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a
   project, then **APIs & Services → Library → Gmail API → Enable**.
2. **Configure the consent screen** (**APIs & Services → OAuth consent screen**, called
   **Google Auth Platform** in the newer console). If you use the wizard in step 3, it
   walks you through this.
   - **Audience:** **Internal** if offered (the project belongs to your Workspace org).
     There is no verification and no test-user list, and only accounts in your domain
     can sign in. Otherwise **External**, left in *Testing*, with each mailbox added
     under **Test users**. Refresh tokens issued in Testing expire after 7 days, so you
     would re-run step 4 weekly.
   - **Branding:** an app name such as `Arango Connector` and a support email.
   - **Data access (scopes):** add `https://www.googleapis.com/auth/gmail.readonly`.
     This records what the app asks for. The helper requests the scope at sign-in
     either way.
3. **Create the OAuth client.** Both ways give the same result: credentials belong to
   the project, not to one API, and Gmail access comes from the scope requested at
   sign-in.
   - **Way 1: the wizard, from the Gmail API page.** Click **Create credentials**,
     choose **User data**, and follow the steps through the consent screen, scopes, and
     an OAuth client of type **Desktop app**. Use this for a new project: it does
     step 2 and this step in one guided flow, so nothing is missed.
   - **Way 2: from APIs & Services → Credentials.** Click **Create credentials → OAuth
     client ID → Desktop app**. Use this once the consent screen is already set up,
     when adding another client, or in a shared project where you don't want the
     wizard changing the consent screen.

   Choose **Desktop app**: the helper signs in through a temporary local web server
   (`localhost`), which is the flow Desktop clients are made for. **Web application**
   clients need fixed redirect URLs and won't work with it. Download the JSON as
   `secrets/gmail/client.json`. It identifies the app, not any user, but keep it
   private anyway.
4. For each mailbox, run the helper. It opens a browser, asks for read-only access,
   checks you signed in as that mailbox, and writes `<mailbox>.json` (mode `0600`):

   ```bash
   make gmail-auth MAILBOX=ada@example.com
   # or: uv run arango-connector-gmail-auth --client-secrets secrets/gmail/client.json \
   #         --mailbox ada@example.com --out-dir secrets/gmail
   ```

   Google redirects the browser to `http://localhost:<port>` **on the machine running
   the helper**, so the browser and the helper must be on the same machine. For a
   remote or headless server, either:
   - run the helper on your laptop and copy the token file to the server's
     `secrets/gmail/`; or
   - fix the port and forward it over SSH, then open the printed URL in your local
     browser:

     ```bash
     ssh -L 8765:localhost:8765 server      # on your laptop
     make gmail-auth MAILBOX=ada@example.com ARGS="--port 8765 --no-browser"   # on the server
     ```

   The helper only accepts **Desktop app** clients and says so if given a Web client.
   A browser-based **Connect** flow for Web clients is planned (SE-138).
5. Set `GMAIL_AUTH_MODE=oauth` and `GMAIL_OAUTH_TOKEN_DIR=secrets/gmail`.

The connector refuses to start if the key file, or any mailbox's token file, is missing.
`secrets/` is git-ignored.

## Configuration

<!-- BEGIN GENERATED: config:gmail -->
| Variable | Default | Values | Description |
|---|---|---|---|
| `GMAIL_AUTH_MODE` | `service_account` | `service_account` \| `oauth` | `service_account` (Workspace, domain-wide delegation) or `oauth` (per-mailbox tokens). |
| `GMAIL_SERVICE_ACCOUNT_FILE` | — | path | Service account JSON key with domain-wide delegation. Required for `service_account`. |
| `GMAIL_OAUTH_TOKEN_DIR` | — | path | Directory of `<mailbox>.json` token files from `arango-connector-gmail-auth`. Required for `oauth`. |
| `GMAIL_MAILBOXES` | **required** | comma-separated | Mailboxes to read, comma-separated. |
| `GMAIL_LABELS` | `INBOX,SENT` | comma-separated | Ingest mail carrying **any** of these labels (IDs or names, resolved per mailbox). The default captures both sides of conversations. |
| `GMAIL_EXCLUDE_LABELS` | `SPAM,TRASH,DRAFT` | comma-separated | ... and **none** of these. Always checked on the message itself. |
| `GMAIL_LABEL_MATCH` | `thread` | `thread` \| `message` | `thread`: a message qualifies if any message in its thread carries a watched label, as Gmail's conversation view shows it, so replies come along. `message`: only its own labels count. |
| `GMAIL_POLL_INTERVAL_SECONDS` | `60` | number > 0 | Seconds between polls. |
| `GMAIL_INITIAL_SYNC` | `now` | `now` \| `lookback` | First start for a mailbox: `now` takes only new mail; `lookback` also takes mail from the last `GMAIL_INITIAL_LOOKBACK`. |
| `GMAIL_INITIAL_LOOKBACK` | `24h` | duration | For `lookback`: e.g. `90m`, `24h`, `7d`. Bounded; not a full backfill. |
| `GMAIL_KEY_MODE` | `hashed` | `hashed` \| `plain` | `hashed`: keys, `scope.container_id`, cursors and logs use a 16-character SHA-256 of the address. `plain`: the address itself. |
| `GMAIL_STORE_BODY` | `true` | `true` \| `false` | `false` stores headers only: no `body_text`, no `snippet`, no body data in `raw`, and attachments limited to `off` or `metadata`. |
| `GMAIL_STORE_BCC` | `true` | `true` \| `false` | `false` drops Bcc from the payload and from `raw` headers. |
| `GMAIL_RAW_MODE` | `stripped` | `full` \| `stripped` \| `none` | `full` (the API message as-is), `stripped` (no base64 body data) or `none`. |
| `GMAIL_ATTACHMENTS` | `off` | `off` \| `metadata` \| `content` \| `text` \| `all` | `off`; `metadata` (filename, type, size; no downloads); `content` (+ file as base64 and sha256); `text` (+ extracted text, no file); `all`. |
| `GMAIL_ATTACHMENT_MAX_BYTES` | `10485760` | integer > 0 | Larger attachments are stored as metadata only (`skipped_reason: "too_large"`). |
| `GMAIL_ATTACHMENT_TEXT_MAX_CHARS` | `1000000` | integer ≥ 0 | Extracted text beyond this is cut off (`text_truncated`). `0` means no limit. |
<!-- END GENERATED: config:gmail -->

Redaction always wins over `GMAIL_RAW_MODE`: with `GMAIL_STORE_BODY=false` or
`GMAIL_STORE_BCC=false`, even `full` raw omits that content. `GMAIL_STORE_BODY=false`
also only allows `GMAIL_ATTACHMENTS=off` or `metadata`; the connector refuses to start
otherwise.

## Running

**Locally**, against Arango from `docker compose up -d arangodb`, or the disposable
one from `make test-db-up` (<http://localhost:18529>, `root` / `test-password`):

```bash
SOURCE=gmail make run      # or: SOURCE=gmail uv run python -m arango_connector_standalone.main
```

The `connector` database and its collections are created on the first start, so they
appear in the Arango UI only after the connector has run once. In the UI, pick the
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
- **Threads.** Gmail shows labels per conversation but stores them per message, and a
  reply arriving after you labelled a conversation does not get the label. With
  `GMAIL_LABEL_MATCH=thread` (the default), a message also qualifies when another,
  non-excluded message in its thread has a watched label, so replies and follow-ups
  are ingested. Applying a watched label to one message also pulls in the rest of its
  thread. A spam or trashed message is never ingested, and a labelled message in spam
  does not qualify its thread. The thread check costs one small `threads.get` per
  thread per poll, and only for messages that are not labelled themselves. Set
  `GMAIL_LABEL_MATCH=message` to require each message's own label.
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
- `label_ids` are Gmail label IDs (`INBOX`, `Label_123`, …), not names. They are the
  message's own labels; in `thread` mode a message can be stored without a watched
  label because its thread has one.

## Attachments

With `GMAIL_ATTACHMENTS` on, every part with a filename (attachments and inline images)
becomes its own `kind: "attachment"` document in the same collection as the emails:

```json
{
  "_key": "gmail_3f9a1c2b7d4e5f60_18c2a1b2c3d4e5f6_att_1",
  "kind": "attachment",
  "source_id": "18c2a1b2c3d4e5f6/1",
  "scope": { "tenant_id": "example.com", "container_id": "3f9a1c2b7d4e5f60", "path": [] },
  "created_at": "2024-05-23T21:33:20Z",
  "payload": {
    "kind": "attachment",
    "message_id": "18c2a1b2c3d4e5f6",
    "message_key": "gmail_3f9a1c2b7d4e5f60_18c2a1b2c3d4e5f6",
    "thread_id": "18c2a1b2c3d4e5f6",
    "part_id": "1",
    "filename": "q2.pdf",
    "mime_type": "application/pdf",
    "size": 48213,
    "inline": false,
    "content_id": null,
    "sha256": "9f2c…",
    "content_base64": "JVBERi0xLjQK…",
    "text": "Quarterly numbers …",
    "text_truncated": false,
    "skipped_reason": null
  },
  "raw": { "...": "the MIME part's headers and sizes, never its data" }
}
```

- **Linking.** `payload.message_key` is the email document's `_key`:
  `FOR a IN emails FILTER a.kind == "attachment" AND a.payload.message_key == @key RETURN a`.
- **Keys** use the message ID and MIME part ID, which are stable. Gmail's own
  attachment IDs change on every fetch.
- **Text extraction** covers PDF, DOCX, HTML and text-like types (plain text, CSV, JSON,
  XML, YAML, Markdown). A generic `application/octet-stream` falls back to the file
  extension. Other types (images, archives, spreadsheets, and legacy Word 97-2003
  `.doc`, which needs an external converter) are stored without text and with
  `skipped_reason: "unsupported_type"`. Scanned PDFs without a text layer give
  `no_text`, and password-protected ones give `encrypted`. The extractors live in
  `packages/core/src/arango_connector/extract.py`, shared by all sources.
- **When files are downloaded.** Only when the email is ingested for the first time:
  new mail, mail that just gained a watched label, or thread replies pulled in with it.
  Read/unread and other label changes rewrite the email but don't re-download its
  attachments. Mail stored before attachments were switched on gets them only if it is
  re-ingested that way.
- **Failures.** An attachment that disappeared is stored with `skipped_reason:
  "download_failed"`. A transient API error fails that mailbox's sync, and the next
  poll retries from the same cursor.
- **Size and retention.** Base64 makes files about a third larger. Arango is not a
  blob store, so keep `GMAIL_ATTACHMENT_MAX_BYTES` modest. Attachments carry their
  email's `created_at`, so they expire with it.

## As an ArangoDB Platform Link

The same source runs as a Link on the ArangoDB Platform: AI tools submit on-demand jobs
(search, read a thread, export a date range) and get the results as JSONL. See
[Links](../links.md) for deploying and using links. The settings above apply, and they set
what a job may touch:

- `GMAIL_MAILBOXES`: the only mailboxes a job may read.
- `GMAIL_LABELS` / `GMAIL_EXCLUDE_LABELS` / `GMAIL_LABEL_MATCH`: every message returned
  passes the same label and thread rules as standalone sync. `export` can narrow to some
  of the labels, never widen. With `GMAIL_LABEL_MATCH=message` the labels are added to
  the Gmail query, so Gmail does the filtering. With `thread` (the default) every message
  matching the query is read and checked, because a reply can qualify without the label,
  so give large-mailbox exports a date range.
- `GMAIL_ATTACHMENTS`: the most a job may request (`metadata` lets jobs ask for `off` or
  `metadata`, and so on).
- Polling and initial-sync settings are ignored.

Credentials go in a Kubernetes Secret mounted with `credentialsSecret` (the service
account key or OAuth token files, at `/secrets/gmail/`); point
`GMAIL_SERVICE_ACCOUNT_FILE` or `GMAIL_OAUTH_TOKEN_DIR` there.

<!-- BEGIN GENERATED: link:gmail -->
Read email from Gmail: search with Gmail query syntax, fetch a whole thread, or export a date range. Returns one document per email, plus one per attachment when requested, as JSONL.

Tags: `email`, `gmail`, `google-workspace`.

**`export`**: Messages received in a date range, optionally narrowed to some labels.

| Field | Required | Default | Values | Description |
|---|---|---|---|---|
| `mailbox` | yes | — | string | Mailbox to read. Must be one of the link's mailboxes. |
| `attachments` | no | `"off"` | `off` \\| `metadata` \\| `content` \\| `text` \\| `all` | Attachment documents to include: off, metadata, content (file as base64), text (extracted text) or all. Limited by the link's configuration. |
| `after` | no | — | date | Received on or after this date. |
| `before` | no | — | date | Received before this date. |
| `labels` | no | — | list of string | Only these labels (IDs or names); must be within the link's labels. |
| `max_results` | no | `1000` | integer ≥ 1, ≤ 10000 | Emails to return. |

Example: Inbox mail since 1 October, with attachment metadata.

```json
{"operation": "export", "mailbox": "ada@example.com", "after": "2026-10-01", "labels": ["INBOX"], "attachments": "metadata"}
```

**`get_thread`**: Every message in one thread.

| Field | Required | Default | Values | Description |
|---|---|---|---|---|
| `mailbox` | yes | — | string | Mailbox to read. Must be one of the link's mailboxes. |
| `attachments` | no | `"off"` | `off` \\| `metadata` \\| `content` \\| `text` \\| `all` | Attachment documents to include: off, metadata, content (file as base64), text (extracted text) or all. Limited by the link's configuration. |
| `thread_id` | yes | — | string length ≥ 1 | Gmail thread ID. |

Example: Every email in the thread.

```json
{"operation": "get_thread", "mailbox": "ada@example.com", "thread_id": "18c2a1b2"}
```

**`search`**: Messages matching a Gmail search query.

| Field | Required | Default | Values | Description |
|---|---|---|---|---|
| `mailbox` | yes | — | string | Mailbox to read. Must be one of the link's mailboxes. |
| `attachments` | no | `"off"` | `off` \\| `metadata` \\| `content` \\| `text` \\| `all` | Attachment documents to include: off, metadata, content (file as base64), text (extracted text) or all. Limited by the link's configuration. |
| `query` | yes | — | string length ≥ 1 | Gmail search syntax, e.g. "from:ada subject:invoice". |
| `max_results` | no | `100` | integer ≥ 1, ≤ 1000 | Emails to return. |

Example: Emails whose subject mentions invoice.

```json
{"operation": "search", "mailbox": "ada@example.com", "query": "subject:invoice"}
```
<!-- END GENERATED: link:gmail -->

## Privacy and security

Email holds personal and sensitive data. Beyond the read-only scope:

- Write Gmail to its own collection (the Compose service uses `emails`), and give
  readers a dedicated Arango user with access to that collection only.
- `GMAIL_KEY_MODE=hashed` (the default) keeps mailbox addresses out of document keys,
  cursors and logs. The address is still present in the payload headers.
- Use `GMAIL_STORE_BODY=false`, `GMAIL_STORE_BCC=false`, `GMAIL_RAW_MODE=none` and
  `GMAIL_ATTACHMENTS=off` (or `metadata`) for metadata-only deployments.
- Attachments often hold the most sensitive data (contracts, IDs, payslips). Turn on
  `content` or `text` deliberately, and restrict who can read the collection.
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

Sending or modifying mail, Pub/Sub push notifications, full mailbox backfill, removing messages that are deleted or moved out of watched labels, and batch
fetching.

There is also no "ingest everything" option yet: `GMAIL_LABELS` needs at least one
label, so archived mail (which loses `INBOX`) and mail with only other labels is
skipped. Planned: `GMAIL_LABELS=*` for every message, with `GMAIL_EXCLUDE_LABELS` still
applied. See the `TODO(future)` comments in `sources/gmail/`. For non-Gmail providers,
see the planned IMAP source (SE-136).
