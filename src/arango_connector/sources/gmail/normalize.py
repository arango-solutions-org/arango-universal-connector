"""Pure mapping from a Gmail API message (``format=full``) to an Envelope. No I/O."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any

from arango_connector.envelope import Envelope, Scope, make_key
from arango_connector.payloads import EmailAddress, EmailPayload
from arango_connector.sources.gmail.config import KeyMode, RawMode

SOURCE = "gmail"

_WHITESPACE = re.compile(r"\s+")

# Headers dropped from ``raw`` when bcc is not stored, so it cannot leak back in.
_BCC_HEADERS = frozenset({"bcc", "x-original-bcc"})


@dataclass(frozen=True)
class NormalizeOptions:
    key_mode: KeyMode = "hashed"
    store_body: bool = True
    store_bcc: bool = True
    raw_mode: RawMode = "stripped"


def mailbox_key(mailbox: str, mode: KeyMode) -> str:
    """The mailbox part of keys, scopes and cursors: a stable hash, or the address."""
    mailbox = mailbox.lower()
    if mode == "plain":
        return mailbox
    return hashlib.sha256(mailbox.encode()).hexdigest()[:16]


# -- headers ------------------------------------------------------------------


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    """Top-level headers by lower-cased name. Repeated headers keep the first value."""
    result: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name", "")).lower()
        if name and name not in result:
            result[name] = str(header.get("value", ""))
    return result


def _decode_words(value: str) -> str:
    """Decode RFC 2047 encoded-words (``=?utf-8?q?...?=``), tolerating bad input."""
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError, binascii.Error):
        return value


def _addresses(value: str | None) -> list[EmailAddress]:
    if not value:
        return []
    # Parse before decoding names: a decoded name may contain commas.
    result = []
    for name, address in getaddresses([value]):
        if not address or "@" not in address:
            continue
        decoded = _decode_words(name).strip() if name else ""
        result.append(EmailAddress(name=decoded or None, address=address.strip()))
    return result


def _message_ids(value: str | None) -> list[str]:
    return value.split() if value else []


# -- body ---------------------------------------------------------------------


class _HTMLText(HTMLParser):
    """Collects readable text from HTML: skips script/style, breaks lines at block tags.

    Source whitespace collapses to single spaces, as a browser renders it. ``<br>`` is
    a hard line break; other block tags only start a new line if one isn't started.
    """

    _BLOCKS = frozenset(
        {
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "hr",
            "ul",
            "ol",
            "table",
        }
    )
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def _soft_break(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "br":
            self._parts.append("\n")
        elif tag in self._BLOCKS:
            self._soft_break()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCKS:
            self._soft_break()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(_WHITESPACE.sub(" ", data))

    def text(self) -> str:
        lines = (line.strip() for line in "".join(self._parts).split("\n"))
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):  # at most one blank line in a row
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _charset(part: dict[str, Any]) -> str:
    content_type = _headers(part).get("content-type", "")
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip().strip('"')
    return "utf-8"


def _decode_data(part: dict[str, Any]) -> str | None:
    """Decode a part's body. Gmail has already undone the transfer encoding."""
    data = (part.get("body") or {}).get("data")
    if not data:
        return None
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:  # unknown charset name
        return raw.decode("utf-8", errors="replace")


def _is_attachment(part: dict[str, Any]) -> bool:
    if part.get("filename"):
        return True
    disposition = _headers(part).get("content-disposition", "")
    return disposition.lower().startswith("attachment")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    """Every MIME part, depth first, including ``part`` itself."""
    parts = [part]
    for child in part.get("parts") or []:
        parts.extend(_walk(child))
    return parts


def extract_body(payload: dict[str, Any]) -> str | None:
    """The message text: the first inline text/plain part, else text/html as text."""
    parts = [p for p in _walk(payload) if not _is_attachment(p)]
    for mime, convert in (("text/plain", None), ("text/html", html_to_text)):
        for part in parts:
            if str(part.get("mimeType", "")).lower() == mime:
                text = _decode_data(part)
                if text is not None:
                    return convert(text) if convert else text
    return None


def _attachment_names(payload: dict[str, Any]) -> list[str]:
    return [str(p["filename"]) for p in _walk(payload) if p.get("filename")]


# -- raw ----------------------------------------------------------------------


def _strip_body_data(part: dict[str, Any]) -> None:
    body = part.get("body")
    if isinstance(body, dict):
        body.pop("data", None)
    for child in part.get("parts") or []:
        _strip_body_data(child)


def _drop_headers(part: dict[str, Any], names: frozenset[str]) -> None:
    if "headers" in part:
        part["headers"] = [
            h for h in part["headers"] if str(h.get("name", "")).lower() not in names
        ]
    for child in part.get("parts") or []:
        _drop_headers(child, names)


def build_raw(message: dict[str, Any], options: NormalizeOptions) -> dict[str, Any]:
    """The message as stored in ``raw``, honouring raw mode and redaction toggles.

    Redaction wins over raw mode: with the body or bcc not stored, ``full`` is
    trimmed too, so ``raw`` can never carry what the payload deliberately omits.
    """
    if options.raw_mode == "none":
        return {}
    raw = copy.deepcopy(message)
    payload = raw.get("payload")
    if isinstance(payload, dict):
        if options.raw_mode == "stripped" or not options.store_body:
            _strip_body_data(payload)
        if not options.store_bcc:
            _drop_headers(payload, _BCC_HEADERS)
    if not options.store_body:
        raw.pop("snippet", None)
    return raw


# -- message ------------------------------------------------------------------


def _internal_date(message: dict[str, Any]) -> datetime | None:
    try:
        millis = int(message["internalDate"])
    except (KeyError, TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def normalize(
    message: dict[str, Any],
    mailbox: str,
    options: NormalizeOptions,
    *,
    updated_at: datetime | None = None,
) -> Envelope[EmailPayload] | None:
    """Map a Gmail message to an Envelope, or ``None`` if it lacks an id.

    ``mailbox`` is the address the message was read from; ``updated_at`` is set by the
    adapter when the message is re-read because its labels changed.
    """
    message_id = message.get("id")
    if not message_id:
        return None

    payload = message.get("payload") or {}
    headers = _headers(payload)
    subject = headers.get("subject")
    senders = _addresses(headers.get("from"))
    snippet = message.get("snippet")
    key_part = mailbox_key(mailbox, options.key_mode)

    return Envelope[EmailPayload](
        key=make_key(SOURCE, key_part, message_id),
        source=SOURCE,
        source_id=message_id,
        scope=Scope(tenant_id=mailbox.partition("@")[2].lower(), container_id=key_part),
        created_at=_internal_date(message),
        updated_at=updated_at,
        payload=EmailPayload(
            subject=_decode_words(subject) if subject is not None else None,
            from_=senders[0] if senders else None,
            to=_addresses(headers.get("to")),
            cc=_addresses(headers.get("cc")),
            bcc=_addresses(headers.get("bcc")) if options.store_bcc else [],
            reply_to=_addresses(headers.get("reply-to")),
            message_id_header=headers.get("message-id") or None,
            in_reply_to=headers.get("in-reply-to") or None,
            references=_message_ids(headers.get("references")),
            thread_id=message.get("threadId"),
            label_ids=list(message.get("labelIds") or []),
            snippet=html.unescape(snippet) if snippet and options.store_body else None,
            body_text=extract_body(payload) if options.store_body else None,
            has_attachments=bool(_attachment_names(payload)),
            attachment_names=_attachment_names(payload),
        ),
        raw=build_raw(message, options),
    )
