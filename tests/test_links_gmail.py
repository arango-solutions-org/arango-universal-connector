"""The Gmail link's operations and scoping, against a fake Gmail and the fake sidecar."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from arango_connector.sources.gmail.config import GmailSettings
from arango_connector_links.link import RESULT_FILE, JobContext, JobError
from arango_connector_links.links.gmail import GmailLink, _range_query
from arango_connector_links.runtime import LinkRuntime
from gmail_fakes import FakeGmailClient, attachment, message, part, with_part_ids
from links_fakes import S, SidecarServer

ADA = "ada@example.com"
TEST = "Label_test"


def settings(**overrides: Any) -> GmailSettings:
    values: dict[str, Any] = {
        "gmail_mailboxes": [ADA],
        "gmail_labels": ["INBOX", "SENT", "test"],
        "gmail_exclude_labels": ["SPAM", "TRASH", "DRAFT"],
        "gmail_key_mode": "plain",
        "gmail_attachments": "all",
    } | overrides
    return GmailSettings.model_construct(**values)


@pytest.fixture
def gmail() -> FakeGmailClient:
    return FakeGmailClient(ADA, user_labels={TEST: "test", "Label_other": "other"})


def link(gmail: FakeGmailClient, **overrides: Any) -> GmailLink:
    return GmailLink(settings(**overrides), lambda mailbox: gmail)


def run(gmail_link: GmailLink, **job: Any) -> list[Any]:
    parsed = gmail_link.parse_input(json.dumps(job).encode())
    return list(gmail_link.run(parsed, JobContext(job_id="j1")))


def ids(envelopes: list[Any]) -> list[str]:
    return [e.source_id for e in envelopes if e.kind == "email"]


# -- scoping -------------------------------------------------------------------------


def test_mailbox_outside_the_allowlist_is_refused(gmail: FakeGmailClient) -> None:
    with pytest.raises(JobError, match="not one of this link's mailboxes"):
        run(link(gmail), operation="search", mailbox="eve@example.com", query="x")


def test_mailbox_match_ignores_case(gmail: FakeGmailClient) -> None:
    gmail.deliver(message("m1"))
    assert ids(run(link(gmail), operation="search", mailbox="ADA@Example.com", query="x")) == ["m1"]


def test_attachments_are_capped_by_configuration(gmail: FakeGmailClient) -> None:
    with pytest.raises(JobError, match="not allowed by this link"):
        run(
            link(gmail, gmail_attachments="metadata"),
            operation="search",
            mailbox=ADA,
            query="x",
            attachments="content",
        )


def test_excluded_and_unwatched_mail_is_never_returned(gmail: FakeGmailClient) -> None:
    gmail.deliver(message("inbox", labels=("INBOX",)))
    gmail.deliver(message("spam", labels=("INBOX", "SPAM")))
    gmail.deliver(message("other", labels=("Label_other",)))
    assert ids(run(link(gmail), operation="search", mailbox=ADA, query="x")) == ["inbox"]


def test_thread_rules_follow_the_configuration(gmail: FakeGmailClient) -> None:
    gmail.deliver(message("orig", labels=("INBOX", TEST)))
    gmail.deliver(message("reply", thread_id="orig", labels=("Label_other",)))
    by_thread = run(link(gmail), operation="search", mailbox=ADA, query="x")
    by_message = run(
        link(gmail, gmail_label_match="message"), operation="search", mailbox=ADA, query="x"
    )
    assert ids(by_thread) == ["orig", "reply"]
    assert ids(by_message) == ["orig"]


# -- operations ----------------------------------------------------------------------


def test_search_stops_at_max_results(gmail: FakeGmailClient) -> None:
    for n in range(5):
        gmail.deliver(message(f"m{n}"))
    assert len(run(link(gmail), operation="search", mailbox=ADA, query="x", max_results=2)) == 2


def test_get_thread_returns_every_message(gmail: FakeGmailClient) -> None:
    gmail.deliver(message("root"))
    gmail.deliver(message("r1", thread_id="root"))
    gmail.deliver(message("elsewhere"))
    assert ids(run(link(gmail), operation="get_thread", mailbox=ADA, thread_id="root")) == [
        "root",
        "r1",
    ]


def test_unknown_thread_fails(gmail: FakeGmailClient) -> None:
    with pytest.raises(JobError, match="not found"):
        run(link(gmail), operation="get_thread", mailbox=ADA, thread_id="nope")


def test_export_narrows_to_requested_labels(gmail: FakeGmailClient) -> None:
    gmail.deliver(message("inbox", labels=("INBOX",)))
    gmail.deliver(message("tagged", labels=(TEST,)))
    result = run(link(gmail), operation="export", mailbox=ADA, labels=["test"])
    assert ids(result) == ["tagged"]


def test_export_refuses_labels_outside_the_configuration(gmail: FakeGmailClient) -> None:
    with pytest.raises(JobError, match="within this link's labels: other"):
        run(link(gmail), operation="export", mailbox=ADA, labels=["other"])


def test_export_date_range_query() -> None:
    assert (
        _range_query(datetime(2026, 10, 1).date(), datetime(2026, 10, 15).date())
        == "after:2026/10/01 before:2026/10/15"
    )
    assert _range_query(None, None) == "in:anywhere"


def test_message_match_puts_the_labels_in_the_gmail_query(gmail: FakeGmailClient) -> None:
    run(link(gmail, gmail_label_match="message"), operation="export", mailbox=ADA)
    run(link(gmail, gmail_label_match="message"), operation="search", mailbox=ADA, query="a OR b")
    assert gmail.queries == [
        '(in:anywhere) {in:inbox label:"test" in:sent}',
        '(a OR b) {in:inbox label:"test" in:sent}',
    ]


def test_thread_match_leaves_the_query_alone(gmail: FakeGmailClient) -> None:
    # A reply without the label still qualifies through its thread, so Gmail can't filter.
    run(link(gmail, gmail_label_match="thread"), operation="search", mailbox=ADA, query="x")
    assert gmail.queries == ["x"]


def test_labels_gmail_search_cannot_name_are_left_to_the_code_filter(
    gmail: FakeGmailClient,
) -> None:
    run(
        link(gmail, gmail_label_match="message", gmail_labels=["INBOX", "TRASH"]),
        operation="search",
        mailbox=ADA,
        query="x",
    )
    assert gmail.queries == ["x"]


def test_attachments_are_included_when_requested(gmail: FakeGmailClient) -> None:
    body = with_part_ids(
        part(
            "multipart/mixed",
            parts=[
                part("text/plain", "see"),
                attachment("a.txt", "text/plain", b"hi", inline_data=True),
            ],
        )
    )
    gmail.deliver(message("m1", body=body))
    result = run(link(gmail), operation="search", mailbox=ADA, query="x", attachments="text")
    assert [e.kind for e in result] == ["email", "attachment"]
    assert result[1].payload.text == "hi"
    default = run(link(gmail), operation="search", mailbox=ADA, query="x")
    assert [e.kind for e in default] == ["email"]  # attachments are off unless asked for


@pytest.mark.parametrize(
    "job",
    [
        {"operation": "search", "mailbox": ADA},  # no query
        {"operation": "search", "mailbox": ADA, "query": "x", "surprise": 1},  # unknown field
        {"operation": "delete_everything", "mailbox": ADA},
    ],
)
def test_invalid_input_is_rejected(gmail: FakeGmailClient, job: dict[str, Any]) -> None:
    with pytest.raises(JobError, match="invalid input"):
        link(gmail).parse_input(json.dumps(job).encode())


def test_input_schema_lists_the_operations(gmail: FakeGmailClient) -> None:
    schema = link(gmail).input_schema()
    mapping = schema["discriminator"]["mapping"]
    assert set(mapping) == {"search", "get_thread", "export"}


# -- through the runtime ---------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[SidecarServer]:
    s = SidecarServer()
    yield s
    s.close()


def test_job_end_to_end_through_the_runtime(gmail: FakeGmailClient, server: SidecarServer) -> None:
    gmail.deliver(message("m1", received=datetime(2026, 10, 2, tzinfo=UTC)))
    job = server.sidecar.submit(
        json.dumps({"operation": "search", "mailbox": ADA, "query": "anything"}).encode()
    )
    LinkRuntime(link(gmail), server.stub).run_once()

    assert job.state == S.JOB_STATE_COMPLETED
    [record] = [json.loads(line) for line in job.files[RESULT_FILE].splitlines()]
    assert record["_key"] == "gmail_ada@example.com_m1"
    assert record["kind"] == "email"
    assert record["expires_at"] is None


def test_scope_violation_fails_the_job(gmail: FakeGmailClient, server: SidecarServer) -> None:
    job = server.sidecar.submit(
        json.dumps({"operation": "search", "mailbox": "eve@example.com", "query": "x"}).encode()
    )
    LinkRuntime(link(gmail), server.stub).run_once()
    assert job.state == S.JOB_STATE_FAILED
    assert "not one of this link's mailboxes" in job.descriptions[-1]
