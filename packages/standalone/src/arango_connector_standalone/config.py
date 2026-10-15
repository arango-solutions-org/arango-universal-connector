"""Settings for the standalone service: ArangoDB, retention, dead-letter, logging.

Each source's own settings live with the source in ``arango_connector.sources``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, split_csv
from arango_connector_standalone.retention import AgeField, RetentionMode, RetentionPolicy


class CoreSettings(ConnectorSettings):
    """Settings shared by every deployment, whatever the source."""

    source: str = Field(
        default="slack", description="Source adapter to run (one source per process)."
    )
    sink: str = Field(default="arango", description="Sink to write to.")

    arango_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8529"],
        description="Arango endpoint(s), comma-separated for multiple coordinators.",
    )
    arango_username: str = Field(default="root", description="Arango user.")
    arango_password: SecretStr = Field(
        description="Arango password. Also the root password of the Compose Arango."
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
        description="Delete envelopes this many days after they were posted, in Arango "
        "and the dead-letter file. `0` disables retention.",
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
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Python log level (case-insensitive)."
    )

    @field_validator("arango_hosts", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return split_csv(value)

    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            days=self.retention_days, mode=self.retention_mode, age_field=self.retention_age_field
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value
