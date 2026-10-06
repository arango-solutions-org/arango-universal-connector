"""Settings for the Slack source. Loaded only when ``SOURCE=slack``."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, split_csv


class SlackSettings(ConnectorSettings):
    slack_bot_token: SecretStr
    slack_app_token: SecretStr
    slack_channel_ids: Annotated[set[str], NoDecode, Field(min_length=1)]

    @field_validator("slack_channel_ids", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return split_csv(value)
