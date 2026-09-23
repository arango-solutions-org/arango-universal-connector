"""Runtime configuration, read from the environment and an optional ``.env`` file."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """All connector settings. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    source: str = "slack"
    sink: str = "arango"

    # TODO(future): source-specific settings should move into per-adapter settings
    # classes once there is more than one source.
    slack_bot_token: SecretStr
    slack_app_token: SecretStr
    slack_channel_ids: Annotated[set[str], NoDecode, Field(min_length=1)]

    arango_hosts: Annotated[list[str], NoDecode] = ["http://localhost:8529"]
    arango_username: str = "root"
    arango_password: SecretStr
    arango_db: str = "connector"
    arango_collection: str = "messages"

    dead_letter_path: Path = Path("./dead_letter.jsonl")
    log_level: str = "INFO"

    @field_validator("slack_channel_ids", "arango_hosts", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_settings(**overrides: Any) -> Settings:
    """Load settings, turning validation errors into a readable ``ConfigError``.

    Error messages name the offending environment variables but never include values.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            var = str(err["loc"][0]).upper() if err["loc"] else "?"
            reason = "is required but not set" if err["type"] == "missing" else err["msg"]
            problems.append(f"  - {var}: {reason}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(problems)) from None
