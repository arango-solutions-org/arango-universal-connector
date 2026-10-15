"""Core source operations, called directly as a Links job does (no standalone adapter)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from arango_connector.envelope import Envelope, Scope
from arango_connector.payloads import MessagePayload
from arango_connector.sources.gmail.normalize import (
    AttachmentOptions,
    NormalizeOptions,
    attachment_parts,
)
from arango_connector.sources.gmail.operations import (
    after_query,
    attachment_bytes,
    attachment_envelopes,
)
from arango_connector.sources.jira.operations import comment_envelopes, scoped_jql
from arango_connector.sources.slack.operations import UserNames
from gmail_fakes import FakeGmailClient, attachment, message, part, with_part_ids
from jira_fakes import SITE, FakeJiraClient, comment, issue

# -- gmail ------------------------------------------------------------------------------

PDF = b"%PDF-1.4 tiny"


def _mail() -> dict[str, Any]:
    body = with_part_ids(
        part(
            "multipart/mixed",
            parts=[
                part("text/plain", "see attached"),
                attachment("q2.pdf", "application/pdf", PDF),
                attachment("notes.txt", "text/plain", b"inline notes", inline_data=True),
            ],
        )
    )
    return message("m1", body=body)


def test_attachment_bytes_reasons() -> None:
    gmail = FakeGmailClient()
    gmail.attachments[("m1", "ANGjdJ-q2.pdf")] = PDF
    msg = _mail()
    pdf, txt = attachment_parts(msg)

    assert attachment_bytes(gmail, msg, pdf, AttachmentOptions("metadata")) == (None, None)
    assert attachment_bytes(gmail, msg, pdf, AttachmentOptions("content")) == (PDF, None)
    assert attachment_bytes(gmail, msg, txt, AttachmentOptions("content")) == (
        b"inline notes",
        None,
    )
    assert attachment_bytes(gmail, msg, pdf, AttachmentOptions("content", max_bytes=3)) == (
        None,
        "too_large",
    )
    del gmail.attachments[("m1", "ANGjdJ-q2.pdf")]
    assert attachment_bytes(gmail, msg, pdf, AttachmentOptions("content")) == (
        None,
        "download_failed",
    )


def test_attachment_envelopes_one_per_part() -> None:
    gmail = FakeGmailClient()
    gmail.attachments[("m1", "ANGjdJ-q2.pdf")] = PDF
    envelopes = list(
        attachment_envelopes(
            gmail, _mail(), "ada@example.com", NormalizeOptions(), AttachmentOptions("content")
        )
    )
    assert [e.payload.filename for e in envelopes] == ["q2.pdf", "notes.txt"]
    assert all(e.payload.content_base64 for e in envelopes)


def test_after_query() -> None:
    assert after_query(datetime(2024, 1, 1, tzinfo=UTC)) == "after:1704067200"


# -- jira -------------------------------------------------------------------------------

T0 = datetime(2024, 5, 1, 9, 0, tzinfo=UTC)


def test_comment_envelopes_fetch_beyond_embedded_page_and_filter_by_time() -> None:
    jira = FakeJiraClient()
    old = comment("1", "old", created=T0)
    new = comment("2", "new", created=T0 + timedelta(days=2))
    jira.put_issue(issue())
    jira.add_comment("10001", old)
    jira.add_comment("10001", new)
    # Search results embedded only the first comment of two.
    found = issue(comments=[old], comment_total=2)

    every = list(comment_envelopes(jira, found, SITE))
    recent = list(comment_envelopes(jira, found, SITE, since=T0 + timedelta(days=1)))

    assert sorted(e.source_id for e in every) == ["1", "2"]
    assert [e.source_id for e in recent] == ["2"]
    assert jira.comment_fetches == ["10001", "10001"]


def test_comment_envelopes_use_embedded_comments_when_complete() -> None:
    jira = FakeJiraClient()
    found = issue(comments=[comment("1")], comment_total=1)
    assert len(list(comment_envelopes(jira, found, SITE))) == 1
    assert jira.comment_fetches == []


# -- slack ------------------------------------------------------------------------------


def _envelope(author_type: str = "user", author_id: str | None = "U1") -> Envelope[Any]:
    return Envelope[MessagePayload](
        key="slack_C1_1.0",
        source="slack",
        source_id="1.0",
        scope=Scope(container_id="C1"),
        created_at=None,
        payload=MessagePayload(author_type=author_type, author_id=author_id),  # type: ignore[arg-type]
        raw={},
    )


class FakeUsersInfo:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail_with = fail_with

    def __call__(self, *, user: str) -> dict[str, Any]:
        self.calls.append(user)
        if self.fail_with:
            raise self.fail_with
        return {"user": {"real_name": f"Real {user}"}}


def test_user_names_are_cached() -> None:
    lookup = FakeUsersInfo()
    names = UserNames(lookup)
    assert names.fill(_envelope()).payload.author_name == "Real U1"
    assert names.fill(_envelope()).payload.author_name == "Real U1"
    assert lookup.calls == ["U1"]


@pytest.mark.parametrize(
    "error", [SlackApiError("x", {"ok": False, "error": "missing_scope"}), RuntimeError("down")]
)
def test_failed_lookups_are_not_cached(error: Exception) -> None:
    lookup = FakeUsersInfo(fail_with=error)
    names = UserNames(lookup)
    assert names.get("U1") is None
    assert names.get("U1") is None
    assert lookup.calls == ["U1", "U1"]


def test_bots_and_authorless_messages_are_left_alone() -> None:
    lookup = FakeUsersInfo()
    names = UserNames(lookup)
    bot = _envelope(author_type="bot", author_id="B1")
    assert names.fill(bot) is bot
    assert names.fill(_envelope(author_id=None)).payload.author_name is None
    assert lookup.calls == []


@pytest.mark.parametrize(
    ("jql", "expected"),
    [
        ("status = Done", 'project in ("P") AND (status = Done)'),
        (
            'summary ~ "a) OR (b" ORDER BY key',
            'project in ("P") AND (summary ~ "a) OR (b") ORDER BY key',
        ),
        ("ORDER BY updated", 'project in ("P") ORDER BY updated'),
    ],
)
def test_scoped_jql(jql: str, expected: str) -> None:
    assert scoped_jql(jql, ["P"]) == expected


@pytest.mark.parametrize("jql", ["x) OR (project = SECRET", "x OR project = SECRET)", "(x"])
def test_scoped_jql_rejects_queries_that_could_escape_the_scope(jql: str) -> None:
    with pytest.raises(ValueError, match="unbalanced"):
        scoped_jql(jql, ["P"])
