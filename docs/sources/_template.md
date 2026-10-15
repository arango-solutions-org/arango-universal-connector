# <Source name> source

<!--
Copy this file to docs/sources/<name>.md when adding a source, fill in every section,
and add a row to the sources table in README.md and docs/README.md. Delete these
comments. Keep shared behaviour (retention, dead-letter, the Envelope top level) in the
main README and link to it rather than repeating it.
-->

`SOURCE=<name>`. One or two sentences: what it ingests, push or poll, and how quickly
new items arrive.

| | |
|---|---|
| Kind | `message` / `email` / `document` / ... (see `payloads.py`) |
| Delivery | push (…) / poll every `<NAME>_POLL_INTERVAL_SECONDS` |
| Keys | `<name>_{...}_{...}`, deterministic, so re-delivery overwrites |
| Cursor | none / what is stored in `connector_state` |
| Access | the scopes or permissions requested, and that they are the minimum |

## Setup

Step by step, from a fresh account to working credentials: what to create in the
provider's console, which scopes or permissions to grant (exactly), where to put the
resulting secrets, and which variables point at them. Cover each auth mode if there are
several, and say which to choose when.

## Configuration

<!-- Generated: give every field of your settings class a description, add the source to
SOURCE_INFO in core's catalog.py, then put this region here and run `make docs-gen`:

BEGIN GENERATED: config:<name>
END GENERATED: config:<name>

(as HTML comments, exactly like the other source pages). -->

Below the table, note any combinations that are validated at startup.

## Running

Local (`uv run python -m arango_connector_standalone.main` with `SOURCE=<name>`) and Docker
(Compose service or profile, secrets mounts, file permissions for uid 10001).

## How sync works

- **Cursor / resume:** what is stored, when it advances, and the delivery guarantee.
- **What is read:** new items, edits or updates, and how updates map onto stored
  documents (`updated_at`).
- **What is not removed:** deletes and moves.
- **Gaps and first start:** what happens after downtime, and on the very first run.
- **Failures:** isolation, retries and backoff, and the log events to watch.

## Document shape

The `scope` mapping (`tenant_id`, `container_id`, `path`), a full example document, and
notes on any field whose meaning is not obvious. The shared top-level fields are
described in the main README's "Document shape".

## As an ArangoDB Platform Link

If the source has a link: what jobs can do, which settings scope them (and which are
ignored), and how credentials are passed (`envSecret` or `credentialsSecret`). Link to
[Links](../links.md) for deployment. Then add the generated reference:
`<!-- BEGIN GENERATED: link:<name> -->` and `<!-- END GENERATED: link:<name> -->` markers,
filled in by `make docs-gen`.

## Privacy and security

What sensitive data the source can hold, the redaction options, and recommended
collection and user setup.

## Troubleshooting

| Symptom / log event | Cause | Fix |
|---|---|---|

## Not supported yet

What is deliberately out of scope, with `TODO(future)` pointers.
