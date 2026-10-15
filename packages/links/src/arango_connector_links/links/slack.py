"""Slack as a Link: read channel history and threads on demand.

Uses the Slack Web API with the bot token (no Socket Mode app token). ``SLACK_CHANNEL_IDS``
lists the channels jobs may read; the bot must be a member of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from itertools import islice
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slack_sdk.errors import SlackApiError

from arango_connector.envelope import Envelope
from arango_connector.sources.slack.config import SlackApiSettings
from arango_connector.sources.slack.operations import (
    SlackApi,
    UserNames,
    channel_history,
    message_envelopes,
    thread_replies,
)
from arango_connector_links.link import Example, JobContext, JobError, Link


class ChannelHistory(BaseModel):
    """A channel's messages in a time range, newest first."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["channel_history"]
    channel: str = Field(min_length=1, description="Channel ID; must be one of the link's.")
    oldest: datetime | None = Field(default=None, description="Messages at or after this time.")
    latest: datetime | None = Field(default=None, description="Messages at or before this time.")
    max_results: int = Field(default=1000, ge=1, le=10000, description="Messages to return.")


class GetThread(BaseModel):
    """A thread's root message and every reply."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["get_thread"]
    channel: str = Field(min_length=1, description="Channel ID; must be one of the link's.")
    thread_ts: str = Field(min_length=1, description="The thread root message's `ts`.")


SlackInput = Annotated[ChannelHistory | GetThread, Field(discriminator="operation")]

# Slack errors worth reporting as-is: the job's input or the bot's access is the problem.
_CALLER_ERRORS = {
    "channel_not_found": "channel not found",
    "not_in_channel": "the bot is not a member of this channel",
    "thread_not_found": "thread not found",
    "missing_scope": "the Slack app lacks a scope it needs (channels:history, groups:history)",
}


class SlackLink(Link[ChannelHistory | GetThread]):
    name = "slack"
    description = (
        "Read Slack messages: a channel's history in a time range, or a whole thread. "
        "Returns one document per message, with authors' names, as JSONL."
    )
    tags = ("chat", "slack", "messages")
    input_type = SlackInput
    examples = (
        Example(
            "channel_history",
            {
                "operation": "channel_history",
                "channel": "C0123ABC",
                "oldest": "2026-10-01T00:00:00Z",
            },
            "Messages in the channel since 1 October.",
        ),
        Example(
            "get_thread",
            {"operation": "get_thread", "channel": "C0123ABC", "thread_ts": "1716500000.123456"},
            "The thread's root message and replies.",
        ),
    )

    def __init__(self, settings: SlackApiSettings, client_factory: Callable[[], Any]) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._client: Any = None
        self._team_id: str | None = None
        self._names: UserNames | None = None

    def run(
        self, job_input: ChannelHistory | GetThread, ctx: JobContext
    ) -> Iterator[Envelope[Any]]:
        if job_input.channel not in self.settings.slack_channel_ids:
            raise JobError(f"channel {job_input.channel!r} is not one of this link's channels")
        client = self._slack()
        assert self._names is not None
        envelopes: Iterator[Envelope[Any]]
        try:
            if isinstance(job_input, ChannelHistory):
                messages = channel_history(
                    client,
                    job_input.channel,
                    oldest=job_input.oldest.timestamp() if job_input.oldest else None,
                    latest=job_input.latest.timestamp() if job_input.latest else None,
                )
                envelopes = islice(
                    message_envelopes(messages, job_input.channel, self._team_id, self._names),
                    job_input.max_results,
                )
            else:
                messages = thread_replies(client, job_input.channel, job_input.thread_ts)
                envelopes = message_envelopes(
                    messages, job_input.channel, self._team_id, self._names
                )
            for envelope in envelopes:
                ctx.check()
                yield envelope
        except SlackApiError as exc:
            error = str(exc.response.get("error", ""))
            if error in _CALLER_ERRORS:
                raise JobError(_CALLER_ERRORS[error]) from None
            raise

    def _slack(self) -> SlackApi:
        if self._client is None:
            client = self._client_factory()
            self._team_id = (client.auth_test() or {}).get("team_id")
            self._names = UserNames(client.users_info)
            self._client = client
        result: SlackApi = self._client
        return result
