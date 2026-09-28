"""Source-agnostic message record that every source maps into and every sink consumes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 3


def make_key(source: str, channel_id: str, source_ts: str) -> str:
    """Build the deterministic document key ``{source}_{channel_id}_{source_ts}``.

    The key is what makes writes idempotent: re-delivery of the same message
    maps to the same key. All components must only use characters allowed in
    ArangoDB ``_key`` values (Slack IDs and ``ts`` values are alphanumerics and ``.``).
    """
    return f"{source}_{channel_id}_{source_ts}"


class Envelope(BaseModel):
    """A single normalized message, independent of the source it came from."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    key: str = Field(alias="_key")
    schema_version: int = SCHEMA_VERSION
    source: str
    workspace_id: str | None = None
    channel_id: str
    author_id: str | None = None
    author_name: str | None = None
    author_type: Literal["user", "bot"]
    text: str = ""
    source_ts: str
    created_at: datetime
    edited_at: datetime | None = None
    thread_parent: str | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any]

    def to_document(self) -> dict[str, Any]:
        """Return a JSON-safe dict with ``_key`` and ISO 8601 datetimes."""
        return self.model_dump(mode="json", by_alias=True)
