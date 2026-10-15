"""Entrypoint: ``arango-connector-link <name>`` runs one link until signalled."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any

import grpc

from arango_connector.config import ConfigError, load_settings
from arango_connector_links._pb.integrations.link.v1.definition import internal_pb2_grpc
from arango_connector_links.link import Link
from arango_connector_links.runtime import LinkRuntime
from arango_connector_links.settings import LinkSettings

logger = logging.getLogger("arango_connector_links")


def build_gmail() -> Link[Any]:
    from arango_connector.sources.gmail.auth import build_credentials
    from arango_connector.sources.gmail.client import GmailClient, GoogleGmailClient
    from arango_connector.sources.gmail.config import GmailSettings
    from arango_connector_links.links.gmail import GmailLink

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

    return GmailLink(gmail, client_factory)


def build_slack() -> Link[Any]:
    from slack_sdk import WebClient
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

    from arango_connector.sources.slack.config import SlackApiSettings
    from arango_connector_links.links.slack import SlackLink

    slack = load_settings(SlackApiSettings)

    def client_factory() -> Any:
        client = WebClient(token=slack.slack_bot_token.get_secret_value())
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
        return client

    return SlackLink(slack, client_factory)


def build_jira() -> Link[Any]:
    from arango_connector.sources.jira.client import JiraClient, RequestsJiraClient
    from arango_connector.sources.jira.config import JiraSettings
    from arango_connector_links.links.jira import JiraLink

    jira = load_settings(JiraSettings)

    def client_factory() -> JiraClient:
        return RequestsJiraClient(
            jira.jira_base_url,
            jira.jira_email,
            jira.jira_api_token.get_secret_value(),
            page_size=jira.jira_page_size,
        )

    return JiraLink(jira, client_factory)


def build_selftest() -> Link[Any]:
    from arango_connector_links.links.selftest import SelftestLink

    return SelftestLink()


def link_types() -> dict[str, type[Link[Any]]]:
    """Every link's class, for docs and chart generation (no settings needed)."""
    from arango_connector_links.links.gmail import GmailLink
    from arango_connector_links.links.jira import JiraLink
    from arango_connector_links.links.selftest import SelftestLink
    from arango_connector_links.links.slack import SlackLink

    return {cls.name: cls for cls in (GmailLink, SlackLink, JiraLink, SelftestLink)}


LINKS: dict[str, Callable[[], Link[Any]]] = {
    "gmail": build_gmail,
    "slack": build_slack,
    "jira": build_jira,
    "selftest": build_selftest,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arango-connector-link", description="Run a source as an ArangoDB Platform Link."
    )
    parser.add_argument("link", choices=sorted(LINKS))
    args = parser.parse_args(argv)

    try:
        settings = load_settings(LinkSettings)
        link = LINKS[args.link]()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        stream=sys.stdout,
    )

    channel = grpc.insecure_channel(settings.integration_service_address)
    runtime = LinkRuntime(
        link,
        internal_pb2_grpc.LinkV1InternalStub(channel),
        idle_wait=settings.link_idle_wait_seconds,
        cancel_check_interval=settings.link_cancel_check_seconds,
    )

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("signal_received signal=%s", signal.Signals(signum).name)
        runtime.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    logger.info("link_starting link=%s sidecar=%s", link.name, settings.integration_service_address)
    try:
        runtime.run_forever()
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
