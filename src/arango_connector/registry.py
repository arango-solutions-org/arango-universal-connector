"""Name -> factory maps for sources and sinks. This is the future plugin point.

A source factory receives the core settings and the state store, and loads its own
settings class with ``load_settings``, so its environment variables are only required
when that source is selected.

TODO(future): discover additional sources/sinks via package entry points.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arango_connector.config import CoreSettings, load_settings
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import SourceAdapter
from arango_connector.state.base import StateStore

SourceFactory = Callable[[CoreSettings, StateStore], SourceAdapter]


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


SinkFactory = Callable[[CoreSettings], Sink]


def build_slack_adapter(settings: CoreSettings, state: StateStore) -> SourceAdapter:
    """Build the Slack Socket Mode adapter. Slack keeps no state; ``state`` is unused."""
    # Imported lazily so other sources don't pay for (or require) slack_bolt.
    from arango_connector.sources.slack.adapter import SlackSocketModeAdapter
    from arango_connector.sources.slack.config import SlackSettings

    slack = load_settings(SlackSettings)
    return SlackSocketModeAdapter(
        bot_token=slack.slack_bot_token.get_secret_value(),
        app_token=slack.slack_app_token.get_secret_value(),
        channel_ids=slack.slack_channel_ids,
    )


def build_gmail_adapter(settings: CoreSettings, state: StateStore) -> SourceAdapter:
    """Build the Gmail polling adapter. Its cursors live in ``state``."""
    from arango_connector.sources.gmail.adapter import GmailPollingAdapter
    from arango_connector.sources.gmail.auth import build_credentials
    from arango_connector.sources.gmail.client import GmailClient, GoogleGmailClient
    from arango_connector.sources.gmail.config import GmailSettings
    from arango_connector.sources.gmail.normalize import AttachmentOptions, NormalizeOptions

    gmail = load_settings(GmailSettings)

    def client_factory(mailbox: str) -> GmailClient:
        return GoogleGmailClient(
            build_credentials(
                gmail.gmail_auth_mode,
                mailbox,
                service_account_file=gmail.gmail_service_account_file,
                token_dir=gmail.gmail_oauth_token_dir,
            )
        )

    return GmailPollingAdapter(
        gmail.gmail_mailboxes,
        client_factory,
        state,
        include_labels=gmail.gmail_labels,
        exclude_labels=gmail.gmail_exclude_labels,
        options=NormalizeOptions(
            key_mode=gmail.gmail_key_mode,
            store_body=gmail.gmail_store_body,
            store_bcc=gmail.gmail_store_bcc,
            raw_mode=gmail.gmail_raw_mode,
        ),
        attachments=AttachmentOptions(
            mode=gmail.gmail_attachments,
            max_bytes=gmail.gmail_attachment_max_bytes,
            text_max_chars=gmail.gmail_attachment_text_max_chars,
        ),
        initial_sync=gmail.gmail_initial_sync,
        initial_lookback=gmail.gmail_initial_lookback,
        label_match=gmail.gmail_label_match,
        interval_seconds=gmail.gmail_poll_interval_seconds,
    )


def build_jira_adapter(settings: CoreSettings, state: StateStore) -> SourceAdapter:
    """Build the Jira Cloud polling adapter. Its cursors live in ``state``."""
    from arango_connector.sources.jira.adapter import JiraPollingAdapter
    from arango_connector.sources.jira.client import JiraClient, RequestsJiraClient
    from arango_connector.sources.jira.config import JiraSettings

    jira = load_settings(JiraSettings)

    def client_factory(should_stop: Callable[[], bool]) -> JiraClient:
        return RequestsJiraClient(
            jira.jira_base_url,
            jira.jira_email,
            jira.jira_api_token.get_secret_value(),
            page_size=jira.jira_page_size,
            should_stop=should_stop,
        )

    return JiraPollingAdapter(
        jira.jira_projects,
        client_factory,
        state,
        site=jira.jira_site,
        base_url=jira.jira_base_url,
        initial_sync=jira.jira_initial_sync,
        initial_lookback=jira.jira_initial_lookback,
        interval_seconds=jira.jira_poll_interval_seconds,
    )


def build_arango_sink(settings: CoreSettings) -> Sink:
    """Build the ArangoDB sink from settings."""
    from arango_connector.sinks.arango import ArangoSink

    return ArangoSink(
        hosts=settings.arango_hosts,
        username=settings.arango_username,
        password=settings.arango_password.get_secret_value(),
        db_name=settings.arango_db,
        collection_name=settings.arango_collection,
        retention=settings.retention_policy(),
    )


def build_state_store(settings: CoreSettings) -> StateStore:
    """Build the state store. It lives in ArangoDB alongside the envelopes."""
    from arango_connector.state.arango import ArangoStateStore

    return ArangoStateStore(
        hosts=settings.arango_hosts,
        username=settings.arango_username,
        password=settings.arango_password.get_secret_value(),
        db_name=settings.arango_db,
        collection_name=settings.state_collection,
    )


# TODO(future): register additional sources here, e.g. "teams", "sharepoint".
SOURCES: dict[str, SourceFactory] = {
    "slack": build_slack_adapter,
    "gmail": build_gmail_adapter,
    "jira": build_jira_adapter,
}

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

SINKS: dict[str, SinkFactory] = {
    "arango": build_arango_sink,
}


def build_source(settings: CoreSettings, state: StateStore) -> SourceAdapter:
    """Look up and build the configured source. Raises ``ConfigError`` on bad settings."""
    try:
        factory = SOURCES[settings.source]
    except KeyError:
        raise ValueError(
            f"Unknown SOURCE={settings.source!r}; available: {', '.join(sorted(SOURCES))}"
        ) from None
    return factory(settings, state)


def build_sink(settings: CoreSettings) -> Sink:
    """Look up and build the configured sink."""
    try:
        factory = SINKS[settings.sink]
    except KeyError:
        raise ValueError(
            f"Unknown SINK={settings.sink!r}; available: {', '.join(sorted(SINKS))}"
        ) from None
    return factory(settings)
