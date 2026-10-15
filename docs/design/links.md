# Design: ArangoDB Platform Links

Status: **agreed, in progress** (SE-211). Decisions marked *(agreed)* were settled with the
team; *(proposed)* ones are open for review in the PR.

## Context

The connector today is a **standalone** service: each process runs one source (Slack,
Gmail, Jira) continuously, by push or poll, and writes `Envelope` documents into an
ArangoDB collection. Configuration is environment variables and sync cursors live in a
`connector_state` collection.

The ArangoDB Platform has a framework for this kind of integration: **Links**
([overview](https://arangodb.github.io/kube-arangodb/docs/platform/links.html),
[building](https://arangodb.github.io/kube-arangodb/docs/platform/links/building.html),
[jobs](https://arangodb.github.io/kube-arangodb/docs/platform/links/jobs.html),
[API](https://arangodb.github.io/kube-arangodb/docs/platform/links/api.html),
[CRD](https://arangodb.github.io/kube-arangodb/docs/platform/links/crd.html)). A link is a
Kubernetes Deployment that AI tools discover through the platform. It declares what it
does (description, tags, input and output JSON Schemas, examples) and processes **jobs**
those tools submit:

```
AI tool ── POST /link/<name>/job {input} ──► platform validates input, queues job (Pending)
link pod ── PickUpJob ──► Scheduled ──► Running ──► upload results (JSONL) ──► Completed / Failed
AI tool ── GET /link/<name>/job/<id> ──► reads results from StorageV2 at /links/<link>/<job>/
```

An integration sidecar, injected by the operator, provides the job queue (MetaStore), file
storage (StorageV2) and authentication. The link talks to it over gRPC
(`LinkV1Internal`) or an HTTP gateway.

## Decisions

### Two implementations in one repo *(agreed)*

Both the standalone service and the Links implementation live on `main`, as packages of
one uv workspace. Source logic is written once and shared:

```
packages/
  core/         arango-connector-core      import arango_connector
                envelope, payloads, extract, retention, settings base, source catalogue,
                sources/<name>/: API clients, auth, settings, normalize, and operations
  standalone/   arango-connector           import arango_connector_standalone
                continuous runtime: polling/push adapters, pipeline, sinks, state store,
                maintenance CLI, entrypoint, Dockerfile, compose
  links/        arango-connector-links     import arango_connector_links
                Links runtime: register info, job loop, JSONL result writer, gRPC stubs,
                one entrypoint per link, Dockerfile
charts/<name>-link/                        Helm chart per link
tools/doctools.py                          docs generation and checks for every package
dev/platform.sh                            local platform for testing links
```

The three packages are versioned together. Standalone keeps its current distribution
name, CLI commands and environment variables, so existing deployments are unaffected.

### Links follow the current Links documentation exactly *(agreed)*

- **On-demand jobs only.** The framework defines one-off jobs and nothing scheduled or
  long-running. Continuous ingestion stays in standalone. If Links gain scheduled or
  long-running jobs, we revisit.
- **Results are JSONL files in StorageV2**, declared up front in the link's
  `result_files`. Links don't write to ArangoDB collections.
- **Stateless between jobs.** No sync cursors and no `StateStore` on the Links side;
  every job is self-contained.
- **gRPC client**, `LinkV1Internal` (the documented recommendation), with Python stubs
  generated from the kube-arangodb protos pinned to the operator release we test against
  (`integrations/link/v1/definition/*.proto`, `integrations/shared/v1`, at tag `1.4.5`).

### Operations *(agreed for Gmail and Slack; proposed for Jira)*

Every operation returns `Envelope` documents, one JSON object per JSONL line, the same
shape standalone writes (`expires_at` is always `null`, since retention doesn't apply).

| Link | Operation | Input (highlights) | Returns |
|---|---|---|---|
| gmail | `search` | `mailbox`, Gmail `query`, `max_results` | matching emails |
| gmail | `get_thread` | `mailbox`, `thread_id` | every email in the thread |
| gmail | `export` | `mailbox`, `labels`, `after`/`before` | emails in the range |
| slack | `channel_history` | `channel`, `oldest`/`latest` | messages in the range |
| slack | `get_thread` | `channel`, `thread_ts` | the root and its replies |
| jira | `search` | JQL `query`, `max_results` | matching issues |
| jira | `get_issue` | `key`, `include_comments` | the issue and its comments |
| jira | `export` | `project`, `updated_after`/`updated_before` | issues and comments in the range |

Gmail operations accept `attachments` (`off`, `metadata`, `content`, `text`, `all`),
capped by the link's configured limits; attachment envelopes go into the same JSONL.
Slack message search is excluded because Slack's search API needs a user token.

The input schema is one JSON Schema per link with an `operation` discriminator, generated
from pydantic models, so the platform validates input before a job is queued.

### Scoping and credentials *(agreed)*

What a job may touch is set by the operator in Helm values (allowed mailboxes and labels,
channels, projects). Job input can only narrow within that scope, and requests outside it
fail the job with a clear message. This matters most for Gmail, where domain-wide
delegation could otherwise reach any mailbox in the domain.

Credentials come from a Kubernetes Secret mounted into the link's Deployment, never from
job input.

### Shared operations in core *(agreed)*

Today each source's adapter owns its polling loop. Core gains bounded **operations** per
source (fetch these messages, this thread, this page of issues) that both runtimes call:

- standalone's adapters keep their loops and cursors, built on the operations, with no
  change in behaviour;
- each link maps a job's `operation` to the same functions and streams the envelopes to
  JSONL.

### Job execution *(proposed)*

1. On start, call `UpdateInfo` with the link's description, tags, schemas, examples and
   `result_files: ["result.0000000.jsonl"]`, retrying until the sidecar answers.
2. Loop: `PickUpJob` (backing off while the queue is empty), `GetJob`, set `RUNNING`,
   run the operation, stream envelopes into `BatchUploadFiles`, then set `COMPLETED`,
   or `FAILED` with a description.
3. Between pages, check the job's state and stop if it was cancelled; enforce the job's
   `timeout`.
4. Several replicas can run safely: pickup is atomic, and stuck jobs are released by the
   platform after a one-minute heartbeat timeout.

### Packaging *(agreed)*

One Helm chart, `charts/arango-connector-link`, parameterised by `link` (`gmail`,
`selftest`), following the documented layout: `ArangoPlatformLink` (`type: Active`),
`ArangoRoute` (`/link/<release>/` → `/_integration/connector/v1/`), a Service for the
route's destination, a Deployment with the sidecar-injection labels, and an
`ArangoProfile` setting `INTEGRATION_LINK_V1_CONNECTOR_ID`. That ID is a UUID the sidecar
uses to namespace the link's job queue; the chart derives a stable one from the release
name unless `linkId` is set. Each link's description, tags and input schema are generated
from the link class into `schemas/<link>.json` (`make docs-gen`; drift-checked in CI).
Images and charts are built in CI; publishing them to a registry is a follow-up.

The `selftest` link has no external source. It returns synthetic records, so a
deployment's wiring can be checked before configuring a real source.

### What kube-arangodb 1.4.5 actually does *(found on a real cluster)*

The published docs describe a slightly newer platform than the 1.4.5 operator we test
against. The chart and runtime follow what 1.4.5 accepts:

- **`ArangoPlatformLink` spec has only `type`, `deployment`, `route` and `description`.**
  `tags`, `schema` and `version` are rejected. The chart adds them only with
  `linkSpec.extended=true`. The link still registers its tags and schemas at startup
  through `UpdateInfo`, which is what AI tools read.
- **`ArangoRoute` resolves `destination.service.port` (a string) by port name**, so the
  route points at the Service's `integration` port, not `9193`.
- **The platform does not validate job input against the schema**, so the link always
  validates it itself and fails the job with the validation message.
- **Route changes reach the gateway after a short delay** (mounted config maps sync in
  about a minute), while the deployment shows `GatewayConfig=False`.

### Docs *(agreed)*

`tools/doctools.py` stays at the root and covers every package. Each source page gains a
Links section whose operations table, input and output schemas and examples are
**generated from the same metadata the link registers with the platform**, so the docs and
the wiki show exactly what AI tools see.

## Testing

- Unit tests per package. The Links runtime is tested against an in-process fake of the
  `LinkV1Internal` gRPC service.
- `make platform-up` (`dev/platform.sh`) creates a local kind cluster on Podman with the
  Enterprise operator, ArangoDB and platform storage, using a dev license from
  `ARANGO_LICENSE_KEY`.
- `make link-e2e` (`dev/link-e2e.sh`) builds the links image, loads it into the cluster,
  installs the chart with the `selftest` link, submits a job through the gateway as an
  AI tool would, waits for `Completed`, and verifies the JSONL result in StorageV2.

## Out of scope

New sources, publishing images and charts to a registry, the Gmail web OAuth flow
(SE-138), and continuous ingestion through Links.
