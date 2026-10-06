"""ArangoDB-backed state store: one document per cursor."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from arango import ArangoClient
from arango.collection import StandardCollection
from tenacity import Retrying

from arango_connector.arango_common import connect, ensure_collection, retrying
from arango_connector.record import make_key
from arango_connector.state.base import StateStore

logger = logging.getLogger(__name__)


class ArangoStateStore(StateStore):
    """Stores each ``(namespace, key)`` as ``{_key, namespace, key, value, updated_at}``.

    The collection has no TTL index: losing a cursor would make a source re-sync or
    skip data, so state is kept until it is overwritten.
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
        self._collection: StandardCollection | None = None

    def setup(self) -> None:
        self._client, db = connect(self.hosts, self._username, self._password, self.db_name)
        ensure_collection(db, self.collection_name)
        self._collection = db.collection(self.collection_name)
        logger.info(
            "arango_state_store_ready db=%s collection=%s", self.db_name, self.collection_name
        )

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        doc = self._retrying()(self._require_collection().get, make_key(namespace, key))
        if not isinstance(doc, dict):
            return None
        value = doc.get("value")
        return value if isinstance(value, dict) else None

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        doc = {
            "_key": make_key(namespace, key),
            "namespace": namespace,
            "key": key,
            "value": value,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._retrying()(
            self._require_collection().insert, doc, overwrite_mode="replace", silent=True
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = self._collection = None

    def _retrying(self) -> Retrying:
        return retrying(self._retry_attempts, self._retry_wait_initial, self._retry_wait_max)

    def _require_collection(self) -> StandardCollection:
        if self._collection is None:
            raise RuntimeError("ArangoStateStore.setup() must be called before use")
        return self._collection
