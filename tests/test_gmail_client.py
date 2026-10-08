"""GoogleGmailClient against canned HTTP responses (googleapiclient's HttpMockSequence)."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence

from arango_connector.sources.gmail.client import GoogleGmailClient, HistoryExpiredError


def _client(*responses: tuple[int, dict[str, Any]]) -> tuple[GoogleGmailClient, Any]:
    http = HttpMockSequence(
        [({"status": str(status)}, json.dumps(body)) for status, body in responses]
    )
    return GoogleGmailClient(http=http), http


def _error(status: int) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": status, "message": "x", "errors": [{"reason": "x"}]}}


def test_history_pages_are_followed() -> None:
    client, http = _client(
        (200, {"history": [{"id": "2"}], "historyId": "3", "nextPageToken": "p1"}),
        (200, {"history": [{"id": "3"}], "historyId": "3"}),
    )
    pages = list(client.list_history("1"))
    assert [p["history"][0]["id"] for p in pages] == ["2", "3"]
    first, second = (uri for uri, *_ in http.request_sequence)
    assert "startHistoryId=1" in first and "pageToken" not in first
    for kind in ("messageAdded", "labelAdded", "labelRemoved"):
        assert f"historyTypes={kind}" in first
    assert "pageToken=p1" in second


def test_history_404_means_expired() -> None:
    client, _ = _client(_error(404))
    with pytest.raises(HistoryExpiredError):
        list(client.list_history("1"))


def test_other_history_errors_propagate() -> None:
    client, _ = _client(_error(400))
    with pytest.raises(HttpError):
        list(client.list_history("1"))


def test_message_ids_are_paged() -> None:
    client, _ = _client(
        (200, {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t"}),
        (200, {"messages": [{"id": "c"}]}),
    )
    assert list(client.list_message_ids("after:1")) == ["a", "b", "c"]


def test_empty_message_list() -> None:
    client, _ = _client((200, {"resultSizeEstimate": 0}))
    assert list(client.list_message_ids("after:1")) == []


def test_get_message_404_is_none() -> None:
    client, _ = _client(_error(404))
    assert client.get_message("gone") is None


def test_get_message_retries_transient_errors() -> None:
    client, _ = _client(_error(503), (200, {"id": "m", "labelIds": ["INBOX"]}))
    assert client.get_message("m") == {"id": "m", "labelIds": ["INBOX"]}


def test_profile_and_labels() -> None:
    client, _ = _client(
        (200, {"emailAddress": "ada@example.com", "historyId": "42"}),
        (200, {"labels": [{"id": "INBOX", "name": "INBOX"}]}),
    )
    assert client.get_profile()["historyId"] == "42"
    assert client.list_labels() == [{"id": "INBOX", "name": "INBOX"}]


def test_thread_messages_are_ids_and_labels_only() -> None:
    client, http = _client(
        (200, {"id": "t", "messages": [{"id": "a", "labelIds": ["INBOX"]}, {"id": "b"}]})
    )
    assert client.get_thread_messages("t") == [
        {"id": "a", "labelIds": ["INBOX"]},
        {"id": "b", "labelIds": []},
    ]
    [(uri, *_)] = http.request_sequence
    assert "format=minimal" in uri


def test_missing_thread_is_empty() -> None:
    client, _ = _client(_error(404))
    assert client.get_thread_messages("gone") == []


def test_attachment_bytes_are_decoded() -> None:
    data = base64.urlsafe_b64encode(b"%PDF-1.4 bytes").decode().rstrip("=")
    client, http = _client((200, {"size": 14, "data": data}))
    assert client.get_attachment("m1", "att1") == b"%PDF-1.4 bytes"
    [(uri, *_)] = http.request_sequence
    assert "/messages/m1/attachments/att1" in uri


def test_missing_attachment_is_none() -> None:
    client, _ = _client(_error(404))
    assert client.get_attachment("m1", "gone") is None
