"""Runtime configuration, read from the environment and an optional ``.env`` file.

``CoreSettings`` holds what every deployment needs. Each source (and sink) keeps its
own settings class next to its code and loads it in its registry factory, so a
deployment only needs the variables of the source it actually runs.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from arango_connector.retention import AgeField, RetentionMode, RetentionPolicy


def split_csv(value: Any) -> Any:
    """Split a comma-separated env value into a list. Use in ``mode="before"`` validators."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: Any) -> Any:
    """Accept ``"90m"``, ``"24h"``, ``"7d"``, ``"3600s"`` or a number of seconds.

    Use in ``mode="before"`` validators; anything else is passed through unchanged.
    """
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            amount, unit = match.groups()
            return timedelta(**{_DURATION_UNITS[unit.lower()]: int(amount)})
        if value.strip().isdigit():
            return timedelta(seconds=int(value))
    return value


class ConnectorSettings(BaseSettings):
    """Base for every settings class. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class CoreSettings(ConnectorSettings):
    """Settings shared by every deployment, whatever the source."""

    source: str = Field(
        default="slack", description="Source adapter to run (one source per process)."
    )
    sink: str = Field(default="arango", description="Sink to write to.")

    arango_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8529"],
        description="ArangoDB endpoint(s), comma-separated for multiple coordinators.",
    )
    arango_username: str = Field(default="root", description="ArangoDB user.")
    arango_password: SecretStr = Field(
        description="ArangoDB password. Also the root password of the Compose ArangoDB."
    )
    arango_db: str = Field(
        default="connector", description="Database, created on first start if missing."
    )
    arango_collection: str = Field(
        default="messages", description="Collection envelopes are written to."
    )
    state_collection: str = Field(
        default="connector_state",
        description="Collection holding source sync cursors (e.g. Gmail historyId). "
        "Never subject to retention.",
    )
    retention_days: int = Field(
        default=30,
        ge=0,
        # Larger windows push expires_at past year 9999 and every write would fail.
        le=36500,
        description="Delete envelopes this many days after they were posted, in ArangoDB "
        "and the dead-letter file. `0` disables retention; at most `36500` (100 years).",
    )
    retention_mode: RetentionMode = Field(
        default="expiry",
        description="`expiry`: each document stores its own `expires_at`, fixed at write "
        "time. `age`: documents expire `RETENTION_DAYS` after `RETENTION_AGE_FIELD`.",
    )
    retention_age_field: AgeField = Field(
        default="created_at", description="`age` mode only: the date retention counts from."
    )

    dead_letter_path: Path = Field(
        default=Path("./dead_letter.jsonl"),
        description="JSONL file for envelopes that still fail after retries "
        "(`/data/dead_letter.jsonl` in Docker).",
    )
    log_level: str = Field(default="INFO", description="Python log level.")

    @field_validator("arango_hosts", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return split_csv(value)

    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            days=self.retention_days, mode=self.retention_mode, age_field=self.retention_age_field
        )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_settings[S: BaseSettings](cls: type[S], **overrides: Any) -> S:
    """Load a settings class, turning validation errors into a readable ``ConfigError``.

    Error messages name the offending environment variables but never include values.
    """
    try:
        return cls(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            reason = "is required but not set" if err["type"] == "missing" else err["msg"]
            reason = reason.removeprefix("Value error, ")
            if err["loc"]:
                problems.append(f"  - {str(err['loc'][0]).upper()}: {reason}")
            else:  # a check spanning several variables names them itself
                problems.append(f"  - {reason}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(problems)) from None
