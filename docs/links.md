# Links: connectors on the ArangoDB Platform

Each source can run as an **ArangoDB Platform Link**: a Deployment on the platform that AI
tools discover and send **on-demand jobs** to (search Gmail, read a Slack thread, export
Jira issues). Results come back as JSONL files in the platform's file storage. This is the
other half of the connector; the [standalone service](../README.md) is for **continuous**
ingestion into ArangoDB collections. The design is in [design/links.md](design/links.md).

| | Standalone | Link |
|---|---|---|
| Runs | continuously, pushing or polling a source | when an AI tool submits a job |
| Writes | documents into an ArangoDB collection | a JSONL result file in StorageV2 |
| State | sync cursors in `connector_state` | none (each job is self-contained) |
| Deployed with | Docker / Compose | Helm chart on the ArangoDB Platform (Kubernetes) |

Results are the same `Envelope` documents in both, one per JSONL line, so tools see one
shape whichever way the data arrived. Retention doesn't apply to link results
(`expires_at` is always `null`).

## How a job runs

```
AI tool ── POST /link/<name>/job {input, timeout} ──► Pending
link    ── claims it ──► Scheduled ──► Running ──► uploads result.0000000.jsonl ──► Completed
                                                └─ on error, invalid input or timeout ──► Failed (with the reason)
AI tool ── GET /link/<name>/job/<id> ──► status history, and the result path once Completed
```

The link registers its description, tags, input schema (one per operation) and output
schema with the platform when it starts. That is what AI tools read to know what to send.
Input that doesn't match the schema fails the job with the validation message.

## Available links

| Link | Operations | Reference |
|---|---|---|
| `gmail` | `search`, `get_thread`, `export` | [Gmail](sources/gmail.md#as-an-arangodb-platform-link) |
| `slack` | `channel_history`, `get_thread` | [Slack](sources/slack.md#as-an-arangodb-platform-link) |
| `jira` | `search`, `get_issue`, `export` | [Jira](sources/jira.md#as-an-arangodb-platform-link) |
| `selftest` | `echo` | [below](#the-selftest-link): no external source, for checking a deployment |

Each source's settings decide what jobs may touch: allowed mailboxes, channels or
projects, labels, and attachment limits. A job can only narrow that scope, never widen
it. Credentials come from Kubernetes Secrets, never from job input.

## Prerequisites

- An ArangoDB Platform with the **kube-arangodb Enterprise operator 1.4.5 or later**,
  with webhooks enabled (for sidecar injection) and the gateway feature on.
- An `ArangoDeployment` with a gateway, and an `ArangoPlatformStorage` (results are
  stored there).
- The links image, `arango-connector-link`, built from `packages/links/Dockerfile`
  (`make link-image`) and pushed to a registry the cluster can pull from.

For local development, `make platform-up` creates all of this on kind and Podman. See
[Local development](#local-development).

## Deploying a link

The chart, `charts/arango-connector-link`, deploys one link per release. It creates the
`ArangoPlatformLink`, an `ArangoRoute` at `/link/<release>/`, a Service, the Deployment
(with the sidecar-injection labels) and an `ArangoProfile` with the link's ID.

```bash
# Credentials as a Secret: token variables (envSecret) or files (credentialsSecret)
kubectl -n arango create secret generic jira-link-credentials \
  --from-literal=JIRA_API_TOKEN=...

helm install jira charts/arango-connector-link -n arango \
  --set link=jira \
  --set arangodb_platform.deployment.name=deployment \
  --set image.repository=registry.example.com/arango-connector-link --set image.tag=0.1.0 \
  --set envSecret=jira-link-credentials \
  --set env.JIRA_BASE_URL=https://your-site.atlassian.net \
  --set env.JIRA_EMAIL=connector-bot@example.com \
  --set env.JIRA_PROJECTS=PROJ
```

| Value | Default | Notes |
|---|---|---|
| `link` | `selftest` | `gmail`, `slack`, `jira` or `selftest` |
| `arangodb_platform.deployment.name` | required | The `ArangoDeployment` to attach to |
| `image.repository` / `image.tag` / `image.pullPolicy` | `arango-connector-link` / `0.1.0` / `IfNotPresent` | |
| `env` | `{}` | The source's settings, e.g. `GMAIL_MAILBOXES`. See each source's page |
| `envSecret` | `""` | Secret whose keys become environment variables (tokens) |
| `credentialsSecret` | `""` | Secret mounted read-only at `/secrets/<link>/` (key or token files) |
| `linkId` | derived | The UUID naming the link's job queue. Derived from the release name, so it is stable across upgrades |
| `linkSpec.extended` | `false` | Also put tags, schema and version on the `ArangoPlatformLink` (operators newer than 1.4.5 accept them) |
| `replicas` | `1` | More replicas share the queue safely; each job runs once |
| `resources` | small | Requests and limits for the link container |

Check it came up:

```bash
kubectl -n arango get arangoplatformlink,arangoroute
kubectl -n arango logs deploy/jira -c connector     # link_registered, link_ready
```

The link is `Ready` once it registers. The route can take about a minute to reach the
gateway after install, and until then `/link/<release>/` answers `404 unknown path`.

## Using a link

Requests go through the platform gateway, authenticated like any ArangoDB request. The
job's `input` is the operation's JSON, base64-encoded:

```bash
GATEWAY=https://your-platform:8529
INPUT=$(printf '{"operation":"get_issue","key":"PROJ-123"}' | base64)

curl -u user:password -X POST "$GATEWAY/link/jira/job" \
  -H 'Content-Type: application/json' -d "{\"input\":\"$INPUT\",\"timeout\":\"120s\"}"
# {"id":"0b0de28b-…"}

curl -u user:password "$GATEWAY/link/jira/job/0b0de28b-…"
# statuses newest first: JOB_STATE_COMPLETED "2 record(s) in result.0000000.jsonl", …
# "result": "/links/<link id>/<job id>/"
```

Other endpoints: `GET /link/<name>/job` lists jobs, `POST /link/<name>/job/<id>/cancel`
cancels one (a running job stops at its next record), and `GET /link/<name>/info` returns
the registered definition. The result file, `result.0000000.jsonl`, is read from the
platform's file storage (StorageV2) under the job's `result` path.

## The selftest link

`selftest` has no external source. Deploy it first to check that registration, the job
queue and result storage work, before configuring a real source.

<!-- BEGIN GENERATED: link:selftest -->
Diagnostic link with no external source: returns `count` message documents containing `text`, to check that jobs and results work on this platform.

Tags: `diagnostic`, `test`.

**`echo`**: ``count`` synthetic message documents containing ``text``.

| Field | Required | Default | Values | Description |
|---|---|---|---|---|
| `text` | no | `"hello from the connector"` | string | Each message's text. |
| `count` | no | `1` | integer ≥ 0, ≤ 10000 | Documents to return. |

Example: Two documents.

```json
{"operation": "echo", "text": "ping", "count": 2}
```
<!-- END GENERATED: link:selftest -->

## Local development

```bash
make platform-up     # kind + Podman: Enterprise operator, ArangoDB, platform storage
make link-e2e        # build, load, install selftest, run a job, verify the result
make platform-down
```

`make platform-up` needs a dev license in `ARANGO_LICENSE_KEY` (`client_id:client_secret`)
and a rootful Podman machine with at least 8 GB of RAM. To try a real link locally, build
and load the image (`make link-image`, then `make platform-load IMAGE=…`), create the
credentials Secret, and `helm install` as above with `image.pullPolicy=Never`.

## Operator 1.4.5 notes

The chart follows what kube-arangodb 1.4.5 accepts, which differs a little from the
published Links docs:

- **`ArangoPlatformLink` has no `tags`, `schema` or `version` fields.** The link registers
  them at startup instead. Set `linkSpec.extended=true` on newer operators.
- **The route's destination port is resolved by Service port name**, so it is
  `integration`, not `9193`.
- **The platform doesn't validate job input**; the link does, and fails invalid jobs.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `helm install` fails: `.spec.schema: field not declared` | `linkSpec.extended=true` on a 1.4.5 operator | Leave `linkSpec.extended` off |
| Pod has no `integration` container | Sidecar injection didn't happen | Check the operator's webhooks are enabled and the pod labels are intact |
| `ArangoRoute` `DestinationValid=False` | The route's port doesn't name a Service port | Use the chart's route unchanged (port `integration`) |
| `404 unknown path '/link/…'` | The route hasn't reached the gateway yet | Wait a minute; `GatewayConfig` on the `ArangoDeployment` turns `True` |
| Job `FAILED`: `invalid input: …` | The input doesn't match the operation's schema | Check the operation's fields below each source's reference |
| Job `FAILED`: `… is not one of this link's …` | The job asked for a mailbox, channel or project outside the link's scope | Add it to the link's settings, or ask within scope |
| Job `FAILED`: `internal error (…)` | An unexpected error | The link's logs (`-c connector`) have the details |
| Sidecar logs `link.v1=unhealthy` | The link hasn't registered | Check the connector container's logs |
