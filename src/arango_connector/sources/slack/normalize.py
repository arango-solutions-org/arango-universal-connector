"""Pure mapping from a Slack ``message`` event to an Envelope. No I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arango_connector.envelope import Envelope, make_key

SOURCE = "slack"

# Subtypes that represent a genuinely new message. Everything else is skipped.
# TODO(future): handle "message_changed" (edits) and "message_deleted" (deletes).
# TODO(future): handle "file_share" and reactions once files/reactions are in scope.
INGESTED_SUBTYPES = frozenset({"bot_message", "thread_broadcast"})

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def slack_ts_to_datetime(ts: str) -> datetime:
    """Convert a Slack ``ts`` (``"<seconds>.<micros>"``) to an aware UTC datetime.

    Parses the integer and fractional parts separately so no precision is lost
    to float rounding.
    """
    seconds, _, fraction = ts.partition(".")
    micros = int((fraction + "000000")[:6]) if fraction else 0
    return _EPOCH + timedelta(seconds=int(seconds), microseconds=micros)


def normalize(event: dict[str, Any], team_id: str | None) -> Envelope | None:
    """Map a Slack event to an Envelope, or return ``None`` if it should be skipped.

    Skips non-``message`` events, subtypes other than ``bot_message`` /
    ``thread_broadcast`` (edits, deletes, joins, ...) and events missing ``ts``
    or ``channel``.
    """
    if event.get("type") != "message":
        return None

    subtype = event.get("subtype")
    if subtype is not None and subtype not in INGESTED_SUBTYPES:
        return None

    ts = event.get("ts")
    channel_id = event.get("channel")
    if not ts or not channel_id:
        return None

    # Classic bot messages use subtype "bot_message"; app bots posting with a bot
    # token have no subtype but carry a "bot_id".
    is_bot = subtype == "bot_message" or bool(event.get("bot_id"))
    author_id = event.get("bot_id") if is_bot else event.get("user")

    thread_ts = event.get("thread_ts")
    thread_parent = thread_ts if thread_ts and thread_ts != ts else None

    return Envelope(
        key=make_key(SOURCE, channel_id, ts),
        source=SOURCE,
        workspace_id=team_id or event.get("team"),
        channel_id=channel_id,
        author_id=author_id,
        author_type="bot" if is_bot else "user",
        text=event.get("text") or "",
        source_ts=ts,
        created_at=slack_ts_to_datetime(ts),
        thread_parent=thread_parent,
        raw=event,
    )
