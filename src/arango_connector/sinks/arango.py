"""ArangoDB sink: idempotent upserts of Envelopes into a document collection."""

from __future__ import annotations

import logging
from typing import Any

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import ArangoServerError

from arango_connector.arango_common import (
    ArangoWriteError,
    connect,
    ensure_collection,
    retrying,
)
from arango_connector.envelope import Envelope
from arango_connector.sinks.base import Sink

logger = logging.getLogger(__name__)

# Persistent indexes ensured on every start.
# TODO(future): users/channels collections, edge collections (authored, reply_to,
# posted_in) and an ArangoSearch view would be set up alongside these.
INDEXES: tuple[tuple[str, ...], ...] = (
    ("scope.container_id", "created_at"),
    ("source", "scope.tenant_id", "scope.container_id"),
    ("kind",),
)

# Retention is enforced by a TTL index on ``expires_at``, which the pipeline stamps as
# (created_at or ingested_at) + RETENTION_DAYS. expireAfter=0 means "delete once the
# stored date has passed", so envelopes without a created_at still expire.
TTL_FIELD = "expires_at"
TTL_EXPIRE_AFTER = 0

# Re-derives expires_at for documents whose value does not match the current window:
# documents written before expires_at existed (schema v3), and every document after
# RETENTION_DAYS changes. Compared at millisecond precision, which is all AQL keeps.
_RECONCILE_EXPIRY_AQL = """
FOR d IN @@collection
    LET base = d.created_at != null ? d.created_at : d.ingested_at
    FILTER base != null
    LET expected = DATE_ADD(base, @days, "day")
    FILTER d.expires_at == null OR DATE_TIMESTAMP(d.expires_at) != DATE_TIMESTAMP(expected)
    UPDATE d WITH { expires_at: DATE_ISO8601(expected) } IN @@collection
    COLLECT WITH COUNT INTO updated
    RETURN updated
"""


def ttl_expiry_seconds(index: dict[str, Any]) -> int | None:
    """Read a TTL index's window, whichever spelling the driver used.

    ``add_index`` echoes ArangoDB's ``expireAfter``, but ``indexes()`` renames it to
    ``expiry_time``. Reading only one of them silently never matches, which would make
    reconciliation drop and recreate the index on every start.
    """
    for key in ("expiry_time", "expireAfter"):
        value = index.get(key)
        if isinstance(value, int):
            return value
    return None


class ArangoSink(Sink):
    """Writes Envelopes into a single ArangoDB document collection.

    Idempotency comes from the deterministic ``_key`` plus ``overwrite_mode="replace"``:
    re-delivering the same item overwrites the existing document instead of creating a
    duplicate. Replace rather than update because an Envelope is always a whole document:
    update merges nested objects, so keys an edit removed from ``raw`` would otherwise
    survive the write.
    """

    def __init__(
        self,
        hosts: str | list[str],
        username: str,
        password: str,
        db_name: str,
        collection_name: str,
        *,
        retention_days: int = 0,
        retry_attempts: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 5.0,
    ) -> None:
        self.hosts = hosts
        self.db_name = db_name
        self.collection_name = collection_name
        self.retention_days = retention_days
        self._username = username
        self._password = password
        self._retry_attempts = retry_attempts
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        self._client: ArangoClient | None = None
        self._db: StandardDatabase | None = None
        self._collection: StandardCollection | None = None

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        """Create the database, collection and indexes if missing. Idempotent.

        Also reconciles retention (the TTL index and stored ``expires_at`` values),
        which can delete existing documents.
        """
        self._client, self._db = connect(self.hosts, self._username, self._password, self.db_name)
        ensure_collection(self._db, self.collection_name)
        collection = self._db.collection(self.collection_name)

        for fields in INDEXES:
            # Arango returns the existing index when an identical one exists.
            result = collection.add_index({"type": "persistent", "fields": list(fields)})
            if isinstance(result, dict) and result.get("isNewlyCreated"):
                logger.info(
                    "arango_index_created collection=%s fields=%s", self.collection_name, fields
                )

        self._reconcile_ttl_index(collection)
        self._reconcile_expiry()

        self._collection = collection
        logger.info(
            "arango_sink_ready hosts=%s db=%s collection=%s retention_days=%s",
            self.hosts,
            self.db_name,
            self.collection_name,
            self.retention_days,
        )

    def _reconcile_ttl_index(self, collection: StandardCollection) -> None:
        """Make the collection's TTL index match ``retention_days``.

        ArangoDB allows at most one TTL index per collection, so an index on another
        field or with another ``expireAfter`` (such as the pre-v4 index on
        ``created_at``) is dropped before the desired one is added.
        """
        wanted = self.retention_days > 0
        indexes = collection.indexes()
        # Not a list only for async/batch execution, which this sink never uses.
        if not isinstance(indexes, list):
            indexes = []
        existing = [idx for idx in indexes if idx["type"] == "ttl"]

        if (
            wanted
            and len(existing) == 1
            and existing[0]["fields"] == [TTL_FIELD]
            and ttl_expiry_seconds(existing[0]) == TTL_EXPIRE_AFTER
        ):
            return

        for idx in existing:
            collection.delete_index(idx["id"])
            logger.info(
                "arango_ttl_index_dropped collection=%s fields=%s",
                self.collection_name,
                idx["fields"],
            )

        if not wanted:
            return

        collection.add_index(
            {"type": "ttl", "fields": [TTL_FIELD], "expireAfter": TTL_EXPIRE_AFTER}
        )
        logger.warning(
            "arango_ttl_index_created collection=%s field=%s retention_days=%s "
            "documents older than this are removed",
            self.collection_name,
            TTL_FIELD,
            self.retention_days,
        )

    def _reconcile_expiry(self) -> None:
        """Bring stored ``expires_at`` values in line with the current window.

        ``expires_at`` is fixed when a document is written, so without this a changed
        RETENTION_DAYS would only apply to new documents, and documents written before
        ``expires_at`` existed would never expire.
        """
        if self.retention_days <= 0 or self._db is None:
            return
        bind_vars: dict[str, Any] = {
            "@collection": self.collection_name,
            "days": self.retention_days,
        }
        cursor = self._db.aql.execute(_RECONCILE_EXPIRY_AQL, bind_vars=bind_vars)
        updated = next(iter(cursor), 0)  # type: ignore[arg-type]
        if updated:
            logger.warning(
                "arango_expiry_reconciled collection=%s updated=%s retention_days=%s",
                self.collection_name,
                updated,
                self.retention_days,
            )

    def close(self) -> None:
        """Close the HTTP session(s)."""
        if self._client is not None:
            self._client.close()
        self._client = self._db = self._collection = None

    # -- writes ------------------------------------------------------------

    def write(self, envelopes: list[Envelope[Any]]) -> None:
        """Upsert envelopes, retrying transient failures; re-raises on final failure."""
        if not envelopes:
            return
        docs = [r.to_document() for r in envelopes]
        retrying(self._retry_attempts, self._retry_wait_initial, self._retry_wait_max)(
            self._write_once, docs
        )

    def _write_once(self, docs: list[dict[str, Any]]) -> None:
        collection = self._require_collection()
        if len(docs) == 1:
            collection.insert(docs[0], overwrite_mode="replace", silent=True)
            return
        results = collection.insert_many(docs, overwrite_mode="replace")
        if isinstance(results, list):
            errors = [r for r in results if isinstance(r, ArangoServerError)]
            if errors:
                raise ArangoWriteError(errors)

    def _require_collection(self) -> StandardCollection:
        if self._collection is None:
            raise RuntimeError("ArangoSink.setup() must be called before write()")
        return self._collection
