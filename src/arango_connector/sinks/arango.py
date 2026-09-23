"""ArangoDB sink: idempotent upserts of Envelopes into a document collection."""

from __future__ import annotations

import logging
from typing import Any

import requests
from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import ArangoServerError, DatabaseCreateError
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from arango_connector.envelope import Envelope
from arango_connector.sinks.base import Sink

logger = logging.getLogger(__name__)

# Persistent indexes ensured on every start.
# TODO(future): users/channels collections, edge collections (authored, reply_to,
# posted_in) and an ArangoSearch view would be set up alongside these.
INDEXES: tuple[tuple[str, ...], ...] = (
    ("channel_id", "created_at"),
    ("source", "workspace_id", "channel_id"),
)


class ArangoWriteError(Exception):
    """One or more documents in a batch failed to write."""

    def __init__(self, errors: list[ArangoServerError]) -> None:
        self.errors = errors
        codes = sorted({str(e.http_code) for e in errors})
        super().__init__(
            f"{len(errors)} document(s) failed to write (http_codes={','.join(codes)}): "
            f"{errors[0].error_message}"
        )


def _is_retryable(exc: BaseException) -> bool:
    """Retry connection problems and transient server errors, not client mistakes."""
    # python-arango raises the builtin ConnectionAbortedError once its own
    # per-host attempts are exhausted; raw requests errors can also surface.
    if isinstance(exc, ConnectionError | requests.exceptions.RequestException):
        return True
    if isinstance(exc, ArangoServerError):
        return exc.http_code is None or exc.http_code >= 500 or exc.http_code in (408, 409, 429)
    if isinstance(exc, ArangoWriteError):
        return any(_is_retryable(e) for e in exc.errors)
    return False


class ArangoSink(Sink):
    """Writes Envelopes into a single ArangoDB document collection.

    Idempotency comes from the deterministic ``_key`` plus ``overwrite_mode="update"``:
    re-delivering the same message updates the existing document instead of
    creating a duplicate.
    """

    def __init__(
        self,
        hosts: str | list[str],
        username: str,
        password: str,
        db_name: str,
        collection_name: str,
        *,
        retry_attempts: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 5.0,
    ) -> None:
        self.hosts = hosts
        self.db_name = db_name
        self.collection_name = collection_name
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
        """Create the database, collection and indexes if missing. Idempotent."""
        self._client = ArangoClient(hosts=self.hosts)
        sys_db = self._client.db("_system", username=self._username, password=self._password)

        if not sys_db.has_database(self.db_name):
            try:
                sys_db.create_database(self.db_name)
                logger.info("arango_database_created db=%s", self.db_name)
            except DatabaseCreateError:
                # Lost a race with another process creating it; fine if it now exists.
                if not sys_db.has_database(self.db_name):
                    raise

        self._db = self._client.db(self.db_name, username=self._username, password=self._password)

        if not self._db.has_collection(self.collection_name):
            self._db.create_collection(self.collection_name)
            logger.info(
                "arango_collection_created db=%s collection=%s", self.db_name, self.collection_name
            )
        collection = self._db.collection(self.collection_name)

        for fields in INDEXES:
            # Arango returns the existing index when an identical one exists.
            result = collection.add_index({"type": "persistent", "fields": list(fields)})
            if isinstance(result, dict) and result.get("isNewlyCreated"):
                logger.info(
                    "arango_index_created collection=%s fields=%s", self.collection_name, fields
                )

        self._collection = collection
        logger.info(
            "arango_sink_ready hosts=%s db=%s collection=%s",
            self.hosts,
            self.db_name,
            self.collection_name,
        )

    def close(self) -> None:
        """Close the HTTP session(s)."""
        if self._client is not None:
            self._client.close()
        self._client = self._db = self._collection = None

    # -- writes ------------------------------------------------------------

    def write(self, envelopes: list[Envelope]) -> None:
        """Upsert envelopes, retrying transient failures; re-raises on final failure."""
        if not envelopes:
            return
        docs = [e.to_document() for e in envelopes]
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=self._retry_wait_initial, max=self._retry_wait_max),
            retry=retry_if_exception(_is_retryable),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        retrying(self._write_once, docs)

    def _write_once(self, docs: list[dict[str, Any]]) -> None:
        collection = self._require_collection()
        if len(docs) == 1:
            collection.insert(docs[0], overwrite_mode="update", silent=True)
            return
        results = collection.insert_many(docs, overwrite_mode="update")
        if isinstance(results, list):
            errors = [r for r in results if isinstance(r, ArangoServerError)]
            if errors:
                raise ArangoWriteError(errors)

    def _require_collection(self) -> StandardCollection:
        if self._collection is None:
            raise RuntimeError("ArangoSink.setup() must be called before write()")
        return self._collection
