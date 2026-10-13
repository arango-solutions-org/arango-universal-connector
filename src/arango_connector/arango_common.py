"""ArangoDB helpers shared by the sink and the state store."""

from __future__ import annotations

import logging

import requests
from arango import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import ArangoServerError, DatabaseCreateError, ServerConnectionError
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


def is_retryable(exc: BaseException) -> bool:
    """Retry connection problems and transient server errors, not client mistakes."""
    # python-arango raises the builtin ConnectionAbortedError once its own
    # per-host attempts are exhausted; raw requests errors can also surface.
    if isinstance(exc, ConnectionError | requests.exceptions.RequestException):
        return True
    if isinstance(exc, ArangoServerError):
        return exc.http_code is None or exc.http_code >= 500 or exc.http_code in (408, 409, 429)
    return False


def retrying(attempts: int, wait_initial: float, wait_max: float) -> Retrying:
    """The retry policy for writes: exponential backoff on retryable errors, then re-raise."""
    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=wait_initial, max=wait_max),
        retry=retry_if_exception(is_retryable),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


def connect(
    hosts: str | list[str],
    username: str,
    password: str,
    db_name: str,
    *,
    create: bool = True,
) -> tuple[ArangoClient, StandardDatabase]:
    """Open a client and the named database, creating the database if missing.

    With ``create=False`` a missing database raises ``LookupError`` instead. Only
    creating the database needs access to ``_system``, so a user limited to ``db_name``
    can connect once it exists.
    """
    client = ArangoClient(hosts=hosts)
    try:
        return client, client.db(db_name, username=username, password=password, verify=True)
    except ServerConnectionError:
        pass  # missing, or not accessible: the _system path below creates or explains
    sys_db = client.db("_system", username=username, password=password)

    if not sys_db.has_database(db_name):
        if not create:
            client.close()
            raise LookupError(f"database {db_name!r} does not exist")
        try:
            sys_db.create_database(db_name)
            logger.info("arango_database_created db=%s", db_name)
        except DatabaseCreateError:
            # Lost a race with another process creating it; fine if it now exists.
            if not sys_db.has_database(db_name):
                raise

    return client, client.db(db_name, username=username, password=password)


def ensure_collection(db: StandardDatabase, name: str) -> None:
    """Create the collection if missing."""
    if not db.has_collection(name):
        db.create_collection(name)
        logger.info("arango_collection_created db=%s collection=%s", db.name, name)
