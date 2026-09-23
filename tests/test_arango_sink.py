"""Integration tests for ArangoSink against a real ArangoDB.

Run with ``docker compose up -d arangodb`` and ``uv run pytest -m integration``.
Connection details come from ARANGO_HOSTS / ARANGO_USERNAME / ARANGO_PASSWORD
(falling back to ``.env``). A throwaway database is created and dropped per test.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from arango_connector.sinks.arango import INDEXES, ArangoSink
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


@pytest.fixture
def sink() -> Iterator[ArangoSink]:
    password = _env("ARANGO_PASSWORD")
    if password is None:
        pytest.skip("ARANGO_PASSWORD not set")
    hosts = _env("ARANGO_HOSTS", "http://localhost:8529") or "http://localhost:8529"
    username = _env("ARANGO_USERNAME", "root") or "root"
    db_name = f"connector_test_{uuid.uuid4().hex[:8]}"

    s = ArangoSink(
        hosts=hosts.split(",")[0],
        username=username,
        password=password,
        db_name=db_name,
        collection_name="messages",
        retry_wait_initial=0.01,
    )
    yield s

    # Tear down the throwaway database.
    from arango import ArangoClient

    client = ArangoClient(hosts=hosts.split(",")[0])
    sys_db = client.db("_system", username=username, password=password)
    if sys_db.has_database(db_name):
        sys_db.delete_database(db_name)
    client.close()
    s.close()


def _count(sink: ArangoSink) -> int:
    assert sink._collection is not None
    count = sink._collection.count()
    assert isinstance(count, int)
    return count


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
