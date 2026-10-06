"""ArangoDB helpers shared by the sink and the state store."""

from __future__ import annotations

import logging

import requests
from arango import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import ArangoServerError, DatabaseCreateError
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class ArangoWriteError(Exception):
    """One or more documents in a batch failed to write."""

    def __init__(self, errors: list[ArangoServerError]) -> None:
        self.errors = errors
        codes = sorted({str(e.http_code) for e in errors})
        super().__init__(
            f"{len(errors)} document(s) failed to write (http_codes={','.join(codes)}): "
            f"{errors[0].error_message}"
        )


def is_retryable(exc: BaseException) -> bool:
    """Retry connection problems and transient server errors, not client mistakes."""
    # python-arango raises the builtin ConnectionAbortedError once its own
    # per-host attempts are exhausted; raw requests errors can also surface.
    if isinstance(exc, ConnectionError | requests.exceptions.RequestException):
        return True
    if isinstance(exc, ArangoServerError):
        return exc.http_code is None or exc.http_code >= 500 or exc.http_code in (408, 409, 429)
    if isinstance(exc, ArangoWriteError):
        return any(is_retryable(e) for e in exc.errors)
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
    hosts: str | list[str], username: str, password: str, db_name: str
) -> tuple[ArangoClient, StandardDatabase]:
    """Open a client and the named database, creating the database if missing."""
    client = ArangoClient(hosts=hosts)
    sys_db = client.db("_system", username=username, password=password)

    if not sys_db.has_database(db_name):
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
