"""Shared test helpers."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


@pytest.fixture
def load_event() -> Callable[[str], dict[str, Any]]:
    """Return a loader for Slack event fixtures by basename."""
    return _load


def _env(name: str, default: str | None = None) -> str | None:
    """Read a setting from the environment, falling back to the repo's ``.env``."""
    if name in os.environ:
        return os.environ[name]
    dotenv = Path(__file__).parent.parent / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip()
    return default


@dataclass(frozen=True)
class ArangoConn:
    hosts: str
    username: str
    password: str
    db_name: str


@pytest.fixture
def arango_conn() -> Iterator[ArangoConn]:
    """Connection details for a throwaway database, dropped after the test.

    Skips when ARANGO_PASSWORD is not configured.
    """
    password = _env("ARANGO_PASSWORD")
    if password is None:
        pytest.skip("ARANGO_PASSWORD not set")
    hosts = (_env("ARANGO_HOSTS") or "http://localhost:8529").split(",")[0]
    username = _env("ARANGO_USERNAME") or "root"
    conn = ArangoConn(hosts, username, password, f"connector_test_{uuid.uuid4().hex[:8]}")

    yield conn

    from arango import ArangoClient

    client = ArangoClient(hosts=hosts)
    sys_db = client.db("_system", username=username, password=password)
    if sys_db.has_database(conn.db_name):
        sys_db.delete_database(conn.db_name)
    client.close()
