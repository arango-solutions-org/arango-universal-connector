"""Thin wrapper over the Gmail API, so the adapter can be tested with a fake."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# googleapiclient's own retry: exponential backoff on 5xx, 429 and rate-limit 403s.
NUM_RETRIES = 5
_PAGE_SIZE = 500
_HISTORY_TYPES = ["messageAdded", "labelAdded", "labelRemoved"]


class HistoryExpiredError(Exception):
    """The start history id is too old (Gmail keeps roughly a week); resync needed."""


class GmailClient(Protocol):
    """The Gmail operations the adapter needs, for one mailbox."""

    def get_profile(self) -> dict[str, Any]: ...

    def list_labels(self) -> list[dict[str, Any]]: ...

    def list_history(self, start_history_id: str) -> Iterator[dict[str, Any]]:
        """Yield history pages (each with ``history`` and ``historyId``) in order.

        Raises ``HistoryExpiredError`` if ``start_history_id`` is no longer available.
        """
        ...

    def list_message_ids(self, query: str) -> Iterator[str]: ...

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """The message in ``format=full``, or ``None`` if it no longer exists."""
        ...


def _status(exc: HttpError) -> int:
    return int(getattr(exc.resp, "status", 0) or 0)


class GoogleGmailClient:
    """``GmailClient`` backed by google-api-python-client for one mailbox."""

    def __init__(self, credentials: Any = None, *, http: Any = None) -> None:
        """Pass ``credentials``; ``http`` (an authorised or mock transport) is for tests.

        Uses the discovery document bundled with google-api-python-client, so building
        the client makes no network call.
        """
        self._users = build(
            "gmail", "v1", credentials=credentials, http=http, cache_discovery=False
        ).users()

    def get_profile(self) -> dict[str, Any]:
        result: dict[str, Any] = self._users.getProfile(userId="me").execute(
            num_retries=NUM_RETRIES
        )
        return result

    def list_labels(self) -> list[dict[str, Any]]:
        result = self._users.labels().list(userId="me").execute(num_retries=NUM_RETRIES)
        return list(result.get("labels", []))

    def list_history(self, start_history_id: str) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            try:
                page: dict[str, Any] = (
                    self._users.history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=_HISTORY_TYPES,
                        maxResults=_PAGE_SIZE,
                        pageToken=page_token,
                    )
                    .execute(num_retries=NUM_RETRIES)
                )
            except HttpError as exc:
                if _status(exc) == 404:
                    raise HistoryExpiredError(start_history_id) from exc
                raise
            yield page
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def list_message_ids(self, query: str) -> Iterator[str]:
        page_token: str | None = None
        while True:
            page = (
                self._users.messages()
                .list(userId="me", q=query, maxResults=_PAGE_SIZE, pageToken=page_token)
                .execute(num_retries=NUM_RETRIES)
            )
            for message in page.get("messages", []):
                yield str(message["id"])
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = (
                self._users.messages()
                .get(userId="me", id=message_id, format="full")
                .execute(num_retries=NUM_RETRIES)
            )
        except HttpError as exc:
            if _status(exc) == 404:
                return None
            raise
        return result
