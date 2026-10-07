"""Gmail attachments: MIME part parsing, envelope mapping per mode, and adapter fetching."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from arango_connector.envelope import Envelope
from arango_connector.sources.gmail.normalize import (
    AttachmentOptions,
    NormalizeOptions,
    attachment_parts,
    normalize_attachment,
)
from gmail_fakes import FakeGmailClient, attachment, message, part, with_part_ids
from test_extract import make_pdf
from test_gmail_adapter import ADA, NOW, Clock, ids, make_adapter, poll

PDF = make_pdf("Quarterly numbers")
CSV = b"region,revenue\nEMEA,42\n"


def mail_with_attachments(message_id: str = "m1", **kw: Any) -> dict[str, Any]:
    body = with_part_ids(
        part(
            "multipart/mixed",
            parts=[
                part("text/plain", "see attached"),
                attachment("q2.pdf", "application/pdf", PDF),
                attachment("numbers.csv", "text/csv", CSV, inline_data=True),
                attachment("logo.png", "image/png", b"\x89PNG", inline=True, content_id="logo@x"),
            ],
        )
    )
    return message(message_id, body=body, **kw)


def _by_name(envs: list[Envelope[Any]]) -> dict[str, Envelope[Any]]:
    return {e.payload.filename: e for e in envs if e.kind == "attachment"}


# -- parsing and mapping ---------------------------------------------------------------


def test_attachment_parts() -> None:
    parts = attachment_parts(mail_with_attachments())
    assert [(p.part_id, p.filename, p.mime_type) for p in parts] == [
        ("1", "q2.pdf", "application/pdf"),
        ("2", "numbers.csv", "text/csv"),
        ("3", "logo.png", "image/png"),
    ]
    pdf, csv, logo = parts
    assert pdf.attachment_id == "ANGjdJ-q2.pdf" and pdf.data is None and pdf.size == len(PDF)
    assert csv.data is not None and csv.attachment_id is None
    assert logo.inline and logo.content_id == "logo@x"
    assert "data" not in str(csv.raw["body"])  # raw never carries the bytes


@pytest.mark.parametrize(
    ("mode", "content", "text"),
    [
        ("metadata", False, False),
        ("content", True, False),
        ("text", False, True),
        ("all", True, True),
    ],
)
def test_modes(mode: str, content: bool, text: bool) -> None:
    msg = mail_with_attachments()
    [pdf_part, *_] = attachment_parts(msg)
    data = PDF if mode != "metadata" else None

    env = normalize_attachment(
        msg, pdf_part, "ada@example.com", NormalizeOptions(), AttachmentOptions(mode), data=data
    )

    p = env.payload
    assert (p.content_base64 is not None) is content
    assert (p.text is not None) is text
    if content:
        assert base64.b64decode(p.content_base64) == PDF
    if text:
        assert "Quarterly numbers" in p.text
    assert p.sha256 == (hashlib.sha256(PDF).hexdigest() if data else None)


def test_attachment_envelope_links_to_its_email() -> None:
    msg = mail_with_attachments(thread_id="t1")
    [pdf_part, *_] = attachment_parts(msg)
    env = normalize_attachment(
        msg,
        pdf_part,
        "ada@example.com",
        NormalizeOptions(key_mode="plain"),
        AttachmentOptions("metadata"),
    )
    assert env.key == "gmail_ada@example.com_m1_att_1"
    assert env.kind == "attachment"
    assert env.source_id == "m1/1"
    assert env.payload.message_key == "gmail_ada@example.com_m1"
    assert env.payload.thread_id == "t1"
    assert env.created_at == datetime(2024, 5, 23, 21, 33, 20, tzinfo=UTC)  # the email's
    assert env.raw["filename"] == "q2.pdf"


def test_raw_none_mode_applies_to_attachments() -> None:
    msg = mail_with_attachments()
    [pdf_part, *_] = attachment_parts(msg)
    env = normalize_attachment(
        msg,
        pdf_part,
        "a@example.com",
        NormalizeOptions(raw_mode="none"),
        AttachmentOptions("metadata"),
    )
    assert env.raw == {}


def test_unsupported_text_type_records_reason() -> None:
    msg = mail_with_attachments()
    logo = attachment_parts(msg)[2]
    env = normalize_attachment(
        msg, logo, "a@example.com", NormalizeOptions(), AttachmentOptions("all"), data=b"\x89PNG"
    )
    assert env.payload.text is None
    assert env.payload.skipped_reason == "unsupported_type"
    assert env.payload.content_base64 is not None  # bytes are still stored


# -- adapter ---------------------------------------------------------------------------


def _setup(mode: str, **kw: Any) -> tuple[FakeGmailClient, Any]:
    gmail = FakeGmailClient()
    gmail.attachments[("m1", "ANGjdJ-q2.pdf")] = PDF
    gmail.attachments[("m1", "ANGjdJ-logo.png")] = b"\x89PNG"
    adapter, _ = make_adapter({ADA: gmail}, attachments=AttachmentOptions(mode, **kw))
    poll(adapter)
    return gmail, adapter


def test_off_emits_no_attachment_envelopes() -> None:
    gmail, adapter = _setup("off")
    gmail.deliver(mail_with_attachments())
    assert [e.kind for e in poll(adapter)] == ["email"]
    assert gmail.attachment_gets == []


def test_metadata_mode_never_downloads() -> None:
    gmail, adapter = _setup("metadata")
    gmail.deliver(mail_with_attachments())

    envs = poll(adapter)

    assert [e.kind for e in envs] == ["email", "attachment", "attachment", "attachment"]
    assert gmail.attachment_gets == []
    assert all(e.payload.content_base64 is None for e in envs[1:])


def test_all_mode_downloads_and_extracts() -> None:
    gmail, adapter = _setup("all")
    gmail.deliver(mail_with_attachments())

    atts = _by_name(poll(adapter))

    assert sorted(gmail.attachment_gets) == [("m1", "ANGjdJ-logo.png"), ("m1", "ANGjdJ-q2.pdf")]
    assert "Quarterly numbers" in atts["q2.pdf"].payload.text
    assert atts["numbers.csv"].payload.text == CSV.decode().strip()  # inline data, no fetch
    assert atts["logo.png"].payload.skipped_reason == "unsupported_type"


def test_too_large_attachments_are_metadata_only() -> None:
    gmail, adapter = _setup("content", max_bytes=100)
    gmail.deliver(mail_with_attachments())

    atts = _by_name(poll(adapter))

    assert atts["q2.pdf"].payload.skipped_reason == "too_large"
    assert atts["q2.pdf"].payload.content_base64 is None
    assert atts["numbers.csv"].payload.content_base64 is not None
    assert ("m1", "ANGjdJ-q2.pdf") not in gmail.attachment_gets


def test_vanished_attachment_is_marked_not_fatal() -> None:
    gmail, adapter = _setup("content")
    del gmail.attachments[("m1", "ANGjdJ-q2.pdf")]
    gmail.deliver(mail_with_attachments())

    atts = _by_name(poll(adapter))

    assert atts["q2.pdf"].payload.skipped_reason == "download_failed"


def test_read_toggle_does_not_re_download() -> None:
    gmail, adapter = _setup("content")
    gmail.deliver(mail_with_attachments(labels=("INBOX", "UNREAD")))
    poll(adapter)
    gmail.attachment_gets.clear()

    gmail.relabel("m1", remove=("UNREAD",))

    assert [e.kind for e in poll(adapter)] == ["email"]
    assert gmail.attachment_gets == []


def test_attachments_come_with_mail_that_gains_a_watched_label() -> None:
    gmail, adapter = _setup("metadata")
    gmail.deliver(mail_with_attachments(labels=("IMPORTANT",)))
    assert poll(adapter) == []

    gmail.relabel("m1", add=("INBOX",))

    envs = poll(adapter)
    assert [e.kind for e in envs] == ["email", "attachment", "attachment", "attachment"]
    assert all(e.updated_at is not None for e in envs)


def test_attachments_come_with_lookback() -> None:
    gmail = FakeGmailClient()
    gmail.deliver(mail_with_attachments(received=NOW))
    adapter, _ = make_adapter(
        {ADA: gmail},
        clock=Clock(NOW),
        initial_sync="lookback",
        attachments=AttachmentOptions("metadata"),
    )
    assert ids(poll(adapter)) == ["m1", "m1/1", "m1/2", "m1/3"]
