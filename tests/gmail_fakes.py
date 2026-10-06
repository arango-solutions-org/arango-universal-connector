"""Builders for Gmail API message shapes and an in-memory GmailClient for adapter tests."""

from __future__ import annotations

import base64
import copy
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from arango_connector.sources.gmail.client import HistoryExpiredError

SYSTEM_LABELS = ["INBOX", "SENT", "SPAM", "TRASH", "DRAFT", "UNREAD", "STARRED", "IMPORTANT"]


def b64(text: str, charset: str = "utf-8") -> str:
    """Gmail-style body data: base64url without padding."""
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def part(
    mime: str,
    text: str | None = None,
    *,
    charset: str = "utf-8",
    filename: str = "",
    headers: dict[str, str] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    all_headers = {"Content-Type": f"{mime}; charset={charset}"} | (headers or {})
    body: dict[str, Any] = {"size": len(text or "")}
    if text is not None:
        body["data"] = b64(text, charset)
    if filename:
        body = {"attachmentId": f"att-{filename}", "size": 1234}
    built: dict[str, Any] = {
        "partId": "",
        "mimeType": mime,
        "filename": filename,
        "headers": [{"name": k, "value": v} for k, v in all_headers.items()],
        "body": body,
    }
    if parts is not None:
        built["parts"] = parts
        built["body"] = {"size": 0}
    return built


def message(
    message_id: str = "18c2a1b2c3d4e5f6",
    *,
    thread_id: str | None = None,
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    received: datetime = datetime(2024, 5, 23, 21, 33, 20, tzinfo=UTC),
    snippet: str = "Hi Ada, the numbers for Q2 &amp; Q3 are attached",
) -> dict[str, Any]:
    default_headers = {
        "From": '"Grace Hopper" <grace@example.com>',
        "To": "Ada Lovelace <ada@example.com>, bob@example.com",
        "Subject": "Q2 numbers",
        "Message-ID": "<CAF=abc123@mail.gmail.com>",
        "Date": "Thu, 23 May 2024 21:33:20 +0000",
    }
    payload = copy.deepcopy(body) if body is not None else part("text/plain", "Hi Ada,\nSee Q2.")
    top_headers = default_headers if headers is None else headers
    payload["headers"] = [{"name": k, "value": v} for k, v in top_headers.items()] + [
        h for h in payload.get("headers", []) if h["name"].lower().startswith("content-")
    ]
    return {
        "id": message_id,
        "threadId": thread_id or message_id,
        "labelIds": list(labels),
        "snippet": snippet,
        "historyId": "1000",
        "internalDate": str(int(received.timestamp() * 1000)),
        "sizeEstimate": 4096,
        "payload": payload,
    }


class FakeGmailClient:
    """An in-memory mailbox implementing the GmailClient protocol.

    Every change appends a history record with a new history id, like Gmail does.
    """

    def __init__(
        self,
        address: str = "ada@example.com",
        user_labels: dict[str, str] | None = None,
        page_size: int = 2,
    ) -> None:
        self.address = address
        self.history_id = 1000
        self.messages: dict[str, dict[str, Any]] = {}
        self.records: list[tuple[int, dict[str, Any]]] = []
        self.labels = [{"id": label, "name": label, "type": "system"} for label in SYSTEM_LABELS]
        for label_id, name in (user_labels or {}).items():
            self.labels.append({"id": label_id, "name": name, "type": "user"})
        self.page_size = page_size
        self.expired_before: int | None = None
        self.fail_get: set[str] = set()
        self.queries: list[str] = []
        self.gets: list[str] = []

    # -- mutations used by tests -------------------------------------------

    def _record(self, **change: Any) -> None:
        self.history_id += 1
        self.records.append((self.history_id, {"id": str(self.history_id), **change}))

    def deliver(self, msg: dict[str, Any]) -> None:
        self.messages[msg["id"]] = msg
        ref = {"id": msg["id"], "threadId": msg["threadId"], "labelIds": msg["labelIds"]}
        self._record(messagesAdded=[{"message": ref}])

    def relabel(self, message_id: str, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()):
        msg = self.messages[message_id]
        msg["labelIds"] = [lbl for lbl in msg["labelIds"] if lbl not in remove] + list(add)
        ref = {"id": message_id, "labelIds": msg["labelIds"]}
        if add:
            self._record(labelsAdded=[{"message": ref, "labelIds": list(add)}])
        if remove:
            self._record(labelsRemoved=[{"message": ref, "labelIds": list(remove)}])

    def delete(self, message_id: str) -> None:
        del self.messages[message_id]
        self._record(messagesDeleted=[{"message": {"id": message_id}}])

    # -- GmailClient ---------------------------------------------------------

    def get_profile(self) -> dict[str, Any]:
        return {"emailAddress": self.address, "historyId": str(self.history_id)}

    def list_labels(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.labels)

    def list_history(self, start_history_id: str) -> Iterator[dict[str, Any]]:
        start = int(start_history_id)
        if self.expired_before is not None and start < self.expired_before:
            raise HistoryExpiredError(start_history_id)
        records = [r for hid, r in self.records if hid > start]
        if not records:
            yield {"historyId": str(self.history_id)}
            return
        for i in range(0, len(records), self.page_size):
            page: dict[str, Any] = {
                "history": copy.deepcopy(records[i : i + self.page_size]),
                "historyId": str(self.history_id),
            }
            if i + self.page_size < len(records):
                page["nextPageToken"] = f"p{i}"
            yield page

    def list_message_ids(self, query: str) -> Iterator[str]:
        self.queries.append(query)
        match = re.fullmatch(r"after:(\d+)", query)
        assert match, f"unexpected query {query!r}"
        after_ms = int(match.group(1)) * 1000
        for message_id, msg in self.messages.items():
            if int(msg["internalDate"]) > after_ms:
                yield message_id

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        self.gets.append(message_id)
        if message_id in self.fail_get:
            raise ConnectionError(f"gmail unavailable fetching {message_id}")
        msg = self.messages.get(message_id)
        return copy.deepcopy(msg) if msg is not None else None
