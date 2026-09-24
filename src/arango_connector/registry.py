"""Name -> factory maps for sources and sinks. This is the future plugin point.

TODO(future): discover additional sources/sinks via package entry points.
"""

from __future__ import annotations

from collections.abc import Callable

from arango_connector.config import Settings
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import SourceAdapter


def build_slack_adapter(settings: Settings) -> SourceAdapter:
    """Build the Slack Socket Mode adapter from settings."""
    # Imported lazily so other sources don't pay for (or require) slack_bolt.
    from arango_connector.sources.slack.adapter import SlackSocketModeAdapter

    return SlackSocketModeAdapter(
        bot_token=settings.slack_bot_token.get_secret_value(),
        app_token=settings.slack_app_token.get_secret_value(),
        channel_ids=settings.slack_channel_ids,
    )


def build_arango_sink(settings: Settings) -> Sink:
    """Build the ArangoDB sink from settings."""
    from arango_connector.sinks.arango import ArangoSink

    hosts = settings.arango_hosts
    return ArangoSink(
        hosts=hosts[0] if len(hosts) == 1 else hosts,
        username=settings.arango_username,
        password=settings.arango_password.get_secret_value(),
        db_name=settings.arango_db,
        collection_name=settings.arango_collection,
        retention_days=settings.retention_days,
    )


# TODO(future): register additional sources here, e.g. "discord", "teams".
SOURCES: dict[str, Callable[[Settings], SourceAdapter]] = {
    "slack": build_slack_adapter,
}

SINKS: dict[str, Callable[[Settings], Sink]] = {
    "arango": build_arango_sink,
}


def build_source(settings: Settings) -> SourceAdapter:
    """Look up and build the configured source."""
    try:
        factory = SOURCES[settings.source]
    except KeyError:
        raise ValueError(
            f"Unknown SOURCE={settings.source!r}; available: {', '.join(sorted(SOURCES))}"
        ) from None
    return factory(settings)


def build_sink(settings: Settings) -> Sink:
    """Look up and build the configured sink."""
    try:
        factory = SINKS[settings.sink]
    except KeyError:
        raise ValueError(
            f"Unknown SINK={settings.sink!r}; available: {', '.join(sorted(SINKS))}"
        ) from None
    return factory(settings)
