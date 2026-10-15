"""The source catalogue: what each source is, for docs and tooling.

Shared by every runtime. Each runtime keeps its own way of building a source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceInfo:
    """What a source is, for docs and tooling. Every entry in ``SOURCES`` has one."""

    title: str
    summary: str
    kinds: tuple[str, ...]
    delivery: str
    # "module:Class" of its settings class, imported only when docs are generated.
    settings: str
    # Its page under docs/sources/.
    doc: str


SOURCE_INFO: dict[str, SourceInfo] = {
    "slack": SourceInfo(
        title="Slack",
        summary="Messages, thread replies and edits from allowlisted channels, in real time.",
        kinds=("message",),
        delivery="push (Socket Mode)",
        settings="arango_connector.sources.slack.config:SlackSettings",
        doc="slack.md",
    ),
    "gmail": SourceInfo(
        title="Gmail",
        summary="Email from selected mailboxes and labels, incrementally, with optional "
        "attachments and text extraction.",
        kinds=("email", "attachment"),
        delivery="poll (Gmail history API)",
        settings="arango_connector.sources.gmail.config:GmailSettings",
        doc="gmail.md",
    ),
    "jira": SourceInfo(
        title="Jira",
        summary="Issues and comments from selected Jira Cloud projects, incrementally.",
        kinds=("issue", "comment"),
        delivery="poll (incremental JQL)",
        settings="arango_connector.sources.jira.config:JiraSettings",
        doc="jira.md",
    ),
}
