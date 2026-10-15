# Docs

Shared behaviour (configuration, retention, the document envelope, development) is in
the [main README](../README.md). Each source has its own page covering setup,
configuration, how sync works, its document shape, privacy and troubleshooting.

## Sources

<!-- BEGIN GENERATED: catalogue -->
| Source | `SOURCE=` | Kinds | Standalone delivery | As a Link | What it ingests | Docs |
|---|---|---|---|---|---|---|
| Slack | `slack` | `message` | push (Socket Mode) | `channel_history`, `get_thread` | Messages, thread replies and edits from allowlisted channels, in real time. | [Slack docs](sources/slack.md) |
| Gmail | `gmail` | `email`, `attachment` | poll (Gmail history API) | `export`, `get_thread`, `search` | Email from selected mailboxes and labels, incrementally, with optional attachments and text extraction. | [Gmail docs](sources/gmail.md) |
| Jira | `jira` | `issue`, `comment` | poll (incremental JQL) | `export`, `get_issue`, `search` | Issues and comments from selected Jira Cloud projects, incrementally. | [Jira docs](sources/jira.md) |
<!-- END GENERATED: catalogue -->

## Links

Every source can also run as an ArangoDB Platform Link, answering on-demand jobs from AI
tools instead of ingesting continuously. [links.md](links.md) covers deploying and using
them; each source page has its link's operations. The design is in
[design/links.md](design/links.md).

## Adding a source

Follow "Adding a new source or sink" in the [main README](../README.md#adding-a-new-source-or-sink),
copy [sources/_template.md](sources/_template.md) to `sources/<name>.md`, fill in every
section, and add a row to the table above and to the sources table in the main README.

## How these docs are built

`docs/` and the main `README.md` are the **source of truth**. They are reviewed in PRs
together with the code. The [GitHub wiki](https://github.com/arango-solutions-org/arango-universal-connector/wiki)
is a **generated copy**: every merge to `main` republishes it (`.github/workflows/wiki.yml`),
overwriting any direct edits. Change things here, not in the wiki.

Some parts are generated from the code so they can't drift:

| Generated content | From |
|---|---|
| Configuration tables (core and per source) | the settings classes: field names, defaults, allowed values and `description`s |
| The source catalogue (the table above, and in the main README) | `SOURCE_INFO` in core's `catalog.py` |
| Link references (operations, input fields, examples) and the chart's `schemas/` | each link's input models and examples |

They sit between `<!-- BEGIN GENERATED: … -->` and `<!-- END GENERATED: … -->` markers.
Don't edit inside them; change the code and run:

```bash
make docs-gen     # rewrite the generated regions
make docs-check   # what CI runs: stale regions, .env.example gaps, broken links/anchors
make wiki-build   # render the wiki pages locally into build/wiki to preview them
```

`make check`, and so CI on every PR, includes `docs-check`. A new setting without a
`description`, a setting missing from `.env.example`, or a stale table fails the build.

