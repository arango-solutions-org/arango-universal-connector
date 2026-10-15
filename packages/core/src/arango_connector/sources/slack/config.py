"""Settings for the Slack source.

``SlackApiSettings`` is what reading Slack's Web API needs (the Links runtime);
``SlackSettings`` adds the Socket Mode app token the standalone listener needs.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, split_csv


class SlackApiSettings(ConnectorSettings):
    slack_bot_token: SecretStr = Field(description="Bot User OAuth Token (`xoxb-…`).")
    slack_channel_ids: Annotated[set[str], NoDecode] = Field(
        min_length=1, description="Channel IDs to watch, comma-separated, e.g. `C012,C034`."
    )

    @field_validator("slack_channel_ids", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return split_csv(value)


class SlackSettings(SlackApiSettings):
    slack_app_token: SecretStr = Field(
        description="App-level token (`xapp-…`) with the `connections:write` scope."
    )
