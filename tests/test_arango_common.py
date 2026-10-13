"""Which ArangoDB write errors are retried (then dead-lettered) and which are not."""

from __future__ import annotations

import pytest
import requests
from arango.exceptions import ArangoServerError

from arango_connector.arango_common import is_retryable


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
