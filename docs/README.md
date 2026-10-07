# Docs

Shared behaviour (configuration, retention, the document envelope, development) is in
the [main README](../README.md). Each source has its own page covering setup,
configuration, how sync works, its document shape, privacy and troubleshooting.

## Sources

| Source | `SOURCE=` | Kind | Delivery | Page |
|---|---|---|---|---|
| Slack | `slack` | `message` | push (Socket Mode) | [sources/slack.md](sources/slack.md) |
| Gmail | `gmail` | `email`, `attachment` | poll (Gmail history API) | [sources/gmail.md](sources/gmail.md) |

## Adding a source

Follow "Adding a new source or sink" in the [main README](../README.md#adding-a-new-source-or-sink),
copy [sources/_template.md](sources/_template.md) to `sources/<name>.md`, fill in every
section, and add a row to the table above and to the sources table in the main README.
