"""End to end: Jira adapter (fake API) -> Pipeline -> real ArangoSink and ArangoStateStore."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from arango import ArangoClient

from arango_connector.pipeline import Pipeline
from arango_connector.retention import RetentionPolicy
from arango_connector.sinks.arango import ArangoSink
from arango_connector.sources.jira.adapter import JiraPollingAdapter
from arango_connector.state.arango import ArangoStateStore
from jira_fakes import BASE_URL, SITE, FakeJiraClient, comment, issue

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

COLLECTION = "jira"
# The Compose default for the Jira service: Jira issues outlive any created_at window.
RETENTION = RetentionPolicy(0)


class Harness:
    """Runs one poll cycle per call through a fresh Pipeline, like a connector restart."""

    def __init__(self, conn: ArangoConn, jira: FakeJiraClient) -> None:
        self.conn = conn
        self.jira = jira
        self.now = datetime.now(UTC).replace(microsecond=0)

    def _arango(self) -> dict[str, Any]:
        return {
            "hosts": self.conn.hosts,
            "username": self.conn.username,
            "password": self.conn.password,
            "db_name": self.conn.db_name,
        }

    def run_once(self) -> None:
        state = ArangoStateStore(**self._arango(), collection_name="connector_state")
        sink = ArangoSink(**self._arango(), collection_name=COLLECTION, retention=RETENTION)
        adapter = JiraPollingAdapter(
            ["PROJ"],
            lambda _should_stop: self.jira,
            state,
            site=SITE,
            base_url=BASE_URL,
            initial_sync="all",
            interval_seconds=0.05,
            clock=lambda: self.now,
        )
        pipeline = Pipeline(
            adapter, sink, dead_letter_path="/dev/null", retention=RETENTION, state=state
        )
        # Exactly one poll, then stop: the wait after the poll sees the stop flag. (A
        # timer could fire before the pipeline's setup finishes, and skip the poll.)
        poll_once = adapter.poll_once

        def poll_then_stop(emit: Any) -> None:
            poll_once(emit)
            adapter.stop()

        adapter.poll_once = poll_then_stop  # type: ignore[method-assign]
        pipeline.run()
        pipeline.shutdown()

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now

    def docs(self) -> list[dict[str, Any]]:
        return list(self._db().collection(COLLECTION).all())

    def cursor(self) -> dict[str, Any] | None:
        doc = self._db().collection("connector_state").get(f"jira_{SITE}:PROJ")
        return doc["value"] if isinstance(doc, dict) else None

    def _db(self) -> Any:
        client = ArangoClient(hosts=self.conn.hosts)
        return client.db(
            self.conn.db_name, username=self.conn.username, password=self.conn.password
        )


@pytest.fixture
def harness(arango_conn: ArangoConn) -> Iterator[Harness]:
    yield Harness(arango_conn, FakeJiraClient())


def by_source(docs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {(d["kind"], d["source_id"]): d for d in docs}
    assert len(result) == len(docs), "duplicate (kind, source_id)"
    return result


def test_issues_and_comments_land_once_across_resyncs_and_restarts(harness: Harness) -> None:
    old = harness.now - timedelta(days=700)
    harness.jira.put_issue(issue("1", "PROJ-1", created=old, updated=old))
    harness.jira.put_issue(issue("2", "PROJ-2", created=old, updated=harness.advance(seconds=1)))
    harness.jira.add_comment("2", comment("c1", "First", created=harness.advance(seconds=1)))
    harness.advance(minutes=1)

    harness.run_once()
    docs = by_source(harness.docs())
    assert set(docs) == {("issue", "1"), ("issue", "2"), ("comment", "c1")}
    issue_doc = docs[("issue", "2")]
    assert issue_doc["_key"] == f"jira_{SITE}_issue_2"
    assert issue_doc["payload"]["issue_key"] == "PROJ-2"
    assert issue_doc["payload"]["comment_count"] == 1
    assert issue_doc["expires_at"] is None  # retention off: a 700-day-old issue is kept
    comment_doc = docs[("comment", "c1")]
    assert comment_doc["scope"]["path"] == ["2"]
    assert comment_doc["payload"]["parent_key"] == "PROJ-2"
    cursor = harness.cursor()
    assert cursor is not None and "initial" not in cursor

    # Restart with nothing new: nothing changes.
    harness.advance(minutes=5)
    harness.run_once()
    assert len(harness.docs()) == 3

    # An edit and a new comment overwrite / add, never duplicate.
    harness.jira.edit_comment("2", "c1", "First (edited)", harness.advance(seconds=1))
    harness.jira.add_comment("2", comment("c2", "Second", created=harness.advance(seconds=1)))
    harness.jira.touch("1", harness.advance(seconds=1), summary="Renamed")
    harness.advance(minutes=1)
    harness.run_once()

    docs = by_source(harness.docs())
    assert set(docs) == {
        ("issue", "1"),
        ("issue", "2"),
        ("comment", "c1"),
        ("comment", "c2"),
    }
    assert docs[("comment", "c1")]["payload"]["text"] == "First (edited)"
    assert docs[("comment", "c1")]["updated_at"] is not None
    assert docs[("issue", "1")]["payload"]["summary"] == "Renamed"

    # A full re-sync from scratch (cursor lost) still produces no duplicates.
    harness._db().collection("connector_state").truncate()
    harness.run_once()
    assert len(by_source(harness.docs())) == 4
