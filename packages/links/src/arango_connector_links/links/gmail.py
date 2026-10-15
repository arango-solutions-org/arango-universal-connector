"""Gmail as a Link: search, read threads and export mail on demand.

Uses the same settings as the standalone Gmail source. In a link they set the scope a
job may touch: ``GMAIL_MAILBOXES`` lists the mailboxes jobs may read, ``GMAIL_LABELS``
and ``GMAIL_EXCLUDE_LABELS`` filter every message returned (with the same thread rules,
``GMAIL_LABEL_MATCH``), and ``GMAIL_ATTACHMENTS`` is the most a job may ask for.
Settings that only make sense for continuous sync (polling, initial sync) are ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arango_connector.envelope import Envelope
from arango_connector.sources.gmail.client import GmailClient
from arango_connector.sources.gmail.config import AttachmentMode, GmailSettings
from arango_connector.sources.gmail.normalize import (
    AttachmentOptions,
    NormalizeOptions,
    normalize,
)
from arango_connector.sources.gmail.operations import (
    LabelFilter,
    attachment_envelopes,
    qualifies,
    resolve_labels,
)
from arango_connector_links.link import Example, JobContext, JobError, Link

# What a configured GMAIL_ATTACHMENTS mode lets a job ask for.
_ALLOWED_ATTACHMENTS: dict[str, set[str]] = {
    "off": {"off"},
    "metadata": {"off", "metadata"},
    "content": {"off", "metadata", "content"},
    "text": {"off", "metadata", "text"},
    "all": {"off", "metadata", "content", "text", "all"},
}


class _Common(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox: str = Field(description="Mailbox to read. Must be one of the link's mailboxes.")
    attachments: AttachmentMode = Field(
        default="off",
        description="Attachment documents to include: off, metadata, content (file as "
        "base64), text (extracted text) or all. Limited by the link's configuration.",
    )


class Search(_Common):
    """Messages matching a Gmail search query."""

    operation: Literal["search"]
    query: str = Field(
        min_length=1, description='Gmail search syntax, e.g. "from:ada subject:invoice".'
    )
    max_results: int = Field(default=100, ge=1, le=1000, description="Emails to return.")


class GetThread(_Common):
    """Every message in one thread."""

    operation: Literal["get_thread"]
    thread_id: str = Field(min_length=1, description="Gmail thread ID.")


class Export(_Common):
    """Messages received in a date range, optionally narrowed to some labels."""

    operation: Literal["export"]
    after: date | None = Field(default=None, description="Received on or after this date.")
    before: date | None = Field(default=None, description="Received before this date.")
    labels: list[str] | None = Field(
        default=None,
        description="Only these labels (IDs or names); must be within the link's labels.",
    )
    max_results: int = Field(default=1000, ge=1, le=10000, description="Emails to return.")


GmailInput = Annotated[Search | GetThread | Export, Field(discriminator="operation")]


class GmailLink(Link[Search | GetThread | Export]):
    name = "gmail"
    description = (
        "Read email from Gmail: search with Gmail query syntax, fetch a whole thread, or "
        "export a date range. Returns one document per email, plus one per attachment "
        "when requested, as JSONL."
    )
    tags = ("email", "gmail", "google-workspace")
    input_type = GmailInput
    examples = (
        Example(
            "search",
            {"operation": "search", "mailbox": "ada@example.com", "query": "subject:invoice"},
            "Emails whose subject mentions invoice.",
        ),
        Example(
            "get_thread",
            {"operation": "get_thread", "mailbox": "ada@example.com", "thread_id": "18c2a1b2"},
            "Every email in the thread.",
        ),
        Example(
            "export",
            {
                "operation": "export",
                "mailbox": "ada@example.com",
                "after": "2026-10-01",
                "labels": ["INBOX"],
                "attachments": "metadata",
            },
            "Inbox mail since 1 October, with attachment metadata.",
        ),
    )

    def __init__(
        self, settings: GmailSettings, client_factory: Callable[[str], GmailClient]
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._clients: dict[str, GmailClient] = {}
        self._labels: dict[str, LabelFilter] = {}
        self._available_labels: dict[str, list[dict[str, Any]]] = {}

    def run(
        self, job_input: Search | GetThread | Export, ctx: JobContext
    ) -> Iterator[Envelope[Any]]:
        mailbox = job_input.mailbox.strip().lower()
        if mailbox not in self.settings.gmail_mailboxes:
            raise JobError(f"mailbox {mailbox!r} is not one of this link's mailboxes")
        allowed = _ALLOWED_ATTACHMENTS[self.settings.gmail_attachments]
        if job_input.attachments not in allowed:
            raise JobError(
                f"attachments={job_input.attachments!r} is not allowed by this link "
                f"(allowed: {', '.join(sorted(allowed))})"
            )

        client = self._client(mailbox)
        labels = self._label_filter(mailbox, client)
        limit: int | None = None
        if isinstance(job_input, Search):
            ids: Iterable[str] = client.list_message_ids(job_input.query)
            limit = job_input.max_results
        elif isinstance(job_input, GetThread):
            thread = client.get_thread_messages(job_input.thread_id)
            if not thread:
                raise JobError(f"thread {job_input.thread_id!r} not found")
            ids = [str(m["id"]) for m in thread]
        else:
            labels = self._narrow(mailbox, labels, job_input.labels)
            ids = client.list_message_ids(_range_query(job_input.after, job_input.before))
            limit = job_input.max_results

        options = NormalizeOptions(
            key_mode=self.settings.gmail_key_mode,
            store_body=self.settings.gmail_store_body,
            store_bcc=self.settings.gmail_store_bcc,
            raw_mode=self.settings.gmail_raw_mode,
        )
        attachments = AttachmentOptions(
            mode=job_input.attachments,
            max_bytes=self.settings.gmail_attachment_max_bytes,
            text_max_chars=self.settings.gmail_attachment_text_max_chars,
        )
        threads: dict[str, list[dict[str, Any]]] = {}
        match_thread = self.settings.gmail_label_match == "thread"
        emails = 0
        for message_id in ids:
            ctx.check()
            if limit is not None and emails >= limit:
                return
            message = client.get_message(message_id)
            if message is None:
                continue  # deleted meanwhile
            if not qualifies(client, labels, message, match_thread=match_thread, threads=threads):
                continue
            envelope = normalize(message, mailbox, options)
            if envelope is None:
                continue
            yield envelope
            emails += 1
            if attachments.enabled:
                yield from attachment_envelopes(client, message, mailbox, options, attachments)

    # -- per-mailbox resources ------------------------------------------------------

    def _client(self, mailbox: str) -> GmailClient:
        if mailbox not in self._clients:
            self._clients[mailbox] = self._client_factory(mailbox)
        return self._clients[mailbox]

    def _available(self, mailbox: str, client: GmailClient) -> list[dict[str, Any]]:
        if mailbox not in self._available_labels:
            self._available_labels[mailbox] = client.list_labels()
        return self._available_labels[mailbox]

    def _label_filter(self, mailbox: str, client: GmailClient) -> LabelFilter:
        if mailbox not in self._labels:
            available = self._available(mailbox, client)
            include, _ = resolve_labels(self.settings.gmail_labels, available)
            exclude, _ = resolve_labels(self.settings.gmail_exclude_labels, available)
            self._labels[mailbox] = LabelFilter(include, exclude)
        return self._labels[mailbox]

    def _narrow(
        self, mailbox: str, labels: LabelFilter, requested: list[str] | None
    ) -> LabelFilter:
        """Restrict the include labels to ``requested``, which must lie within them."""
        if not requested:
            return labels
        available = self._available(mailbox, self._client(mailbox))
        wanted: set[str] = set()
        refused: list[str] = []
        for entry in requested:
            resolved, _ = resolve_labels([entry], available)
            if not resolved or not resolved <= labels.include:
                refused.append(entry)  # report what the caller wrote, not Gmail's ID
            wanted |= resolved
        if refused:
            raise JobError(
                "labels must be existing labels within this link's labels: "
                + ", ".join(sorted(refused))
            )
        return LabelFilter(frozenset(wanted), labels.exclude)


def _range_query(after: date | None, before: date | None) -> str:
    """A Gmail query for a received-date range (Gmail dates are YYYY/MM/DD)."""
    parts = []
    if after is not None:
        parts.append(f"after:{after:%Y/%m/%d}")
    if before is not None:
        parts.append(f"before:{before:%Y/%m/%d}")
    # Gmail needs some query; "in:anywhere" also includes archived mail, which the label
    # filter then narrows. Spam and trash stay excluded by GMAIL_EXCLUDE_LABELS.
    return " ".join(parts) or "in:anywhere"
