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
from arango_connector.retention import RetentionPolicy
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

# Retention is enforced by one TTL index whose field and window come from the
# RetentionPolicy (see retention.py for the two modes).


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
        retention: RetentionPolicy | None = None,
        retry_attempts: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 5.0,
    ) -> None:
        self.hosts = hosts
        self.db_name = db_name
        self.collection_name = collection_name
        self.retention = retention or RetentionPolicy()
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

        Also reconciles the retention TTL index, which can delete existing documents.
        Stored documents are never rewritten here; see ``maintenance.reconcile_expiry``.
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

        self._collection = collection
        logger.info(
            "arango_sink_ready hosts=%s db=%s collection=%s retention=%s",
            self.hosts,
            self.db_name,
            self.collection_name,
            self.retention.describe(),
        )

    def _reconcile_ttl_index(self, collection: StandardCollection) -> None:
        """Make the collection's TTL index match the retention policy.

        ArangoDB allows at most one TTL index per collection, so an index on another
        field or with another ``expireAfter`` (a different mode, window, or the pre-v4
        index on ``created_at``) is dropped before the desired one is added.
        """
        desired = self.retention.ttl_index()
        indexes = collection.indexes()
        # Not a list only for async/batch execution, which this sink never uses.
        if not isinstance(indexes, list):
            indexes = []
        existing = [idx for idx in indexes if idx["type"] == "ttl"]

        if (
            desired is not None
            and len(existing) == 1
            and existing[0]["fields"] == [desired[0]]
            and ttl_expiry_seconds(existing[0]) == desired[1]
        ):
            return

        for idx in existing:
            collection.delete_index(idx["id"])
            logger.info(
                "arango_ttl_index_dropped collection=%s fields=%s",
                self.collection_name,
                idx["fields"],
            )

        if desired is None:
            return

        field, expire_after = desired
        collection.add_index({"type": "ttl", "fields": [field], "expireAfter": expire_after})
        logger.warning(
            "arango_ttl_index_created collection=%s field=%s retention=%s "
            "documents past their retention are removed",
            self.collection_name,
            field,
            self.retention.describe(),
        )
        if self.retention.mode == "expiry":
            # Deadlines are fixed at write time, so documents written under another
            # window, another mode or before schema v4 keep theirs (or have none).
            logger.warning(
                "arango_existing_expiry_unchanged collection=%s existing documents keep "
                "their stored expires_at; run arango-connector-reconcile-expiry to apply "
                "this window to them",
                self.collection_name,
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
