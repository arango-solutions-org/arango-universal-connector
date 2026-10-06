"""Typed payloads, one per entity kind.

Payloads are typed by *what* a record is, not *where* it came from: Slack, Teams and
Discord all produce ``message`` payloads, so cross-source queries see one shape.

TODO(future): kinds for upcoming sources follow the same pattern, e.g.

    class DocumentPayload(Payload):   # SharePoint, Drive
        kind: Literal["document"] = "document"
        title, path, mime_type, size, url, author, version, content

    class RowPayload(Payload):        # Snowflake, Postgres
        kind: Literal["row"] = "row"
        table, primary_key: dict[str, Any], columns: dict[str, Any]
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Payload(BaseModel):
    """Base for all payloads. ``kind`` names the entity kind and is copied onto the Record."""

    model_config = ConfigDict(frozen=True)

    kind: str


class MessagePayload(Payload):
    """A chat message (Slack today; Teams, Discord, ... later)."""

    kind: Literal["message"] = "message"
    author_id: str | None = None
    author_name: str | None = None
    author_type: Literal["user", "bot", "system"]
    text: str = ""
    # The conversation thread this message belongs to, if any (Slack ``thread_ts``).
    thread_id: str | None = None
    # The message this one replies to; ``None`` for top-level messages and thread roots.
    parent_id: str | None = None
