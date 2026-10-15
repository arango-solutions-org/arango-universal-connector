"""Slack operations shared by every runtime: resolving authors' display names.

The standalone Socket Mode listener and Links jobs both turn Slack messages into
envelopes with ``normalize`` and fill in human authors' names with ``UserNames``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from arango_connector.envelope import Envelope
from arango_connector.payloads import MessagePayload
from arango_connector.sources.slack.normalize import normalize

logger = logging.getLogger(__name__)

# Slack's users.info, e.g. ``WebClient.users_info``: called as ``users_info(user=<id>)``.
UsersInfo = Callable[..., Any]


class UserNames:
    """``real_name`` per user ID via ``users.info``, cached for the object's lifetime.

    A failed lookup returns ``None`` without caching, so the message is still stored and
    the next message from that user retries.

    TODO(future): refresh on "user_change" events; a renamed user keeps the old name
    until the cache is rebuilt.
    """

    def __init__(self, users_info: UsersInfo) -> None:
        self._users_info = users_info
        self._names: dict[str, str | None] = {}

    def get(self, user_id: str) -> str | None:
        if user_id in self._names:
            return self._names[user_id]
        try:
            response = self._users_info(user=user_id)
        except SlackApiError as exc:
            logger.warning(
                "slack_user_lookup_failed user=%s error=%s", user_id, exc.response.get("error")
            )
            return None
        except Exception:
            logger.warning("slack_user_lookup_failed user=%s", user_id, exc_info=True)
            return None
        user = response.get("user") or {}
        name = user.get("real_name") or (user.get("profile") or {}).get("real_name") or None
        self._names[user_id] = name
        return name

    def fill(self, envelope: Envelope[MessagePayload]) -> Envelope[MessagePayload]:
        """``envelope`` with a human author's name filled in (bots carry their own)."""
        payload = envelope.payload
        if payload.author_type != "user" or not payload.author_id:
            return envelope
        name = self.get(payload.author_id)
        return envelope.model_copy(
            update={"payload": payload.model_copy(update={"author_name": name})}
        )


class SlackApi(Protocol):
    """The Slack Web API methods used to read history (``slack_sdk.WebClient``)."""

    def conversations_history(self, **kwargs: Any) -> Any: ...

    def conversations_replies(self, **kwargs: Any) -> Any: ...


_PAGE_SIZE = 200


def _pages(call: Callable[..., Any], **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Every message across Slack's cursor pages."""
    cursor: str | None = None
    while True:
        response = call(**kwargs, limit=_PAGE_SIZE, **({"cursor": cursor} if cursor else {}))
        yield from response.get("messages") or []
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return


def channel_history(
    client: SlackApi, channel: str, *, oldest: float | None = None, latest: float | None = None
) -> Iterator[dict[str, Any]]:
    """A channel's top-level messages, newest first, within ``oldest``..``latest`` (epoch)."""
    bounds: dict[str, Any] = {"inclusive": True}
    if oldest is not None:
        bounds["oldest"] = f"{oldest:.6f}"
    if latest is not None:
        bounds["latest"] = f"{latest:.6f}"
    yield from _pages(client.conversations_history, channel=channel, **bounds)


def thread_replies(client: SlackApi, channel: str, thread_ts: str) -> Iterator[dict[str, Any]]:
    """A thread's root message and its replies, oldest first."""
    yield from _pages(client.conversations_replies, channel=channel, ts=thread_ts)


def message_envelopes(
    messages: Iterable[dict[str, Any]], channel: str, team_id: str | None, names: UserNames
) -> Iterator[Envelope[MessagePayload]]:
    """Envelopes for messages read through the Web API.

    History messages don't carry their channel, so it is added before ``normalize``; the
    same subtypes are kept or skipped as for live events.
    """
    for message in messages:
        event = {**message, "type": message.get("type") or "message", "channel": channel}
        envelope = normalize(event, team_id)
        if envelope is not None:
            yield names.fill(envelope)
