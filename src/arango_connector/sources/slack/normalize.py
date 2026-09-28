"""Pure mapping from a Slack ``message`` event to an Envelope. No I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arango_connector.envelope import Envelope, make_key

SOURCE = "slack"

# Subtypes that represent a genuinely new message. Everything else is skipped.
# TODO(future): handle "message_deleted" (deletes); needs a delete op on Envelope/Sink.
# TODO(future): handle "file_share" and reactions once files/reactions are in scope.
INGESTED_SUBTYPES = frozenset({"bot_message", "thread_broadcast"})

EDIT_SUBTYPE = "message_changed"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def slack_ts_to_datetime(ts: str) -> datetime:
    """Convert a Slack ``ts`` (``"<seconds>.<micros>"``) to an aware UTC datetime.

    Parses the integer and fractional parts separately so no precision is lost
    to float rounding.
    """
    seconds, _, fraction = ts.partition(".")
    micros = int((fraction + "000000")[:6]) if fraction else 0
    return _EPOCH + timedelta(seconds=int(seconds), microseconds=micros)


def _flatten_edit(event: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a ``message_changed`` event into the plain message event it now represents.

    Returns a copy carrying the edited message's own ``ts``, so the Envelope keys to
    the document already stored for the original. ``previous_message`` is deliberately
    dropped: it holds the pre-edit text, which is the content the edit removed.
    """
    inner = event.get("message")
    if not isinstance(inner, dict):
        return None

    flattened = dict(inner)
    flattened["type"] = "message"
    flattened["channel"] = event.get("channel")
    # The workspace only appears on the outer event for some payloads, and a replace
    # write would otherwise clear the workspace_id stored with the original message.
    if "team" not in flattened and "team" in event:
        flattened["team"] = event["team"]
    return flattened


def _bot_name(event: dict[str, Any]) -> str | None:
    """Classic bot messages carry ``username``; app bots carry ``bot_profile.name``."""
    profile = event.get("bot_profile")
    profile_name = profile.get("name") if isinstance(profile, dict) else None
    return event.get("username") or profile_name or None


def normalize(event: dict[str, Any], team_id: str | None) -> Envelope | None:
    """Map a Slack event to an Envelope, or return ``None`` if it should be skipped.

    ``message_changed`` is mapped onto the original message's key so an edit overwrites
    what is stored. Skips non-``message`` events, subtypes other than ``bot_message`` /
    ``thread_broadcast`` (deletes, joins, ...) and events missing ``ts`` or ``channel``.
    """
    if event.get("type") != "message":
        return None

    subtype = event.get("subtype")
    if subtype == EDIT_SUBTYPE:
        flattened = _flatten_edit(event)
        if flattened is None:
            return None
        event = flattened
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
    # Human names need a users.info lookup, which the adapter does; bots carry theirs.
    author_name = _bot_name(event) if is_bot else None

    thread_ts = event.get("thread_ts")
    thread_parent = thread_ts if thread_ts and thread_ts != ts else None

    edited = event.get("edited")
    edited_ts = edited.get("ts") if isinstance(edited, dict) else None

    return Envelope(
        key=make_key(SOURCE, channel_id, ts),
        source=SOURCE,
        workspace_id=team_id or event.get("team"),
        channel_id=channel_id,
        author_id=author_id,
        author_name=author_name,
        author_type="bot" if is_bot else "user",
        text=event.get("text") or "",
        source_ts=ts,
        created_at=slack_ts_to_datetime(ts),
        edited_at=slack_ts_to_datetime(edited_ts) if edited_ts else None,
        thread_parent=thread_parent,
        raw=event,
    )
