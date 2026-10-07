"""Pure mapping from a Gmail API message (``format=full``) to an Envelope. No I/O."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any

from arango_connector.envelope import Envelope, Scope, make_key
from arango_connector.extract import extract_text, html_to_text
from arango_connector.payloads import AttachmentPayload, EmailAddress, EmailPayload
from arango_connector.sources.gmail.config import AttachmentMode, KeyMode, RawMode

SOURCE = "gmail"

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


# -- attachments ----------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentOptions:
    mode: AttachmentMode = "off"
    max_bytes: int = 10 * 1024 * 1024
    text_max_chars: int = 1_000_000

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def needs_bytes(self) -> bool:
        return self.mode in ("content", "text", "all")

    @property
    def stores_content(self) -> bool:
        return self.mode in ("content", "all")

    @property
    def extracts_text(self) -> bool:
        return self.mode in ("text", "all")


@dataclass(frozen=True)
class AttachmentPart:
    """One attachment as described by the message's MIME tree (no content)."""

    part_id: str
    filename: str
    mime_type: str
    size: int
    inline: bool
    content_id: str | None
    # Large bodies are fetched separately by id; small ones come inline as data.
    attachment_id: str | None
    data: str | None
    raw: dict[str, Any]


def attachment_parts(message: dict[str, Any]) -> list[AttachmentPart]:
    """Every part with a filename, in MIME order."""
    parts = []
    for part in _walk(message.get("payload") or {}):
        if not part.get("filename"):
            continue
        headers = _headers(part)
        body = part.get("body") or {}
        raw = copy.deepcopy(part)
        raw.pop("parts", None)
        _strip_body_data(raw)
        content_id = headers.get("content-id", "").strip().strip("<>") or None
        parts.append(
            AttachmentPart(
                part_id=str(part.get("partId") or "0"),
                filename=str(part["filename"]),
                mime_type=str(part.get("mimeType") or "application/octet-stream"),
                size=int(body.get("size") or 0),
                inline=headers.get("content-disposition", "").lower().startswith("inline"),
                content_id=content_id,
                attachment_id=body.get("attachmentId"),
                data=body.get("data"),
                raw=raw,
            )
        )
    return parts


def decode_part_data(data: str) -> bytes:
    """Decode Gmail's base64url body data."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def normalize_attachment(
    message: dict[str, Any],
    part: AttachmentPart,
    mailbox: str,
    options: NormalizeOptions,
    attachments: AttachmentOptions,
    *,
    data: bytes | None = None,
    skipped_reason: str | None = None,
    updated_at: datetime | None = None,
) -> Envelope[AttachmentPayload]:
    """Map one attachment to an Envelope. ``data`` is its bytes when they were fetched;
    ``skipped_reason`` says why they weren't although the mode wanted them."""
    message_id = str(message["id"])
    key_part = mailbox_key(mailbox, options.key_mode)

    content_base64 = sha256 = text = None
    truncated = False
    if data is not None:
        sha256 = hashlib.sha256(data).hexdigest()
        if attachments.stores_content:
            content_base64 = base64.b64encode(data).decode("ascii")
        if attachments.extracts_text:
            extracted = extract_text(
                data, part.mime_type, part.filename, max_chars=attachments.text_max_chars
            )
            text, truncated = extracted.text, extracted.truncated
            skipped_reason = skipped_reason or extracted.reason

    return Envelope[AttachmentPayload](
        key=make_key(SOURCE, key_part, message_id, "att", part.part_id),
        source=SOURCE,
        source_id=f"{message_id}/{part.part_id}",
        scope=Scope(tenant_id=mailbox.partition("@")[2].lower(), container_id=key_part),
        # The message's time, so an attachment expires together with its email.
        created_at=_internal_date(message),
        updated_at=updated_at,
        payload=AttachmentPayload(
            message_id=message_id,
            message_key=make_key(SOURCE, key_part, message_id),
            thread_id=message.get("threadId"),
            part_id=part.part_id,
            filename=part.filename,
            mime_type=part.mime_type,
            size=len(data) if data is not None else part.size,
            inline=part.inline,
            content_id=part.content_id,
            sha256=sha256,
            content_base64=content_base64,
            text=text,
            text_truncated=truncated,
            skipped_reason=skipped_reason,
        ),
        raw={} if options.raw_mode == "none" else part.raw,
    )
