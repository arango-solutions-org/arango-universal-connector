"""Integration tests for ArangoSink against a real ArangoDB.

Run with ``docker compose up -d arangodb`` and ``uv run pytest -m integration``.
Connection details come from ARANGO_HOSTS / ARANGO_USERNAME / ARANGO_PASSWORD
(falling back to ``.env``). A throwaway database is created and dropped per test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from arango_connector.record import Record
from arango_connector.sinks.arango import (
    INDEXES,
    TTL_EXPIRE_AFTER,
    TTL_FIELD,
    ArangoSink,
    ttl_expiry_seconds,
)
from arango_connector.sources.slack.normalize import normalize

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

LoadEvent = Callable[[str], dict[str, Any]]


MakeSink = Callable[..., ArangoSink]


@pytest.fixture
def make_sink(arango_conn: ArangoConn) -> Iterator[MakeSink]:
    """Build sinks that all share one throwaway database.

    Tests that reconcile indexes need several sinks against the same collection.
    """
    created: list[ArangoSink] = []

    def _make(retention_days: int = 0) -> ArangoSink:
        s = ArangoSink(
            hosts=arango_conn.hosts,
            username=arango_conn.username,
            password=arango_conn.password,
            db_name=arango_conn.db_name,
            collection_name="messages",
            retention_days=retention_days,
            retry_wait_initial=0.01,
        )
        created.append(s)
        return s

    yield _make

    for s in created:
        s.close()


@pytest.fixture
def sink(make_sink: MakeSink) -> ArangoSink:
    return make_sink()


def _count(sink: ArangoSink) -> int:
    assert sink._collection is not None
    count = sink._collection.count()
    assert isinstance(count, int)
    return count


def _ttl_indexes(sink: ArangoSink) -> list[dict[str, Any]]:
    assert sink._collection is not None
    indexes = sink._collection.indexes()
    assert isinstance(indexes, list)
    return [idx for idx in indexes if idx["type"] == "ttl"]


def test_setup_is_idempotent(sink: ArangoSink) -> None:
    sink.setup()
    sink.setup()
    sink.setup()
    assert sink._db is not None
    assert sink._db.has_collection("messages")


def test_indexes_exist(sink: ArangoSink) -> None:
    sink.setup()
    sink.setup()
    assert sink._collection is not None
    persistent = [
        tuple(idx["fields"])
        for idx in sink._collection.indexes()  # type: ignore[union-attr]
        if idx["type"] == "persistent"
    ]
    for fields in INDEXES:
        assert persistent.count(fields) == 1, persistent


def test_same_envelope_twice_yields_one_document(sink: ArangoSink, load_event: LoadEvent) -> None:
    sink.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None

    sink.write([env])
    sink.write([env])

    assert _count(sink) == 1
    assert sink._collection is not None
    doc = sink._collection.get(env.key)
    assert isinstance(doc, dict)
    assert doc["payload"]["text"] == env.payload.text
    assert doc["created_at"] == env.to_document()["created_at"]
    assert doc["raw"] == env.raw


def test_batch_write_is_idempotent(sink: ArangoSink, load_event: LoadEvent) -> None:
    sink.setup()
    envs = [
        normalize(load_event(n), "T0TESTTEAM")
        for n in ("user_message", "thread_reply", "bot_message")
    ]
    batch = [e for e in envs if e is not None]

    sink.write(batch)
    sink.write(batch)

    assert _count(sink) == 3


def test_no_ttl_index_when_retention_disabled(make_sink: MakeSink) -> None:
    sink = make_sink(retention_days=0)
    sink.setup()
    assert _ttl_indexes(sink) == []


def test_ttl_index_created_and_setup_is_idempotent(make_sink: MakeSink) -> None:
    sink = make_sink(retention_days=30)
    sink.setup()
    first_id = _ttl_indexes(sink)[0]["id"]
    sink.setup()

    ttl = _ttl_indexes(sink)
    assert len(ttl) == 1
    assert ttl[0]["fields"] == [TTL_FIELD]
    assert ttl_expiry_seconds(ttl[0]) == TTL_EXPIRE_AFTER
    # Same index, not dropped and recreated on the second start.
    assert ttl[0]["id"] == first_id


def test_legacy_created_at_ttl_index_is_replaced(make_sink: MakeSink) -> None:
    legacy = make_sink(retention_days=0)
    legacy.setup()
    assert legacy._collection is not None
    legacy._collection.add_index({"type": "ttl", "fields": ["created_at"], "expireAfter": 86400})

    # A second TTL index would be rejected, so this only passes if the first is dropped.
    upgraded = make_sink(retention_days=30)
    upgraded.setup()

    ttl = _ttl_indexes(upgraded)
    assert len(ttl) == 1
    assert ttl[0]["fields"] == [TTL_FIELD]


def _stamped(record: Record[Any], days: int) -> Record[Any]:
    assert record.created_at is not None
    return record.model_copy(update={"expires_at": record.created_at + timedelta(days=days)})


def _expires_at(sink: ArangoSink, key: str) -> datetime:
    assert sink._collection is not None
    doc = sink._collection.get(key)
    assert isinstance(doc, dict)
    return datetime.fromisoformat(doc[TTL_FIELD])


def test_changing_retention_rewrites_expires_at(make_sink: MakeSink, load_event: LoadEvent) -> None:
    first = make_sink(retention_days=30)
    first.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None and env.created_at is not None
    first.write([_stamped(env, 30)])

    changed = make_sink(retention_days=7)
    changed.setup()

    assert _expires_at(changed, env.key) == env.created_at.replace(
        microsecond=env.created_at.microsecond // 1000 * 1000
    ) + timedelta(days=7)


def test_documents_without_expires_at_are_backfilled(
    make_sink: MakeSink, load_event: LoadEvent
) -> None:
    """Pre-v4 documents carry no expires_at; setup must give them one or they never expire."""
    plain = make_sink(retention_days=0)
    plain.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None and env.created_at is not None
    plain.write([env])  # expires_at is None

    with_retention = make_sink(retention_days=30)
    with_retention.setup()

    assert (
        _expires_at(with_retention, env.key).date() == (env.created_at + timedelta(days=30)).date()
    )


def test_disabling_retention_drops_the_index(make_sink: MakeSink) -> None:
    make_sink(retention_days=30).setup()

    disabled = make_sink(retention_days=0)
    disabled.setup()

    assert _ttl_indexes(disabled) == []


def test_ttl_field_is_a_date_arango_can_expire(make_sink: MakeSink, load_event: LoadEvent) -> None:
    """expires_at is written with microsecond precision; Arango must parse it as a date."""
    sink = make_sink(retention_days=1)
    sink.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None
    sink.write([_stamped(env, 1)])

    assert sink._db is not None
    cursor = sink._db.aql.execute(
        "FOR m IN messages LIMIT 1 RETURN IS_DATESTRING(m.@field)",
        bind_vars={"field": TTL_FIELD},
    )
    assert list(cursor) == [True]  # type: ignore[arg-type]


def test_edit_overwrites_stored_message(sink: ArangoSink, load_event: LoadEvent) -> None:
    sink.setup()
    original = load_event("user_message")
    original["reactions"] = [{"name": "wave", "count": 1}]
    first = normalize(original, "T0TESTTEAM")
    assert first is not None
    sink.write([first])

    edit = normalize(load_event("message_changed"), "T0TESTTEAM")
    assert edit is not None
    assert edit.key == first.key
    sink.write([edit])

    assert _count(sink) == 1
    assert sink._collection is not None
    doc = sink._collection.get(first.key)
    assert isinstance(doc, dict)
    assert doc["payload"]["text"] == "Hello from the test channel (edited)"
    assert doc["updated_at"] is not None
    # Replace, not update: keys the edit dropped must not linger inside raw.
    assert "reactions" not in doc["raw"]
    assert "wave" not in json.dumps(doc)
