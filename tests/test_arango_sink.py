"""Integration tests for ArangoSink against a real ArangoDB.

Run with ``docker compose up -d arangodb`` and ``uv run pytest -m integration``.
Connection details come from ARANGO_HOSTS / ARANGO_USERNAME / ARANGO_PASSWORD
(falling back to ``.env``). A throwaway database is created and dropped per test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

from arango_connector.sources.slack.normalize import normalize
from arango_connector_standalone.retention import RetentionPolicy
from arango_connector_standalone.sinks.arango import INDEXES, ArangoSink, ttl_expiry_seconds

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

    def _make(retention_days: int = 0, **policy: Any) -> ArangoSink:
        s = ArangoSink(
            hosts=arango_conn.hosts,
            username=arango_conn.username,
            password=arango_conn.password,
            db_name=arango_conn.db_name,
            collection_name="messages",
            retention=RetentionPolicy(retention_days, **policy),
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
    assert ttl[0]["fields"] == ["expires_at"]
    assert ttl_expiry_seconds(ttl[0]) == 0
    # Same index, not dropped and recreated on the second start.
    assert ttl[0]["id"] == first_id


def test_age_mode_ttl_index(make_sink: MakeSink) -> None:
    sink = make_sink(retention_days=30, mode="age", age_field="ingested_at")
    sink.setup()

    ttl = _ttl_indexes(sink)
    assert len(ttl) == 1
    assert ttl[0]["fields"] == ["ingested_at"]
    assert ttl_expiry_seconds(ttl[0]) == 30 * 86400


def test_switching_mode_replaces_the_index(make_sink: MakeSink) -> None:
    make_sink(retention_days=30, mode="age").setup()

    # A second TTL index would be rejected, so this only passes if the first is dropped.
    switched = make_sink(retention_days=30)
    switched.setup()

    ttl = _ttl_indexes(switched)
    assert len(ttl) == 1
    assert ttl[0]["fields"] == ["expires_at"]


def test_age_mode_window_change_replaces_the_index(make_sink: MakeSink) -> None:
    make_sink(retention_days=30, mode="age").setup()

    changed = make_sink(retention_days=7, mode="age")
    changed.setup()

    ttl = _ttl_indexes(changed)
    assert len(ttl) == 1
    assert ttl_expiry_seconds(ttl[0]) == 7 * 86400


def test_legacy_created_at_ttl_index_is_replaced(make_sink: MakeSink) -> None:
    legacy = make_sink(retention_days=0)
    legacy.setup()
    assert legacy._collection is not None
    legacy._collection.add_index({"type": "ttl", "fields": ["created_at"], "expireAfter": 86400})

    upgraded = make_sink(retention_days=30)
    upgraded.setup()

    ttl = _ttl_indexes(upgraded)
    assert len(ttl) == 1
    assert ttl[0]["fields"] == ["expires_at"]


def test_setup_never_rewrites_stored_documents(make_sink: MakeSink, load_event: LoadEvent) -> None:
    """Changing the window is applied by reconcile-expiry, not silently at startup."""
    first = make_sink(retention_days=30)
    first.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None
    stamped = RetentionPolicy(30).stamp(env)
    first.write([stamped])

    make_sink(retention_days=7).setup()

    assert first._collection is not None
    doc = first._collection.get(env.key)
    assert isinstance(doc, dict)
    assert doc["expires_at"] == stamped.to_document()["expires_at"]


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
    sink.write([RetentionPolicy(1).stamp(env)])

    assert sink._db is not None
    cursor = sink._db.aql.execute("FOR m IN messages LIMIT 1 RETURN IS_DATESTRING(m.expires_at)")
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


def test_user_limited_to_the_database_can_run_the_sink(
    sink: ArangoSink, arango_conn: ArangoConn, load_event: LoadEvent
) -> None:
    sink.setup()  # as the admin user: creates the database and collection
    from arango import ArangoClient

    client = ArangoClient(hosts=arango_conn.hosts)
    sys_db = client.db("_system", username=arango_conn.username, password=arango_conn.password)
    user = f"{arango_conn.db_name}_writer"
    sys_db.create_user(user, "writer-password")
    try:
        sys_db.update_permission(user, "rw", arango_conn.db_name)
        limited = ArangoSink(
            hosts=arango_conn.hosts,
            username=user,
            password="writer-password",
            db_name=arango_conn.db_name,
            collection_name="messages",
            retry_wait_initial=0.01,
        )
        limited.setup()
        env = normalize(load_event("user_message"), "T0TESTTEAM")
        assert env is not None
        limited.write([env])
        limited.close()
        assert _count(sink) == 1
    finally:
        sys_db.delete_user(user)
        client.close()
