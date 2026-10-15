"""Retention policy: how long stored envelopes live and how their deletion is enforced.

Two modes, mirroring MongoDB's two kinds of TTL index:

``expiry`` (default)
    Each document carries its own deadline. The pipeline stamps ``expires_at`` =
    (``created_at`` or ``ingested_at``) + ``days`` when it writes, and the TTL index on
    ``expires_at`` uses ``expireAfter: 0``. Deadlines are fixed at write time, so changing
    ``days`` applies to new documents only; existing ones keep theirs until
    ``arango-connector-reconcile-expiry`` is run. This is what makes per-document
    deadlines possible (per-source windows, legal holds).

``age``
    Documents expire ``days`` after a date they already carry (``age_field``). The
    window lives in the TTL index, so changing ``days`` applies to every document at
    once. Documents without that field never expire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from arango_connector.envelope import Envelope

RetentionMode = Literal["expiry", "age"]
AgeField = Literal["created_at", "ingested_at"]

EXPIRY_FIELD = "expires_at"
SECONDS_PER_DAY = 86400


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RetentionPolicy:
    """``days == 0`` disables retention entirely, in either mode."""

    days: int = 0
    mode: RetentionMode = "expiry"
    age_field: AgeField = "created_at"

    def __post_init__(self) -> None:
        if self.days < 0:
            raise ValueError("retention days must be >= 0")

    @property
    def enabled(self) -> bool:
        return self.days > 0

    def ttl_index(self) -> tuple[str, int] | None:
        """The ``(field, expireAfter seconds)`` TTL index to enforce, or ``None``."""
        if not self.enabled:
            return None
        if self.mode == "expiry":
            return EXPIRY_FIELD, 0
        return self.age_field, self.days * SECONDS_PER_DAY

    def stamp(self, envelope: Envelope[Any]) -> Envelope[Any]:
        """Return ``envelope`` with ``expires_at`` set as this policy dictates.

        Only ``expiry`` mode stores a deadline; in ``age`` mode the field is left empty so
        it cannot drift out of step with the index's window.
        """
        if not self.enabled or self.mode != "expiry":
            return envelope.model_copy(update={EXPIRY_FIELD: None})
        expires_at = envelope.retention_base + timedelta(days=self.days)
        return envelope.model_copy(update={EXPIRY_FIELD: expires_at})

    def expiry_of(self, doc: dict[str, Any]) -> datetime | None:
        """When a stored document expires under this policy, or ``None`` if never.

        Used where no TTL index reaches (the dead-letter file). In ``expiry`` mode a
        document written before ``expires_at`` existed falls back to its creation (else
        ingestion) time plus the window, so old dead-letter lines still age out.
        Raises ``ValueError``/``TypeError`` on unparseable dates.
        """
        if not self.enabled:
            return None
        window = timedelta(days=self.days)
        if self.mode == "age":
            base = _parse(doc.get(self.age_field))
            return base + window if base is not None else None
        stored = _parse(doc.get(EXPIRY_FIELD))
        if stored is not None:
            return stored
        base = _parse(doc.get("created_at") or doc.get("ingested_at"))
        return base + window if base is not None else None

    def describe(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.mode == "age":
            return f"age days={self.days} field={self.age_field}"
        return f"expiry days={self.days}"
