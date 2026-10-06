"""End to end: Gmail adapter (fake API) -> Pipeline -> real ArangoSink and ArangoStateStore."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from arango import ArangoClient

from arango_connector.pipeline import Pipeline
from arango_connector.retention import RetentionPolicy
from arango_connector.sinks.arango import ArangoSink
from arango_connector.sources.gmail.adapter import GmailPollingAdapter
from arango_connector.sources.gmail.normalize import NormalizeOptions, mailbox_key
from arango_connector.state.arango import ArangoStateStore
from gmail_fakes import FakeGmailClient, message

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

ADA = "ada@example.com"


RETENTION = RetentionPolicy(30)


class Harness:
    """Runs one poll cycle per call through a fresh Pipeline, like a connector restart."""

    def __init__(self, conn: ArangoConn, gmail: FakeGmailClient) -> None:
        self.conn = conn
        self.gmail = gmail

    def _arango(self) -> dict[str, Any]:
        return {
            "hosts": self.conn.hosts,
            "username": self.conn.username,
            "password": self.conn.password,
            "db_name": self.conn.db_name,
        }

    def run_once(self) -> None:
        state = ArangoStateStore(**self._arango(), collection_name="connector_state")
        sink = ArangoSink(**self._arango(), collection_name="emails", retention=RETENTION)
        adapter = GmailPollingAdapter(
            [ADA],
            lambda _mailbox: self.gmail,
            state,
            include_labels=["INBOX", "SENT"],
            exclude_labels=["SPAM", "TRASH", "DRAFT"],
            options=NormalizeOptions(),
            interval_seconds=0.05,
        )
        pipeline = Pipeline(
            adapter, sink, dead_letter_path="/dev/null", retention=RETENTION, state=state
        )
        # One poll, then stop: the wait after the poll sees the stop flag.
        stopper = threading.Timer(0.02, adapter.stop)
        stopper.start()
        pipeline.run()
        stopper.join()
        pipeline.shutdown()

    def docs(self) -> dict[str, dict[str, Any]]:
        return {d["source_id"]: d for d in self._db().collection("emails").all()}  # type: ignore[union-attr]

    def cursor(self) -> dict[str, Any] | None:
        doc = self._db().collection("connector_state").get(f"gmail_{mailbox_key(ADA, 'hashed')}")
        return doc["value"] if isinstance(doc, dict) else None

    def ttl_indexes(self) -> list[dict[str, Any]]:
        indexes = self._db().collection("emails").indexes()
        assert isinstance(indexes, list)
        return [i for i in indexes if i["type"] == "ttl"]

    def _db(self) -> Any:
        client = ArangoClient(hosts=self.conn.hosts)
        return client.db(
            self.conn.db_name, username=self.conn.username, password=self.conn.password
        )


@pytest.fixture
def harness(arango_conn: ArangoConn) -> Iterator[Harness]:
    yield Harness(arango_conn, FakeGmailClient(ADA))


def test_new_mail_lands_once_and_cursor_survives_restarts(harness: Harness) -> None:
    harness.run_once()  # first start: record the cursor only
    assert harness.docs() == {}
    cursor = harness.cursor()
    assert cursor is not None and cursor["history_id"] == str(harness.gmail.history_id)
    ttl_index_id = harness.ttl_indexes()[0]["id"]

    harness.gmail.deliver(message("m1"))
    harness.gmail.deliver(message("m2", labels=("SENT",)))
    harness.gmail.deliver(message("spam", labels=("INBOX", "SPAM")))
    harness.run_once()

    docs = harness.docs()
    assert set(docs) == {"m1", "m2"}
    doc = docs["m1"]
    assert doc["kind"] == "email"
    assert doc["payload"]["from"]["address"] == "grace@example.com"
    assert doc["payload"]["body_text"] == "Hi Ada,\nSee Q2."
    assert doc["expires_at"].startswith("2024-06-22T21:33:20")  # received + 30 days
    assert ADA not in doc["_key"]

    # Restart with nothing new, then a label change: still exactly one doc each.
    harness.run_once()
    harness.gmail.relabel("m1", remove=("UNREAD",))
    harness.run_once()

    docs = harness.docs()
    assert set(docs) == {"m1", "m2"}
    assert docs["m1"]["payload"]["label_ids"] == ["INBOX"]
    assert docs["m1"]["updated_at"] is not None
    # Restarts with an unchanged policy keep the same TTL index.
    assert [i["id"] for i in harness.ttl_indexes()] == [ttl_index_id]
