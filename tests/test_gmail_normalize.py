"""Unit tests for the pure Gmail message -> Envelope mapping."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest

from arango_connector.payloads import EmailAddress
from arango_connector.sources.gmail.normalize import (
    NormalizeOptions,
    extract_body,
    html_to_text,
    mailbox_key,
    normalize,
)
from gmail_fakes import message, part

MAILBOX = "Ada@Example.com"
HASHED = hashlib.sha256(b"ada@example.com").hexdigest()[:16]
DEFAULT = NormalizeOptions()


def test_field_mapping() -> None:
    env = normalize(message(), MAILBOX, DEFAULT)

    assert env is not None
    assert env.key == f"gmail_{HASHED}_18c2a1b2c3d4e5f6"
    assert env.source == "gmail"
    assert env.kind == "email"
    assert env.source_id == "18c2a1b2c3d4e5f6"
    assert env.scope.tenant_id == "example.com"
    assert env.scope.container_id == HASHED
    assert env.created_at == datetime(2024, 5, 23, 21, 33, 20, tzinfo=UTC)
    assert env.updated_at is None

    p = env.payload
    assert p.subject == "Q2 numbers"
    assert p.from_ == EmailAddress(name="Grace Hopper", address="grace@example.com")
    assert p.to == [
        EmailAddress(name="Ada Lovelace", address="ada@example.com"),
        EmailAddress(address="bob@example.com"),
    ]
    assert p.cc == [] and p.bcc == [] and p.reply_to == []
    assert p.message_id_header == "<CAF=abc123@mail.gmail.com>"
    assert p.thread_id == "18c2a1b2c3d4e5f6"
    assert p.label_ids == ["INBOX", "UNREAD"]
    assert p.snippet == "Hi Ada, the numbers for Q2 & Q3 are attached"  # entities decoded
    assert p.body_text == "Hi Ada,\nSee Q2."
    assert p.has_attachments is False


def test_plain_key_mode_uses_the_lowercased_address() -> None:
    env = normalize(message(), MAILBOX, NormalizeOptions(key_mode="plain"))
    assert env is not None
    assert env.key == "gmail_ada@example.com_18c2a1b2c3d4e5f6"
    assert env.scope.container_id == "ada@example.com"


def test_mailbox_key_is_case_insensitive() -> None:
    assert mailbox_key("ADA@example.com", "hashed") == mailbox_key("ada@EXAMPLE.com", "hashed")


def test_threading_headers() -> None:
    msg = message(
        "m2",
        thread_id="t1",
        headers={
            "From": "bob@example.com",
            "Subject": "Re: Q2 numbers",
            "In-Reply-To": "<CAF=abc123@mail.gmail.com>",
            "References": "<root@x> <CAF=abc123@mail.gmail.com>",
            "Cc": "carol@example.com",
            "Reply-To": "team@example.com",
        },
    )
    env = normalize(msg, MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.thread_id == "t1"
    assert env.payload.in_reply_to == "<CAF=abc123@mail.gmail.com>"
    assert env.payload.references == ["<root@x>", "<CAF=abc123@mail.gmail.com>"]
    assert env.payload.cc == [EmailAddress(address="carol@example.com")]
    assert env.payload.reply_to == [EmailAddress(address="team@example.com")]


def test_encoded_words_are_decoded_after_address_parsing() -> None:
    msg = message(
        headers={
            # "Doe, Jöhn" contains a comma once decoded; parsing first keeps one address.
            "From": "=?utf-8?q?Doe=2C_J=C3=B6hn?= <john@example.com>",
            "Subject": "=?utf-8?b?R3LDvMOfZQ==?= aus Berlin",
            "To": "ada@example.com",
        }
    )
    env = normalize(msg, MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.from_ == EmailAddress(name="Doe, Jöhn", address="john@example.com")
    assert env.payload.subject == "Grüße aus Berlin"


def test_missing_headers_are_empty_not_errors() -> None:
    env = normalize(message(headers={}), MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.subject is None
    assert env.payload.from_ is None
    assert env.payload.to == []
    assert env.payload.message_id_header is None


def test_multipart_alternative_prefers_plain_text() -> None:
    body = part(
        "multipart/alternative",
        parts=[part("text/plain", "plain version"), part("text/html", "<p>html version</p>")],
    )
    env = normalize(message(body=body), MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.body_text == "plain version"


def test_html_only_body_becomes_text() -> None:
    html_body = (
        "<html><head><style>p{color:red}</style><title>x</title></head><body>"
        "<p>Hello&nbsp;<b>Ada</b> &amp; team</p><script>alert(1)</script>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )
    env = normalize(message(body=part("text/html", html_body)), MAILBOX, DEFAULT)
    assert env is not None
    # &nbsp; renders as a space, like any other whitespace.
    assert env.payload.body_text == "Hello Ada & team\none\ntwo"


def test_nested_multipart_with_attachments() -> None:
    body = part(
        "multipart/mixed",
        parts=[
            part(
                "multipart/alternative",
                parts=[part("text/plain", "see attached"), part("text/html", "<p>see</p>")],
            ),
            part("application/pdf", filename="q2.pdf"),
            # A text attachment must not be mistaken for the body.
            part("text/plain", "csv,data", filename="numbers.csv"),
        ],
    )
    env = normalize(message(body=body), MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.body_text == "see attached"
    assert env.payload.has_attachments is True
    assert env.payload.attachment_names == ["q2.pdf", "numbers.csv"]


def test_inline_disposition_attachment_without_filename_is_not_the_body() -> None:
    body = part(
        "multipart/mixed",
        parts=[
            part("text/plain", "attached log", headers={"Content-Disposition": "attachment"}),
            part("text/plain", "real body"),
        ],
    )
    assert extract_body(message(body=body)["payload"]) == "real body"


def test_non_utf8_charset() -> None:
    body = part("text/plain", "Café crème", charset="iso-8859-1")
    env = normalize(message(body=body), MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.body_text == "Café crème"


def test_unknown_charset_falls_back_to_utf8() -> None:
    body = part("text/plain", "hello")
    body["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=x-bogus"}]
    assert extract_body(body) == "hello"


def test_no_body_parts() -> None:
    body = part("multipart/mixed", parts=[part("application/pdf", filename="a.pdf")])
    env = normalize(message(body=body), MAILBOX, DEFAULT)
    assert env is not None
    assert env.payload.body_text is None


def test_html_to_text_collapses_whitespace() -> None:
    assert html_to_text("<div>  a \n  b </div><br><br><br><p>c</p>") == "a b\n\nc"


# -- raw modes and redaction ---------------------------------------------------


def _body_data_present(raw: dict[str, object]) -> bool:
    return '"data"' in json.dumps(raw)


def test_stripped_raw_drops_body_data_but_keeps_structure() -> None:
    env = normalize(message(), MAILBOX, DEFAULT)
    assert env is not None
    assert not _body_data_present(env.raw)
    assert env.raw["payload"]["mimeType"] == "text/plain"
    assert env.raw["labelIds"] == ["INBOX", "UNREAD"]


def test_full_raw_keeps_everything() -> None:
    msg = message()
    env = normalize(msg, MAILBOX, NormalizeOptions(raw_mode="full"))
    assert env is not None
    assert env.raw == msg


def test_no_raw() -> None:
    env = normalize(message(), MAILBOX, NormalizeOptions(raw_mode="none"))
    assert env is not None
    assert env.raw == {}


def test_store_body_false_redacts_body_everywhere_even_with_full_raw() -> None:
    env = normalize(message(), MAILBOX, NormalizeOptions(store_body=False, raw_mode="full"))
    assert env is not None
    assert env.payload.body_text is None
    assert env.payload.snippet is None
    assert not _body_data_present(env.raw)
    assert "snippet" not in env.raw
    assert "Q2 &amp; Q3" not in json.dumps(env.to_document())
    # Headers are metadata and stay.
    assert env.payload.subject == "Q2 numbers"


def test_store_bcc_false_redacts_bcc_from_payload_and_raw_headers() -> None:
    headers = {"From": "ada@example.com", "To": "bob@example.com", "Bcc": "secret@example.com"}
    msg = message(headers=headers, labels=("SENT",))

    kept = normalize(msg, MAILBOX, DEFAULT)
    redacted = normalize(msg, MAILBOX, NormalizeOptions(store_bcc=False, raw_mode="full"))

    assert kept is not None and redacted is not None
    assert kept.payload.bcc == [EmailAddress(address="secret@example.com")]
    assert redacted.payload.bcc == []
    assert "secret@example.com" not in json.dumps(redacted.to_document())


def test_input_is_not_mutated() -> None:
    msg = message()
    original = copy.deepcopy(msg)
    normalize(msg, MAILBOX, NormalizeOptions(store_body=False, store_bcc=False))
    assert msg == original


def test_message_without_id_is_skipped() -> None:
    msg = message()
    del msg["id"]
    assert normalize(msg, MAILBOX, DEFAULT) is None


def test_missing_internal_date_gives_no_created_at() -> None:
    msg = message()
    del msg["internalDate"]
    env = normalize(msg, MAILBOX, DEFAULT)
    assert env is not None
    assert env.created_at is None


def test_updated_at_is_passed_through() -> None:
    when = datetime(2024, 6, 1, tzinfo=UTC)
    env = normalize(message(), MAILBOX, DEFAULT, updated_at=when)
    assert env is not None
    assert env.updated_at == when


def test_to_document_is_json_safe_and_uses_from() -> None:
    env = normalize(message(), MAILBOX, DEFAULT)
    assert env is not None
    doc = env.to_document()
    json.dumps(doc)
    assert doc["payload"]["from"] == {"name": "Grace Hopper", "address": "grace@example.com"}
    assert "from_" not in doc["payload"]
    assert doc["kind"] == "email"


@pytest.mark.parametrize("bad", ["", "not-base64!!"])
def test_bad_body_data_is_tolerated(bad: str) -> None:
    body = part("text/plain", "x")
    body["body"]["data"] = bad
    env = normalize(message(body=body), MAILBOX, DEFAULT)
    assert env is not None
