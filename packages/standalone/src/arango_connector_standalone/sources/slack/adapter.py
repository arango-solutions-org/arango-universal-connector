"""Slack Socket Mode source adapter."""

from __future__ import annotations

import logging
import threading
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from arango_connector.sources.slack.normalize import normalize
from arango_connector.sources.slack.operations import UserNames
from arango_connector_standalone.sources.base import EmitFn, SourceAdapter

logger = logging.getLogger(__name__)


class SlackSocketModeAdapter(SourceAdapter):
    """Listens to Slack ``message`` events over Socket Mode for an allowlist of channels.

    TODO(future): historical backfill via ``conversations.history`` with per-channel
    checkpoints would run before (or alongside) the live listener here.
    TODO(future): OAuth install flow / multi-workspace support and the HTTP Events API.
    """

    name = "slack"

    def __init__(self, bot_token: str, app_token: str, channel_ids: set[str]) -> None:
        self.channel_ids = frozenset(channel_ids)
        self._app_token = app_token
        self._emit: EmitFn | None = None
        self._handler: SocketModeHandler | None = None
        self._stopped = threading.Event()
        # Every write replaces the whole document, so events must be handled in the order
        # Slack sent them: listeners run inline (here) on one Socket Mode worker (start()).
        self.app = App(token=bot_token, process_before_response=True)
        self.app.event("message")(self._on_message)
        # Looked up through self.app.client at call time, so the client can be replaced.
        self._user_names = UserNames(lambda **kwargs: self.app.client.users_info(**kwargs))

    def _on_message(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        """Bolt listener. Catches everything so one bad event never kills the listener."""
        try:
            channel = event.get("channel")
            if channel not in self.channel_ids:
                logger.debug("slack_event_ignored reason=unwatched_channel channel=%s", channel)
                return
            envelope = normalize(event, body.get("team_id"))
            if envelope is None:
                logger.debug(
                    "slack_event_skipped channel=%s subtype=%s", channel, event.get("subtype")
                )
                return
            envelope = self._user_names.fill(envelope)
            if self._emit is None:
                logger.warning("slack_event_dropped reason=not_started key=%s", envelope.key)
                return
            self._emit(envelope)
        except Exception:
            logger.exception(
                "slack_event_handler_failed channel=%s ts=%s", event.get("channel"), event.get("ts")
            )

    def start(self, emit: EmitFn) -> None:
        """Connect over Socket Mode and block until :meth:`stop` is called.

        ``SocketModeHandler.start()`` blocks on an internal event nothing can set,
        so we ``connect()`` and block on our own event instead, which lets
        :meth:`stop` shut down cleanly.
        """
        self._emit = emit
        logger.info(
            "slack_adapter_starting watched_channels=%s", ",".join(sorted(self.channel_ids))
        )
        self._handler = SocketModeHandler(self.app, self._app_token, concurrency=1)
        self._handler.connect()
        logger.info("slack_adapter_connected")
        # Wake periodically so signal handlers on the main thread get a chance to run.
        while not self._stopped.wait(timeout=0.5):
            pass
        logger.info("slack_adapter_stopped")

    def stop(self) -> None:
        """Close the Socket Mode connection and unblock :meth:`start`."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:
                logger.exception("slack_handler_close_failed")
