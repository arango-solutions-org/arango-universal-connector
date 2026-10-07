"""Runtime configuration, read from the environment and an optional ``.env`` file.

``CoreSettings`` holds what every deployment needs. Each source (and sink) keeps its
own settings class next to its code and loads it in its registry factory, so a
deployment only needs the variables of the source it actually runs.
"""

from __future__ import annotations

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

    source: str = "slack"
    sink: str = "arango"

    arango_hosts: Annotated[list[str], NoDecode] = ["http://localhost:8529"]
    arango_username: str = "root"
    arango_password: SecretStr
    arango_db: str = "connector"
    arango_collection: str = "messages"
    # Holds source cursors (e.g. Gmail historyId). Never subject to retention.
    state_collection: str = "connector_state"
    # Days to keep envelopes. 0 disables retention. See retention.py for the two modes.
    retention_days: int = Field(default=30, ge=0)
    # "expiry": each document stores its own expires_at (window fixed at write time).
    # "age": documents expire retention_days after retention_age_field.
    retention_mode: RetentionMode = "expiry"
    retention_age_field: AgeField = "created_at"

    dead_letter_path: Path = Path("./dead_letter.jsonl")
    log_level: str = "INFO"

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
