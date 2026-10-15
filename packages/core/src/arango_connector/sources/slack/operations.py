"""Slack operations shared by every runtime: resolving authors' display names.

The standalone Socket Mode listener and Links jobs both turn Slack messages into
envelopes with ``normalize`` and fill in human authors' names with ``UserNames``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from slack_sdk.errors import SlackApiError

from arango_connector.envelope import Envelope
from arango_connector.payloads import MessagePayload

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
