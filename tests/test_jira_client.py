"""RequestsJiraClient against canned HTTP responses (a fake requests.Session)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests

from arango_connector.sources.jira.client import (
    ISSUE_FIELDS,
    MAX_ATTEMPTS,
    JiraAuthError,
    JiraError,
    JiraInterrupted,
    RequestsJiraClient,
    build_jql,
    format_jql_time,
)

MYSELF = {"accountId": "5b10", "timeZone": "America/New_York"}


class FakeResponse:
    def __init__(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.reason = "Reason"
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON")
        return json.loads(json.dumps(self._body))


Reply = FakeResponse | Exception


class FakeSession:
    """Replays ``replies`` in order and records each request."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.auth: Any = None
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kw})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_client(
    *replies: Reply, should_stop: Callable[[], bool] = lambda: False
) -> tuple[RequestsJiraClient, FakeSession, list[float]]:
    session = FakeSession(*replies)
    slept: list[float] = []
    client = RequestsJiraClient(
        "https://acme.atlassian.net/",
        "bot@example.com",
        "secret-token",
        page_size=50,
        session=session,
        sleep=slept.append,
        should_stop=should_stop,
    )
    return client, session, slept


def ok(body: Any) -> FakeResponse:
    return FakeResponse(200, body)


# -- JQL -------------------------------------------------------------------------


def test_build_jql() -> None:
    assert build_jql("PROJ", None) == 'project = "PROJ" ORDER BY updated ASC, key ASC'
    assert build_jql("PROJ", "2024/05/23 17:33") == (
        'project = "PROJ" AND updated >= "2024/05/23 17:33" ORDER BY updated ASC, key ASC'
    )
    assert build_jql('A"B', None).startswith('project = "A\\"B"')


def test_format_jql_time_uses_zone_and_truncates_to_the_minute() -> None:
    moment = datetime(2024, 5, 23, 21, 33, 59, 999000, tzinfo=UTC)
    assert format_jql_time(moment, UTC) == "2024/05/23 21:33"
    assert format_jql_time(moment, ZoneInfo("America/New_York")) == "2024/05/23 17:33"


# -- search ----------------------------------------------------------------------


def test_search_sends_jql_in_the_users_time_zone_and_follows_pages() -> None:
    client, session, _ = make_client(
        ok(MYSELF),
        ok({"issues": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t1", "isLast": False}),
        ok({"issues": [{"id": "3"}], "isLast": True}),
    )
    since = datetime(2024, 5, 23, 21, 33, 20, tzinfo=UTC)

    pages = list(client.search_issues("PROJ", since))

    assert [[i["id"] for i in page] for page in pages] == [["1", "2"], ["3"]]
    myself, first, second = session.requests
    assert myself["url"] == "https://acme.atlassian.net/rest/api/3/myself"
    assert first["method"] == "POST"
    assert first["url"] == "https://acme.atlassian.net/rest/api/3/search/jql"
    assert first["json"]["jql"] == (
        'project = "PROJ" AND updated >= "2024/05/23 17:33" ORDER BY updated ASC, key ASC'
    )
    assert first["json"]["fields"] == list(ISSUE_FIELDS)
    assert first["json"]["maxResults"] == 50
    assert "nextPageToken" not in first["json"]
    assert second["json"]["nextPageToken"] == "t1"
    assert session.auth == ("bot@example.com", "secret-token")
    assert session.headers["Accept"] == "application/json"


def test_time_zone_is_looked_up_once() -> None:
    client, session, _ = make_client(
        ok(MYSELF), ok({"issues": [], "isLast": True}), ok({"issues": [], "isLast": True})
    )
    since = datetime(2024, 5, 23, tzinfo=UTC)
    list(client.search_issues("A", since))
    list(client.search_issues("B", since))
    assert [r["url"].rsplit("/", 1)[-1] for r in session.requests] == ["myself", "jql", "jql"]


def test_unknown_time_zone_falls_back_to_utc() -> None:
    client, _, _ = make_client(ok({"accountId": "x", "timeZone": "Mars/Olympus"}))
    assert client.time_zone() is UTC


def test_search_without_since_reads_everything_and_skips_myself() -> None:
    client, session, _ = make_client(ok({"issues": [{"id": "1"}]}))
    assert list(client.search_issues("PROJ", None)) == [[{"id": "1"}]]
    assert session.requests[0]["json"]["jql"] == 'project = "PROJ" ORDER BY updated ASC, key ASC'


# -- comments --------------------------------------------------------------------


def test_comments_are_paged_by_start_at() -> None:
    client, session, _ = make_client(
        ok({"comments": [{"id": "a"}, {"id": "b"}], "startAt": 0, "total": 3}),
        ok({"comments": [{"id": "c"}], "startAt": 2, "total": 3}),
    )
    assert [c["id"] for c in client.list_comments("10001")] == ["a", "b", "c"]
    first, second = session.requests
    assert first["url"].endswith("/rest/api/3/issue/10001/comment")
    assert first["params"] == {"startAt": 0, "maxResults": 100, "orderBy": "created"}
    assert second["params"]["startAt"] == 2


def test_comments_of_a_deleted_issue_are_empty() -> None:
    client, _, _ = make_client(FakeResponse(404, {"errorMessages": ["Issue does not exist"]}))
    assert list(client.list_comments("10001")) == []


# -- errors and retries ----------------------------------------------------------


def test_rate_limit_waits_retry_after_then_succeeds() -> None:
    client, session, slept = make_client(
        FakeResponse(429, None, {"Retry-After": "7", "RateLimit-Reason": "jira-burst-based"}),
        ok({"issues": [], "isLast": True}),
    )
    assert list(client.search_issues("PROJ", None)) == [[]]
    assert len(session.requests) == 2
    assert sum(slept) == pytest.approx(7)


def test_retry_after_is_capped() -> None:
    client, _, slept = make_client(
        FakeResponse(503, None, {"Retry-After": "3600"}), ok({"issues": [], "isLast": True})
    )
    list(client.search_issues("PROJ", None))
    assert sum(slept) == pytest.approx(60)


def test_server_errors_and_connection_errors_are_retried_with_backoff() -> None:
    client, _, slept = make_client(
        FakeResponse(502),
        requests.ConnectionError("reset"),
        ok({"issues": [{"id": "1"}], "isLast": True}),
    )
    assert list(client.search_issues("PROJ", None)) == [[{"id": "1"}]]
    assert 0 < sum(slept) <= 1.3 * (2 + 4)


def test_gives_up_after_max_attempts() -> None:
    client, session, _ = make_client(*[FakeResponse(429, None, {"Retry-After": "1"})] * 5)
    with pytest.raises(JiraError, match="HTTP 429"):
        list(client.search_issues("PROJ", None))
    assert len(session.requests) == MAX_ATTEMPTS


def test_auth_failure_raises_without_the_token() -> None:
    client, _, _ = make_client(FakeResponse(401, {"errorMessages": ["Unauthorized"]}))
    with pytest.raises(JiraAuthError) as info:
        list(client.search_issues("PROJ", None))
    assert info.value.status == 401
    assert "secret-token" not in str(info.value)


def test_bad_jql_reports_jiras_message() -> None:
    body = {"errorMessages": ["The value 'NOPE' does not exist for the field 'project'."]}
    client, _, _ = make_client(FakeResponse(400, body))
    with pytest.raises(JiraError, match="does not exist for the field 'project'"):
        list(client.search_issues("NOPE", None))


def test_wait_ends_when_stopping() -> None:
    stopping = {"now": False}
    client, session, slept = make_client(
        FakeResponse(429, None, {"Retry-After": "30"}),
        should_stop=lambda: stopping["now"],
    )

    def sleep_then_stop(seconds: float) -> None:
        slept.append(seconds)
        stopping["now"] = True

    client._sleep = sleep_then_stop
    with pytest.raises(JiraInterrupted):
        list(client.search_issues("PROJ", None))
    assert len(session.requests) == 1
    assert sum(slept) < 30
