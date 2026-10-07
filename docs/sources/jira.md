# Jira source

`SOURCE=jira`. Polls each Jira Cloud project in `JIRA_PROJECTS` every
`JIRA_POLL_INTERVAL_SECONDS` (60 by default) with an incremental JQL search, and writes
one document per issue and one per comment. New issues, edits and comments land within
about two minutes.

| | |
|---|---|
| Kind | `issue` and `comment` |
| Delivery | poll, incremental via JQL `updated >= <cursor>` |
| Keys | `jira_{site}_issue_{issue id}` and `jira_{site}_comment_{comment id}`, deterministic, so a re-read overwrites |
| Cursor | per project in `connector_state`: `{updated_since, last_synced_at}` |
| Access | an API token for one Atlassian account; the connector only reads (`GET` and the read-only JQL search `POST`) |

## Setup

The connector authenticates as one Atlassian account with an API token (HTTP basic
auth, email + token). It sees exactly what that account can see.

1. **Pick the account.** A dedicated account (for example `connector-bot@yourco.com`)
   is best: it keeps access auditable and survives people leaving. It needs a Jira
   license (product access) on the site.
2. **Grant read access.** In each project to sync, the account needs the **Browse
   Projects** permission (any role that includes it, such as a viewer role, is enough).
   To include comments restricted to a role or group, the account must also be in that
   role or group; comments it cannot see are not synced. No write permission is needed.
3. **Create the API token.** Signed in as that account, go to
   <https://id.atlassian.com/manage-profile/security/api-tokens> → **Create API token**,
   give it a name (`arango-connector`) and an expiry, and copy it. This is
   `JIRA_API_TOKEN`. Tokens expire (one year at most), so note the date.
   Use a classic token rather than a scoped one: the connector calls the site URL
   directly with basic auth.
4. **Find the project keys.** Each project's key is the prefix of its issue keys
   (`PROJ` in `PROJ-123`), also shown under **Project settings → Details**.
5. **Check the account's time zone** (profile → **Account preferences**). JQL reads
   dates in the account's time zone; the connector looks it up and converts, so any
   zone works, but changing it while the connector runs is picked up only on restart.

Put the site URL, the account's email, the token and the project keys in `.env`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `JIRA_BASE_URL` | required | `https://your-site.atlassian.net`. Must be https; any path is dropped |
| `JIRA_EMAIL` | required | The account the token belongs to |
| `JIRA_API_TOKEN` | required | Held as a secret; never logged |
| `JIRA_PROJECTS` | required | Comma-separated project keys, e.g. `PROJ,OPS` (case-insensitive) |
| `JIRA_POLL_INTERVAL_SECONDS` | `60` | Keep at or below `240` to meet a five-minute freshness target |
| `JIRA_INITIAL_SYNC` | `lookback` | First start per project: `now` (only later changes), `lookback` (issues updated within `JIRA_INITIAL_LOOKBACK`) or `all` (every issue) |
| `JIRA_INITIAL_LOOKBACK` | `30d` | For `lookback`: e.g. `12h`, `30d`, `365d` |
| `JIRA_PAGE_SIZE` | `100` | Issues per search request, 1–100 |

Compose-only (the `connector-jira` service):

| Variable | Default | Notes |
|---|---|---|
| `JIRA_ARANGO_COLLECTION` | `jira` | Becomes `ARANGO_COLLECTION` for the service |
| `JIRA_RETENTION_DAYS` | `0` | Becomes `RETENTION_DAYS`. See [Retention](#retention) |

## Running

**Locally**, against ArangoDB from `docker compose up -d arangodb`, or the disposable
one from `make test-db-up` (<http://localhost:18529>, `root` / `test-password`):

```bash
SOURCE=jira ARANGO_COLLECTION=jira RETENTION_DAYS=0 make run
```

**In Docker**, the `connector-jira` Compose service runs as its own process next to the
others, writing to the `jira` collection with retention off:

```bash
docker compose --profile jira up -d --build
```

### Trying it against your own site

1. Create a scratch project (key `CONN`), set `JIRA_PROJECTS=CONN`,
   `JIRA_POLL_INTERVAL_SECONDS=15`, and start the connector.
2. Create an issue, add a comment, then edit the comment and the issue summary.
3. Query:

   ```aql
   FOR d IN jira SORT d.updated_at DESC
     RETURN { key: d._key, kind: d.kind, jira: d.payload.issue_key || d.payload.parent_key,
              text: d.payload.summary || d.payload.text, updated_at: d.updated_at }
   ```

4. Restart the connector and check nothing is duplicated:

   ```aql
   FOR d IN jira COLLECT kind = d.kind, id = d.source_id WITH COUNT INTO n
     FILTER n > 1 RETURN { kind, id, n }
   ```

## How sync works

- **Cursor per project.** Each project's cursor is the newest `updated` time read, kept
  in `connector_state`. It is checkpointed after every page of results (issues come
  oldest-update first) and set to the sync's start time when the sync completes, so a
  restart, a failure or a long first sync resumes rather than starting over. Delivery is
  at-least-once; deterministic keys make repeats harmless.
- **Overlap.** Each search re-reads from two minutes before the cursor. JQL compares
  dates to the minute, and Jira's search index can lag an update by a few seconds; the
  overlap covers both, and the re-read items overwrite themselves. This also assumes the
  connector host's clock is within about two minutes of Jira's (use NTP).
- **What is read.** Every issue whose `updated` changed (created, edited, transitioned,
  commented on), and every comment on those issues that was added or edited in the same
  window. Adding or editing a comment bumps the issue's `updated`, which is how comment
  changes are found. A changed issue or comment overwrites its document, with
  `updated_at` set from Jira.
- **Comments.** Search results embed only the first page of an issue's comments; when
  an issue has more, the connector fetches them all from the comment endpoint.
- **First start.** `lookback` (the default) reads issues updated within
  `JIRA_INITIAL_LOOKBACK` with all of their comments. `all` reads the whole project.
  `now` only records a cursor. An interrupted first sync remembers that it is one and
  keeps reading all comments of the issues it has not reached yet.
- **Adding a project** to `JIRA_PROJECTS` later gives it its own first sync; the other
  projects carry on from their cursors.
- **What is not removed.** Deleted issues and comments stay stored (see
  [Not supported yet](#not-supported-yet)). An issue moved to an unwatched project
  stays as last seen; moved to a watched one, it keeps its key and its `scope` and
  `issue_key` follow the move.
- **Rate limits and failures.** 429 and 5xx responses and connection errors are retried
  up to five times per request, waiting `Retry-After` when Jira sends it (capped at 60s)
  and an exponential backoff with jitter otherwise; each wait logs `jira_request_retry`.
  A project that still fails is logged as `jira_project_sync_failed` and retried on the
  next poll without holding up the others. If every project fails, polling backs off
  exponentially, up to 5 minutes. A shutdown cuts a retry wait short and keeps the last
  checkpoint (`jira_sync_interrupted`).

## Document shape

`scope`: `tenant_id` = the site host, `container_id` = the project's numeric id,
`path` = `[]` for issues and `[issue id]` for comments.

An issue:

```json
{
  "_key": "jira_acme.atlassian.net_issue_10001",
  "schema_version": 4,
  "source": "jira",
  "kind": "issue",
  "source_id": "10001",
  "scope": { "tenant_id": "acme.atlassian.net", "container_id": "10000", "path": [] },
  "created_at": "2024-05-23T21:33:20.123000Z",
  "updated_at": "2024-05-24T09:12:05.481000Z",
  "ingested_at": "2024-05-24T09:13:01.004000Z",
  "expires_at": null,
  "payload": {
    "kind": "issue",
    "issue_key": "PROJ-1",
    "project_key": "PROJ",
    "summary": "Login page times out",
    "description": "Steps to reproduce\n\n1. Open /login\n2. Wait 30s",
    "issue_type": "Bug",
    "status": "In Progress",
    "status_category": "indeterminate",
    "priority": "Medium",
    "resolution": null,
    "resolved_at": null,
    "due_date": "2024-06-30",
    "labels": ["frontend", "auth"],
    "components": ["Web"],
    "assignee_id": "5b10ac8d82e05b22cc7d4ef5",
    "assignee_name": "Grace Hopper",
    "reporter_id": "5b10a2844c20165700ede21g",
    "reporter_name": "Ada Lovelace",
    "parent_id": "10000",
    "parent_key": "PROJ-0",
    "url": "https://acme.atlassian.net/browse/PROJ-1",
    "comment_count": 2
  },
  "raw": { "id": "10001", "key": "PROJ-1", "fields": { "...": "as returned, without comment" } }
}
```

A comment:

```json
{
  "_key": "jira_acme.atlassian.net_comment_20001",
  "source": "jira",
  "kind": "comment",
  "source_id": "20001",
  "scope": { "tenant_id": "acme.atlassian.net", "container_id": "10000", "path": ["10001"] },
  "created_at": "2024-05-24T09:12:05.481000Z",
  "updated_at": "2024-05-24T09:12:05.481000Z",
  "payload": {
    "kind": "comment",
    "author_id": "5b10a2844c20165700ede21g",
    "author_name": "Ada Lovelace",
    "author_type": "user",
    "text": "Looks good to me",
    "parent_id": "10001",
    "parent_key": "PROJ-1",
    "visibility": null
  },
  "raw": { "...": "the comment as returned, body in ADF" }
}
```

- Keys use Jira's numeric ids, not `PROJ-123`, which changes when an issue moves
  between projects. Look issues up by `payload.issue_key`.
- `description` and comment `text` are plain text rendered from Atlassian Document
  Format (mentions become `@Name`, links their URL, lists and tables flattened). The
  original ADF is in `raw`.
- `status_category` is Jira's workflow-independent bucket: `new`, `indeterminate` or
  `done`.
- `parent_id` / `parent_key` on an issue is its epic or, for a sub-task, its story. On a
  comment it is the issue commented on.
- `author_type` is `bot` for app accounts (automation, integrations), `user` otherwise.
- `comment_count` is the issue's total number of comments when it was read.
- Only the fields listed above are requested from Jira; custom fields are not.

## Retention

Retention counts from `created_at`, which for Jira is when the issue (or comment) was
created. With `RETENTION_DAYS=30`, an issue created two years ago and updated today
would be written already expired and removed by ArangoDB's TTL sweep almost at once.
So the Compose service runs Jira with retention **off** (`JIRA_RETENTION_DAYS=0`), in
its own collection. If you turn it on, expect only recently created issues and comments
to be kept.

## Privacy and security

Issues and comments can hold customer data, credentials pasted by mistake, and
discussion restricted to a role or group.

- The connector stores whatever the account can see. Restricted comments keep their
  restriction in `payload.visibility` (`{type: "role" | "group", value}`), but ArangoDB
  does not enforce it: give readers of the `jira` collection the same clearance, or
  filter on `payload.visibility == null`.
- Write Jira to its own collection (the Compose service uses `jira`), and give readers
  a dedicated ArangoDB user with access to that collection only.
- Use a dedicated account with read-only project roles, and an expiring token.
- The token is held as a secret and never logged; error messages carry only Jira's own
  error text.

## Troubleshooting

| Symptom / log event | Cause | Fix |
|---|---|---|
| `JIRA_…: is required but not set` at startup | Missing setting | See [Configuration](#configuration) |
| `jira_project_sync_failed` with `HTTP 401` | Wrong email or token, or the token expired | Create a new token for the same account as `JIRA_EMAIL` |
| `jira_project_sync_failed` with `HTTP 403` | The account has no product access to the site | Give it a Jira license |
| `jira_project_sync_failed` with `does not exist for the field 'project'` | Wrong key, or the account cannot browse the project | Check the key and the **Browse Projects** permission |
| `jira_request_retry reason=jira-…` or `reason=429` | Rate limited | Nothing; it waits `Retry-After`. If constant, raise `JIRA_POLL_INTERVAL_SECONDS` or split projects across connectors |
| `jira_unknown_time_zone` | The account's time zone is not recognised | The connector uses UTC; set a standard zone on the account |
| Restricted comments missing | The account is not in that role or group | Add it, or accept that they are not synced |
| Old issues disappear right after being written | Retention is on; it counts from creation | Set `RETENTION_DAYS=0` for Jira; see [Retention](#retention) |

## Not supported yet

Webhooks (push instead of polling), deletes of issues and comments, attachments,
custom field mapping, worklogs and changelog history, OAuth 2.0 (3LO) and scoped
tokens, and Jira Data Center / Server. See the `TODO(future)` comments in
`sources/jira/`.
