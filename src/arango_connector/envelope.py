"""Source-agnostic envelope that every source maps into and every sink consumes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from arango_connector.payloads import Payload

SCHEMA_VERSION = 4

# Characters ArangoDB allows in a document ``_key``.
_ARANGO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,254}$")


def make_key(source: str, *parts: str) -> str:
    """Build the deterministic document key ``{source}_{part}_{part}...``.

    The key is what makes writes idempotent: re-delivery of the same item maps to the
    same key. Raises ``ValueError`` on an empty part (which would let distinct items
    collide) or if the result is not a valid ArangoDB ``_key``.
    """
    if not source or not all(parts):
        raise ValueError(f"key parts must be non-empty: {(source, *parts)!r}")
    key = "_".join((source, *parts))
    if not _ARANGO_KEY_RE.match(key):
        raise ValueError(f"not a valid ArangoDB _key: {key!r}")
    return key


class Scope(BaseModel):
    """Where an envelope lives in its source, from outermost to innermost.

    ============  ==============  ================  ====================
    source        tenant_id       container_id      path
    ============  ==============  ================  ====================
    slack         team            channel           []
    gmail         domain          mailbox           []
    sharepoint    tenant          site              [library, folder]
    snowflake     account         database          [schema, table]
    ============  ==============  ================  ====================
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    container_id: str
    path: list[str] = Field(default_factory=list)


class Envelope[P: Payload](BaseModel):
    """A single normalized item, independent of the source it came from."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    key: str = Field(alias="_key")
    schema_version: int = SCHEMA_VERSION
    source: str
    # The item's own id in its source (Slack ``ts``, Gmail message id, ...).
    source_id: str
    scope: Scope
    created_at: datetime | None
    # When the item's content last changed in the source (e.g. a Slack edit).
    updated_at: datetime | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Stamped by the pipeline from RETENTION_DAYS; the sink's TTL index deletes on it.
    expires_at: datetime | None = None
    payload: P
    raw: dict[str, Any]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind(self) -> str:
        """The entity kind, copied from the payload so it can be indexed and queried."""
        return self.payload.kind

    @property
    def retention_base(self) -> datetime:
        """The time retention is measured from: when posted, else when ingested."""
        return self.created_at or self.ingested_at

    def to_document(self) -> dict[str, Any]:
        """Return a JSON-safe dict with ``_key`` and ISO 8601 datetimes.

        ``serialize_as_any`` keeps payload-subclass fields even when the Envelope is
        typed with the base ``Payload``.
        """
        return self.model_dump(mode="json", by_alias=True, serialize_as_any=True)
