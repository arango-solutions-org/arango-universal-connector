"""State store tests: the in-memory store offline, ArangoStateStore against a real ArangoDB."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from arango_connector.state import InMemoryStateStore
from arango_connector.state.arango import ArangoStateStore

if TYPE_CHECKING:
    from conftest import ArangoConn


def test_memory_store_round_trip() -> None:
    store = InMemoryStateStore()
    assert store.get("gmail", "mbox1") is None

    store.put("gmail", "mbox1", {"history_id": "100"})
    store.put("gmail", "mbox1", {"history_id": "200"})

    assert store.get("gmail", "mbox1") == {"history_id": "200"}
    assert store.get("gmail", "mbox2") is None
    assert store.get("other", "mbox1") is None


def test_memory_store_returns_copies() -> None:
    store = InMemoryStateStore()
    value = {"cursor": {"page": 1}}
    store.put("s", "k", value)
    value["cursor"]["page"] = 99

    got = store.get("s", "k")
    assert got == {"cursor": {"page": 1}}
    assert got is not None
    got["cursor"]["page"] = 42
    assert store.get("s", "k") == {"cursor": {"page": 1}}


@pytest.fixture
def arango_store(arango_conn: ArangoConn) -> Iterator[ArangoStateStore]:
    store = ArangoStateStore(
        hosts=arango_conn.hosts,
        username=arango_conn.username,
        password=arango_conn.password,
        db_name=arango_conn.db_name,
        collection_name="connector_state",
        retry_wait_initial=0.01,
    )
    yield store
    store.close()


@pytest.mark.integration
def test_arango_store_round_trip(arango_store: ArangoStateStore) -> None:
    arango_store.setup()
    arango_store.setup()  # idempotent
    assert arango_store.get("gmail", "a@example.com") is None

    arango_store.put("gmail", "a@example.com", {"history_id": "100"})
    arango_store.put("gmail", "a@example.com", {"history_id": "200"})

    assert arango_store.get("gmail", "a@example.com") == {"history_id": "200"}


@pytest.mark.integration
def test_arango_store_has_no_ttl_index(arango_store: ArangoStateStore) -> None:
    arango_store.setup()
    assert arango_store._collection is not None
    indexes = arango_store._collection.indexes()
    assert isinstance(indexes, list)
    assert [idx for idx in indexes if idx["type"] == "ttl"] == []
