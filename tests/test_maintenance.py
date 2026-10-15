"""Integration tests for the reconcile-expiry maintenance command."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arango import ArangoClient
from arango.database import StandardDatabase

from arango_connector_standalone.maintenance import main, reconcile_expiry
from arango_connector_standalone.retention import RetentionPolicy

if TYPE_CHECKING:
    from conftest import ArangoConn

pytestmark = pytest.mark.integration

POSTED = datetime(2024, 5, 1, 12, 0, 0, 123456, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def db(arango_conn: ArangoConn) -> Iterator[StandardDatabase]:
    client = ArangoClient(hosts=arango_conn.hosts)
    sys_db = client.db("_system", username=arango_conn.username, password=arango_conn.password)
    sys_db.create_database(arango_conn.db_name)
    database = client.db(
        arango_conn.db_name, username=arango_conn.username, password=arango_conn.password
    )
    database.create_collection("messages")
    yield database
    client.close()


def _seed(db: StandardDatabase, docs: list[dict[str, Any]]) -> None:
    db.collection("messages").insert_many(docs)


def _expires(db: StandardDatabase, key: str) -> datetime | None:
    doc = db.collection("messages").get(key)
    assert isinstance(doc, dict)
    value = doc.get("expires_at")
    return datetime.fromisoformat(value) if value else None


def _ms(dt: datetime) -> datetime:
    """AQL keeps millisecond precision."""
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def _docs() -> list[dict[str, Any]]:
    return [
        # schema v3: no expires_at at all
        {"_key": "v3", "created_at": _iso(POSTED)},
        # written under a 30-day window
        {"_key": "w30", "created_at": _iso(POSTED), "expires_at": _iso(POSTED + timedelta(30))},
        # no created_at: falls back to ingested_at
        {"_key": "undated", "created_at": None, "ingested_at": _iso(POSTED)},
        # nothing to derive from: left alone
        {"_key": "nodates"},
    ]


def test_applies_new_window_in_batches(db: StandardDatabase) -> None:
    _seed(db, _docs())

    result = reconcile_expiry(db, "messages", RetentionPolicy(7), batch_size=2)

    assert result.scanned == 4
    assert result.batches == 2
    assert result.updated == 3
    for key in ("v3", "w30", "undated"):
        assert _expires(db, key) == _ms(POSTED) + timedelta(days=7)
    assert _expires(db, "nodates") is None

    # Running again finds nothing left to do.
    again = reconcile_expiry(db, "messages", RetentionPolicy(7), batch_size=2)
    assert again.updated == 0


def test_only_missing_leaves_existing_deadlines(db: StandardDatabase) -> None:
    _seed(db, _docs())

    result = reconcile_expiry(db, "messages", RetentionPolicy(7), only_missing=True)

    assert result.updated == 2  # v3, undated
    # Untouched, so still the exact (microsecond) value it was written with.
    assert _expires(db, "w30") == POSTED + timedelta(days=30)
    assert _expires(db, "v3") == _ms(POSTED) + timedelta(days=7)


def test_dry_run_writes_nothing(db: StandardDatabase) -> None:
    _seed(db, _docs())

    result = reconcile_expiry(db, "messages", RetentionPolicy(7), dry_run=True)

    assert result.stale == 3
    assert result.updated == 0
    assert _expires(db, "v3") is None


@pytest.mark.parametrize("policy", [RetentionPolicy(0), RetentionPolicy(30, mode="age")])
def test_refuses_outside_expiry_mode(db: StandardDatabase, policy: RetentionPolicy) -> None:
    with pytest.raises(ValueError, match="RETENTION_MODE=expiry"):
        reconcile_expiry(db, "messages", policy)


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, conn: ArangoConn) -> None:
    """Point the CLI at the test database, isolated from any developer .env or shell env."""
    monkeypatch.chdir(tmp_path)  # the CLI reads .env from the working directory
    for var in ("RETENTION_MODE", "RETENTION_AGE_FIELD", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARANGO_HOSTS", conn.hosts)
    monkeypatch.setenv("ARANGO_USERNAME", conn.username)
    monkeypatch.setenv("ARANGO_PASSWORD", conn.password)
    monkeypatch.setenv("ARANGO_DB", conn.db_name)
    monkeypatch.setenv("ARANGO_COLLECTION", "messages")
    monkeypatch.setenv("RETENTION_DAYS", "7")


def test_cli_end_to_end(
    db: StandardDatabase,
    arango_conn: ArangoConn,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed(db, _docs())
    _cli_env(monkeypatch, tmp_path, arango_conn)

    assert main(["--batch-size", "3"]) == 0
    assert _expires(db, "v3") == _ms(POSTED) + timedelta(days=7)

    monkeypatch.setenv("RETENTION_MODE", "age")
    assert main([]) == 2


def test_missing_collection_is_a_clear_error(db: StandardDatabase) -> None:
    with pytest.raises(LookupError, match="'absent' does not exist"):
        reconcile_expiry(db, "absent", RetentionPolicy(7))


def test_cli_never_creates_a_database(
    arango_conn: ArangoConn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _cli_env(monkeypatch, tmp_path, arango_conn)  # ARANGO_DB is not created by this test

    assert main([]) == 2
    assert "does not exist" in capsys.readouterr().err

    client = ArangoClient(hosts=arango_conn.hosts)
    sys_db = client.db("_system", username=arango_conn.username, password=arango_conn.password)
    assert not sys_db.has_database(arango_conn.db_name)
    client.close()
