"""Settings for the Gmail source. Loaded only when ``SOURCE=gmail``."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, split_csv

AuthMode = Literal["service_account", "oauth"]
InitialSync = Literal["now", "lookback"]
KeyMode = Literal["hashed", "plain"]
RawMode = Literal["full", "stripped", "none"]
LabelMatch = Literal["thread", "message"]

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: Any) -> Any:
    """Accept ``"90m"``, ``"24h"``, ``"7d"``, ``"3600s"`` or a number of seconds."""
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            amount, unit = match.groups()
            return timedelta(**{_DURATION_UNITS[unit.lower()]: int(amount)})
        if value.strip().isdigit():
            return timedelta(seconds=int(value))
    return value


class GmailSettings(ConnectorSettings):
    gmail_auth_mode: AuthMode = "service_account"
    # Service account JSON key with domain-wide delegation (service_account mode).
    gmail_service_account_file: Path | None = None
    # Directory of per-mailbox token files from arango-connector-gmail-auth (oauth mode).
    gmail_oauth_token_dir: Path | None = None

    gmail_mailboxes: Annotated[list[str], NoDecode, Field(min_length=1)]
    # Ingest a message if it carries any of these (label IDs or names) ...
    gmail_labels: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["INBOX", "SENT"], min_length=1
    )
    # ... and none of these.
    gmail_exclude_labels: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SPAM", "TRASH", "DRAFT"]
    )

    # thread: a message qualifies if any message in its thread carries a watched label
    # (as Gmail's conversation view shows it). message: only its own labels count.
    gmail_label_match: LabelMatch = "thread"

    gmail_poll_interval_seconds: float = Field(default=60, gt=0)
    gmail_initial_sync: InitialSync = "now"
    gmail_initial_lookback: timedelta = timedelta(hours=24)

    gmail_key_mode: KeyMode = "hashed"
    gmail_store_body: bool = True
    gmail_store_bcc: bool = True
    gmail_raw_mode: RawMode = "stripped"

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
