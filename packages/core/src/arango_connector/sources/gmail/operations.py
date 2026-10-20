"""Gmail operations shared by every runtime: label filtering and per-message envelopes.

Each function does one bounded piece of work against a ``GmailClient``. The standalone
service builds its history sync on them; a Links job calls them directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector.payloads import AttachmentPayload
from arango_connector.sources.gmail.client import GmailClient
from arango_connector.sources.gmail.normalize import (
    AttachmentOptions,
    AttachmentPart,
    NormalizeOptions,
    attachment_parts,
    decode_part_data,
    normalize_attachment,
)


@dataclass(frozen=True)
class LabelFilter:
    """Ingest a message carrying any ``include`` label and no ``exclude`` label."""

    include: frozenset[str]
    exclude: frozenset[str]

    def included(self, label_ids: Iterable[str]) -> bool:
        return bool(self.include.intersection(label_ids))

    def excluded(self, label_ids: Iterable[str]) -> bool:
        return bool(self.exclude.intersection(label_ids))

    def matches(self, label_ids: Iterable[str]) -> bool:
        labels = list(label_ids)
        return self.included(labels) and not self.excluded(labels)


def resolve_labels(
    configured: Iterable[str], available: list[dict[str, Any]]
) -> tuple[frozenset[str], list[str]]:
    """Map configured label IDs or names (case-insensitive) to this mailbox's label IDs.

    Returns the resolved IDs and the entries that matched nothing. User label IDs
    (``Label_123``) differ per mailbox, which is why names are accepted too.
    """
    by_id = {str(label["id"]): str(label["id"]) for label in available}
    by_name = {str(label.get("name", "")).lower(): str(label["id"]) for label in available}
    resolved: set[str] = set()
    unknown: list[str] = []
    for entry in configured:
        label_id = by_id.get(entry) or by_name.get(entry.lower())
        if label_id is None:
            unknown.append(entry)
        else:
            resolved.add(label_id)
    return frozenset(resolved), unknown


def thread_messages(
    client: GmailClient, threads: dict[str, list[dict[str, Any]]], message: dict[str, Any]
) -> list[dict[str, Any]]:
    """The message's thread as ``{id, labelIds}`` entries, cached in ``threads``."""
    thread_id = str(message.get("threadId") or message["id"])
    if thread_id not in threads:
        threads[thread_id] = client.get_thread_messages(thread_id)
    return threads[thread_id]


def qualifies(
    client: GmailClient,
    labels: LabelFilter,
    message: dict[str, Any],
    *,
    match_thread: bool,
    threads: dict[str, list[dict[str, Any]]],
) -> bool:
    """Whether a message passes the label filter.

    Excluded labels always apply to the message itself. With ``match_thread``, a message
    also qualifies when another non-excluded message in its thread carries a watched
    label (a labelled message sitting in spam does not qualify its thread).
    """
    own = message.get("labelIds") or []
    if labels.excluded(own):
        return False
    if labels.included(own):
        return True
    if not match_thread:
        return False
    return any(
        labels.matches(sibling.get("labelIds") or [])
        for sibling in thread_messages(client, threads, message)
    )


def attachment_bytes(
    client: GmailClient,
    message: dict[str, Any],
    part: AttachmentPart,
    attachments: AttachmentOptions,
) -> tuple[bytes | None, str | None]:
    """The attachment's bytes when the mode needs them, else why there are none.

    A transient API error propagates, so the caller can retry the whole message.
    """
    if not attachments.needs_bytes:
        return None, None
    if part.size > attachments.max_bytes:
        return None, "too_large"
    if part.data:
        return decode_part_data(part.data), None
    if part.attachment_id:
        data = client.get_attachment(str(message["id"]), part.attachment_id)
        if data is not None:
            return data, None
    return None, "download_failed"


def attachment_envelopes(
    client: GmailClient,
    message: dict[str, Any],
    mailbox: str,
    options: NormalizeOptions,
    attachments: AttachmentOptions,
    *,
    updated_at: datetime | None = None,
) -> Iterator[Envelope[AttachmentPayload]]:
    """One envelope per attachment of ``message``, fetching bytes when the mode needs them."""
    for part in attachment_parts(message):
        data, skipped_reason = attachment_bytes(client, message, part, attachments)
        yield normalize_attachment(
            message,
            part,
            mailbox,
            options,
            attachments,
            data=data,
            skipped_reason=skipped_reason,
            updated_at=updated_at,
        )


def after_query(since: datetime) -> str:
    """A Gmail search for mail received after ``since`` (epoch seconds, UTC)."""
    return f"after:{int(since.timestamp())}"
