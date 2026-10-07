"""Settings for the Gmail source. Loaded only when ``SOURCE=gmail``."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, parse_duration, split_csv

AuthMode = Literal["service_account", "oauth"]
InitialSync = Literal["now", "lookback"]
KeyMode = Literal["hashed", "plain"]
RawMode = Literal["full", "stripped", "none"]
LabelMatch = Literal["thread", "message"]
AttachmentMode = Literal["off", "metadata", "content", "text", "all"]


class GmailSettings(ConnectorSettings):
    gmail_auth_mode: AuthMode = Field(
        default="service_account",
        description="`service_account` (Workspace, domain-wide delegation) or `oauth` "
        "(per-mailbox tokens).",
    )
    gmail_service_account_file: Path | None = Field(
        default=None,
        description="Service account JSON key with domain-wide delegation. Required for "
        "`service_account`.",
    )
    gmail_oauth_token_dir: Path | None = Field(
        default=None,
        description="Directory of `<mailbox>.json` token files from "
        "`arango-connector-gmail-auth`. Required for `oauth`.",
    )

    gmail_mailboxes: Annotated[list[str], NoDecode] = Field(
        min_length=1, description="Mailboxes to read, comma-separated."
    )
    gmail_labels: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["INBOX", "SENT"],
        min_length=1,
        description="Ingest mail carrying **any** of these labels (IDs or names, resolved "
        "per mailbox). The default captures both sides of conversations.",
    )
    gmail_exclude_labels: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SPAM", "TRASH", "DRAFT"],
        description="... and **none** of these. Always checked on the message itself.",
    )
    gmail_label_match: LabelMatch = Field(
        default="thread",
        description="`thread`: a message qualifies if any message in its thread carries a "
        "watched label, as Gmail's conversation view shows it, so replies come along. "
        "`message`: only its own labels count.",
    )

    gmail_poll_interval_seconds: float = Field(
        default=60, gt=0, description="Seconds between polls."
    )
    gmail_initial_sync: InitialSync = Field(
        default="now",
        description="First start for a mailbox: `now` takes only new mail; `lookback` also "
        "takes mail from the last `GMAIL_INITIAL_LOOKBACK`.",
    )
    gmail_initial_lookback: timedelta = Field(
        default=timedelta(hours=24),
        description="For `lookback`: e.g. `90m`, `24h`, `7d`. Bounded; not a full backfill.",
    )

    gmail_key_mode: KeyMode = Field(
        default="hashed",
        description="`hashed`: keys, `scope.container_id`, cursors and logs use a "
        "16-character SHA-256 of the address. `plain`: the address itself.",
    )
    gmail_store_body: bool = Field(
        default=True,
        description="`false` stores headers only: no `body_text`, no `snippet`, no body "
        "data in `raw`, and attachments limited to `off` or `metadata`.",
    )
    gmail_store_bcc: bool = Field(
        default=True, description="`false` drops Bcc from the payload and from `raw` headers."
    )
    gmail_raw_mode: RawMode = Field(
        default="stripped",
        description="`full` (the API message as-is), `stripped` (no base64 body data) or `none`.",
    )

    gmail_attachments: AttachmentMode = Field(
        default="off",
        description="`off`; `metadata` (filename, type, size; no downloads); `content` "
        "(+ file as base64 and sha256); `text` (+ extracted text, no file); `all`.",
    )
    gmail_attachment_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        description="Larger attachments are stored as metadata only (`skipped_reason: "
        '"too_large"`).',
    )
    gmail_attachment_text_max_chars: int = Field(
        default=1_000_000,
        ge=0,
        description="Extracted text beyond this is cut off (`text_truncated`). `0` means no limit.",
    )

    @field_validator("gmail_mailboxes", mode="before")
    @classmethod
    def _parse_mailboxes(cls, value: Any) -> Any:
        value = split_csv(value)
        if isinstance(value, list):
            # Lower-cased and de-duplicated, so keys and cursors are stable.
            return list(dict.fromkeys(str(v).lower() for v in value))
        return value

    @field_validator("gmail_mailboxes")
    @classmethod
    def _check_mailboxes(cls, value: list[str]) -> list[str]:
        for mailbox in value:
            local, _, domain = mailbox.partition("@")
            if not local or "." not in domain:
                raise ValueError(f"not an email address: {mailbox!r}")
        return value

    @field_validator("gmail_labels", "gmail_exclude_labels", mode="before")
    @classmethod
    def _parse_labels(cls, value: Any) -> Any:
        return split_csv(value)

    @field_validator("gmail_initial_lookback", mode="before")
    @classmethod
    def _parse_lookback(cls, value: Any) -> Any:
        return parse_duration(value)

    @field_validator("gmail_initial_lookback")
    @classmethod
    def _check_lookback(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("must be positive, e.g. 24h or 7d")
        return value

    @model_validator(mode="after")
    def _check_redaction(self) -> GmailSettings:
        if not self.gmail_store_body and self.gmail_attachments in ("content", "text", "all"):
            raise ValueError(
                f"GMAIL_ATTACHMENTS={self.gmail_attachments} would store message content "
                "although GMAIL_STORE_BODY=false; use off or metadata"
            )
        return self

    @model_validator(mode="after")
    def _check_auth(self) -> GmailSettings:
        if self.gmail_auth_mode == "service_account":
            path = self.gmail_service_account_file
            if path is None:
                raise ValueError("GMAIL_SERVICE_ACCOUNT_FILE is required for service_account")
            if not path.is_file():
                raise ValueError(f"GMAIL_SERVICE_ACCOUNT_FILE does not exist: {path}")
        else:
            token_dir = self.gmail_oauth_token_dir
            if token_dir is None:
                raise ValueError("GMAIL_OAUTH_TOKEN_DIR is required for oauth")
            missing = [m for m in self.gmail_mailboxes if not (token_dir / f"{m}.json").is_file()]
            if missing:
                raise ValueError(
                    f"no token file in GMAIL_OAUTH_TOKEN_DIR for: {', '.join(missing)} "
                    "(create them with arango-connector-gmail-auth)"
                )
        return self
