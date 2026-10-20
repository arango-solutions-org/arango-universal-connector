"""Thin wrapper over the Jira Cloud REST API v3, so the adapter can be tested with a fake."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from arango_connector import __version__

logger = logging.getLogger(__name__)

# The issue fields the connector reads. Everything requested also lands in ``raw``.
ISSUE_FIELDS = (
    "summary",
    "description",
    "status",
    "issuetype",
    "priority",
    "project",
    "assignee",
    "reporter",
    "creator",
    "labels",
    "components",
    "parent",
    "resolution",
    "resolutiondate",
    "duedate",
    "created",
    "updated",
    "comment",
)

MAX_ATTEMPTS = 5
# Longest single wait between attempts, whatever Retry-After asks for.
MAX_RETRY_WAIT_SECONDS = 60.0
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_TIMEOUT = (10, 60)  # (connect, read) seconds
_WAIT_SLICE_SECONDS = 0.5
_COMMENT_PAGE_SIZE = 100
# The format JQL date comparisons accept. JQL reads it in the API user's time zone.
_JQL_TIME_FORMAT = "%Y/%m/%d %H:%M"


class JiraError(Exception):
    """Jira rejected a request. The message carries Jira's own error text, never secrets."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class JiraAuthError(JiraError):
    """401/403: the email or API token is wrong, or the account lacks permission."""


class JiraInterrupted(Exception):
    """A retry wait or a sync was cut short because the connector is stopping."""


class JiraClient(Protocol):
    """The Jira operations the adapter needs."""

    def search_issues(
        self, project: str, updated_since: datetime | None
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield pages of the project's issues updated at or after ``updated_since``.

        Issues come oldest-update first; ``None`` means every issue in the project.
        """
        ...

    def list_comments(self, issue_id: str) -> Iterator[dict[str, Any]]:
        """Yield every comment on the issue, oldest first. Nothing if the issue is gone."""
        ...

    def search_jql(self, jql: str) -> Iterator[list[dict[str, Any]]]:
        """Yield pages of issues matching ``jql``, with the connector's issue fields."""
        ...

    def get_issue(self, key: str) -> dict[str, Any] | None:
        """One issue by key or ID, or ``None`` if it doesn't exist or isn't visible."""
        ...

    def time_zone(self) -> tzinfo:
        """The account's time zone, which JQL dates are interpreted in."""
        ...


def build_jql(project: str, updated_since: str | None) -> str:
    """The incremental search for one project, oldest update first.

    ``updated_since`` is already formatted for JQL (see ``format_jql_time``). Ordering by
    ``updated`` lets the adapter checkpoint after every page; ``key`` breaks ties so pages
    are stable.
    """
    escaped = project.replace("\\", "\\\\").replace('"', '\\"')
    clauses = [f'project = "{escaped}"']
    if updated_since is not None:
        clauses.append(f'updated >= "{updated_since}"')
    return " AND ".join(clauses) + " ORDER BY updated ASC, key ASC"


def format_jql_time(moment: datetime, zone: tzinfo) -> str:
    """``moment`` as a JQL date in ``zone``, truncated to the minute (JQL's precision)."""
    return moment.astimezone(zone).strftime(_JQL_TIME_FORMAT)


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # an HTTP-date; fall back to our own backoff


def _error_text(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason or "error"
    if isinstance(body, dict):
        messages = list(body.get("errorMessages") or [])
        messages += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        if messages:
            return "; ".join(str(m) for m in messages)
    return response.reason or "error"


class RequestsJiraClient:
    """``JiraClient`` backed by ``requests``, authenticating with an email and API token.

    Retries 429 and 5xx responses and connection errors up to ``MAX_ATTEMPTS`` times,
    waiting ``Retry-After`` when Jira sends it and an exponential backoff with jitter
    otherwise. Waits end early once ``should_stop`` returns true.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        page_size: int = 100,
        should_stop: Callable[[], bool] = lambda: False,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._page_size = page_size
        self._should_stop = should_stop
        self._sleep = sleep
        self._session = session or requests.Session()
        self._session.auth = (email, api_token)
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": f"arango-connector/{__version__}"}
        )
        self._zone: tzinfo | None = None

    # -- JiraClient --------------------------------------------------------------

    def search_issues(
        self, project: str, updated_since: datetime | None
    ) -> Iterator[list[dict[str, Any]]]:
        since = format_jql_time(updated_since, self.time_zone()) if updated_since else None
        yield from self.search_jql(build_jql(project, since))

    def search_jql(self, jql: str) -> Iterator[list[dict[str, Any]]]:
        body: dict[str, Any] = {
            "jql": jql,
            "fields": list(ISSUE_FIELDS),
            "maxResults": self._page_size,
        }
        token: str | None = None
        while True:
            request = body if token is None else {**body, "nextPageToken": token}
            page = self._request("POST", "/rest/api/3/search/jql", json=request)
            yield [i for i in page.get("issues") or [] if isinstance(i, dict)]
            token = page.get("nextPageToken")
            # A missing isLast is not taken as the end: stopping early would let the
            # cursor jump past issues never read.
            if page.get("isLast") or not token:
                return

    def get_issue(self, key: str) -> dict[str, Any] | None:
        try:
            return self._request(
                # Encoded so a key can't add path segments (e.g. "../").
                "GET",
                f"/rest/api/3/issue/{quote(key, safe='')}",
                params={"fields": ",".join(ISSUE_FIELDS)},
            )
        except JiraError as exc:
            if exc.status == 404:
                return None
            raise

    def list_comments(self, issue_id: str) -> Iterator[dict[str, Any]]:
        start_at = 0
        while True:
            try:
                page = self._request(
                    "GET",
                    f"/rest/api/3/issue/{issue_id}/comment",
                    params={
                        "startAt": start_at,
                        "maxResults": _COMMENT_PAGE_SIZE,
                        "orderBy": "created",
                    },
                )
            except JiraError as exc:
                if exc.status == 404:
                    return  # deleted since the search; deletes are out of scope
                raise
            comments = [c for c in page.get("comments") or [] if isinstance(c, dict)]
            yield from comments
            start_at += len(comments)
            if not comments or start_at >= int(page.get("total") or 0):
                return

    # -- account -----------------------------------------------------------------

    def time_zone(self) -> tzinfo:
        """The API user's profile time zone, which JQL reads dates in. Cached."""
        if self._zone is None:
            me = self._request("GET", "/rest/api/3/myself")
            name = me.get("timeZone")
            try:
                self._zone = ZoneInfo(str(name)) if name else UTC
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("jira_unknown_time_zone time_zone=%s using UTC", name)
                self._zone = UTC
            logger.info(
                "jira_connected site=%s account_id=%s time_zone=%s",
                self.base_url,
                me.get("accountId"),
                self._zone,
            )
        return self._zone

    # -- transport ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._session.request(
                    method, url, params=params, json=json, timeout=_TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if last:
                    raise
                self._wait(self._backoff(attempt), path, reason=type(exc).__name__)
                continue

            status = response.status_code
            if status in _RETRYABLE_STATUSES and not last:
                retry_after = _retry_after(response)
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                reason = response.headers.get("RateLimit-Reason") or str(status)
                self._wait(delay, path, reason=reason)
                continue
            if status in (401, 403):
                raise JiraAuthError(status, _error_text(response))
            if status >= 400:
                raise JiraError(status, _error_text(response))
            result: dict[str, Any] = response.json()
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(2.0 * 2 ** (attempt - 1), MAX_RETRY_WAIT_SECONDS)) * random.uniform(
            0.7, 1.3
        )

    def _wait(self, seconds: float, path: str, *, reason: str) -> None:
        seconds = min(seconds, MAX_RETRY_WAIT_SECONDS)
        logger.warning(
            "jira_request_retry path=%s reason=%s wait_seconds=%.1f", path, reason, seconds
        )
        # Sliced so a stopping connector is not held up by a long Retry-After.
        remaining = seconds
        while remaining > 0:
            if self._should_stop():
                raise JiraInterrupted(path)
            step = min(remaining, _WAIT_SLICE_SECONDS)
            self._sleep(step)
            remaining -= step
