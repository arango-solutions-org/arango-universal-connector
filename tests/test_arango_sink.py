"""Integration tests for ArangoSink against a real ArangoDB.

Run with ``docker compose up -d arangodb`` and ``uv run pytest -m integration``.
Connection details come from ARANGO_HOSTS / ARANGO_USERNAME / ARANGO_PASSWORD
(falling back to ``.env``). A throwaway database is created and dropped per test.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from arango_connector.sinks.arango import INDEXES, TTL_FIELD, ArangoSink, ttl_expiry_seconds
from arango_connector.sources.slack.normalize import normalize

pytestmark = pytest.mark.integration

LoadEvent = Callable[[str], dict[str, Any]]


def _env(name: str, default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    dotenv = Path(__file__).parent.parent / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip()
    return default


MakeSink = Callable[..., ArangoSink]


@pytest.fixture
def make_sink() -> Iterator[MakeSink]:
    """Build sinks that all share one throwaway database, dropped afterwards.

    Tests that reconcile indexes need several sinks against the same collection.
    """
    password = _env("ARANGO_PASSWORD")
    if password is None:
        pytest.skip("ARANGO_PASSWORD not set")
    hosts = _env("ARANGO_HOSTS", "http://localhost:8529") or "http://localhost:8529"
    username = _env("ARANGO_USERNAME", "root") or "root"
    db_name = f"connector_test_{uuid.uuid4().hex[:8]}"
    created: list[ArangoSink] = []

    def _make(retention_days: int = 0) -> ArangoSink:
        s = ArangoSink(
            hosts=hosts.split(",")[0],
            username=username,
            password=password,
            db_name=db_name,
            collection_name="messages",
            retention_days=retention_days,
            retry_wait_initial=0.01,
        )
        created.append(s)
        return s

    yield _make

    # Tear down the throwaway database.
    from arango import ArangoClient

    client = ArangoClient(hosts=hosts.split(",")[0])
    sys_db = client.db("_system", username=username, password=password)
    if sys_db.has_database(db_name):
        sys_db.delete_database(db_name)
    client.close()
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
    assert doc["text"] == env.text
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
    assert ttl_expiry_seconds(ttl[0]) == 30 * 86400
    # Same index, not dropped and recreated on the second start.
    assert ttl[0]["id"] == first_id


def test_changing_retention_replaces_the_index(make_sink: MakeSink) -> None:
    make_sink(retention_days=30).setup()

    # A second TTL index would be rejected, so this only passes if the first is dropped.
    changed = make_sink(retention_days=7)
    changed.setup()

    ttl = _ttl_indexes(changed)
    assert len(ttl) == 1
    assert ttl_expiry_seconds(ttl[0]) == 7 * 86400


def test_disabling_retention_drops_the_index(make_sink: MakeSink) -> None:
    make_sink(retention_days=30).setup()

    disabled = make_sink(retention_days=0)
    disabled.setup()

    assert _ttl_indexes(disabled) == []


def test_ttl_field_is_a_date_arango_can_expire(make_sink: MakeSink, load_event: LoadEvent) -> None:
    """created_at is written with microsecond precision; Arango must parse it as a date."""
    sink = make_sink(retention_days=1)
    sink.setup()
    env = normalize(load_event("user_message"), "T0TESTTEAM")
    assert env is not None
    sink.write([env])

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
    assert doc["text"] == "Hello from the test channel (edited)"
    assert doc["edited_at"] is not None
    # Replace, not update: keys the edit dropped must not linger inside raw.
    assert "reactions" not in doc["raw"]
    assert "wave" not in json.dumps(doc)
