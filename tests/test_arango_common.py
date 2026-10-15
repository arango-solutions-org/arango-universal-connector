"""ArangoDB helpers: which write errors are retried, and racing collection creation."""

from __future__ import annotations

import pytest
import requests
from arango.exceptions import ArangoServerError, CollectionCreateError

from arango_connector.arango_common import ensure_collection, is_retryable


def _server_error(http_code: int | None) -> ArangoServerError:
    err = ArangoServerError.__new__(ArangoServerError)
    err.http_code = http_code
    return err


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionAbortedError("hosts exhausted"), True),
        (requests.exceptions.Timeout(), True),
        (_server_error(None), True),
        (_server_error(503), True),
        (_server_error(408), True),
        (_server_error(409), True),
        (_server_error(429), True),
        (_server_error(400), False),
        (_server_error(404), False),
        (ValueError("bad document"), False),
    ],
)
def test_is_retryable(exc: BaseException, expected: bool) -> None:
    assert is_retryable(exc) is expected


class RacingDb:
    """Another process creates the collection between our check and our create."""

    name = "connector"

    def __init__(self, exists_after_error: bool) -> None:
        self.checks = [False, exists_after_error]

    def has_collection(self, _name: str) -> bool:
        return self.checks.pop(0)

    def create_collection(self, _name: str) -> None:
        raise CollectionCreateError.__new__(CollectionCreateError)


def test_losing_the_create_race_is_fine() -> None:
    ensure_collection(RacingDb(exists_after_error=True), "connector_state")  # type: ignore[arg-type]


def test_a_real_create_failure_still_raises() -> None:
    with pytest.raises(CollectionCreateError):
        ensure_collection(RacingDb(exists_after_error=False), "connector_state")  # type: ignore[arg-type]
