"""Typed payloads, one per entity kind.

Payloads are typed by *what* an envelope is, not *where* it came from: Slack, Teams and
Discord all produce ``message`` payloads, so cross-source queries see one shape.

TODO(future): kinds for upcoming sources follow the same pattern, e.g.

    class DocumentPayload(Payload):   # SharePoint, Drive
        kind: Literal["document"] = "document"
        title, path, mime_type, size, url, author, version, content

    class RowPayload(Payload):        # Snowflake, Postgres
        kind: Literal["row"] = "row"
        table, primary_key: dict[str, Any], columns: dict[str, Any]
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Payload(BaseModel):
    """Base for all payloads. ``kind`` names the entity kind and is copied onto the Envelope."""

    model_config = ConfigDict(frozen=True)

    kind: str


class MessagePayload(Payload):
    """A chat message (Slack today; Teams, Discord, ... later)."""

    kind: Literal["message"] = "message"
    author_id: str | None = None
    author_name: str | None = None
    author_type: Literal["user", "bot", "system"]
    text: str = ""
    # The conversation thread this message belongs to, if any (Slack ``thread_ts``).
    thread_id: str | None = None
    # The message this one replies to; ``None`` for top-level messages and thread roots.
    parent_id: str | None = None


class EmailAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str | None = None
    address: str


class EmailPayload(Payload):
    """An email message (Gmail today; Outlook, IMAP, ... later)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    kind: Literal["email"] = "email"
    subject: str | None = None
    # Stored as "from"; the attribute needs a trailing underscore in Python.
    from_: EmailAddress | None = Field(default=None, alias="from")
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    bcc: list[EmailAddress] = Field(default_factory=list)
    reply_to: list[EmailAddress] = Field(default_factory=list)
    # RFC 5322 Message-ID / In-Reply-To / References, for threading across mailboxes.
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    # The source's own conversation id (Gmail threadId).
    thread_id: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    snippet: str | None = None
    body_text: str | None = None
    has_attachments: bool = False
    # Names only; attachment content is out of scope.
    attachment_names: list[str] = Field(default_factory=list)


class IssuePayload(Payload):
    """A tracked work item (Jira today; GitHub Issues, Linear, ... later)."""

    kind: Literal["issue"] = "issue"
    # The human-readable key (Jira ``PROJ-123``). It changes when an issue moves project,
    # which is why the Envelope key uses the stable ``source_id`` instead.
    issue_key: str
    project_key: str | None = None
    summary: str = ""
    # Plain text; the source's rich format (Jira ADF) stays in ``raw``.
    description: str = ""
    issue_type: str | None = None
    status: str | None = None
    # The workflow-independent bucket: ``new``, ``indeterminate`` or ``done`` in Jira.
    status_category: str | None = None
    priority: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    due_date: date | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    assignee_id: str | None = None
    assignee_name: str | None = None
    reporter_id: str | None = None
    reporter_name: str | None = None
    # The parent issue (epic, or the story of a sub-task), if any.
    parent_id: str | None = None
    parent_key: str | None = None
    url: str | None = None
    comment_count: int = 0


class CommentVisibility(BaseModel):
    """A restriction on who may see a comment (Jira: a project role or a group)."""

    model_config = ConfigDict(frozen=True)

    type: str
    value: str


class CommentPayload(Payload):
    """A comment on an item such as an issue (Jira today)."""

    kind: Literal["comment"] = "comment"
    author_id: str | None = None
    author_name: str | None = None
    author_type: Literal["user", "bot", "system"] = "user"
    text: str = ""
    # The item commented on: its ``source_id`` and human-readable key.
    parent_id: str
    parent_key: str | None = None
    # ``None`` when everyone who can see the parent can see the comment.
    visibility: CommentVisibility | None = None


class AttachmentPayload(Payload):
    """A file attached to a message (Gmail attachments today; Drive or SharePoint files
    could share this kind later). Stored as its own envelope next to the message."""

    kind: Literal["attachment"] = "attachment"
    # The parent message, in the source's terms and as stored (its envelope _key).
    message_id: str
    message_key: str
    thread_id: str | None = None
    # The MIME part the file came from; stable across fetches, unlike Gmail's attachmentId.
    part_id: str
    filename: str
    mime_type: str
    size: int = 0
    # True for inline parts (e.g. images embedded in an HTML body via Content-ID).
    inline: bool = False
    content_id: str | None = None
    sha256: str | None = None
    content_base64: str | None = None
    text: str | None = None
    text_truncated: bool = False
    # Why content or text is missing although requested: "too_large", "download_failed",
    # or a text reason such as "unsupported_type", "no_text", "encrypted".
    skipped_reason: str | None = None
