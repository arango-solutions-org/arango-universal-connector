"""Settings for the Jira source. Loaded only when ``SOURCE=jira``."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode

from arango_connector.config import ConnectorSettings, parse_duration, split_csv

InitialSync = Literal["now", "lookback", "all"]

# Jira project keys: an uppercase letter, then uppercase letters, digits or underscores.
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


class JiraSettings(ConnectorSettings):
    jira_base_url: str = Field(
        description="The Jira Cloud site, e.g. `https://your-site.atlassian.net`. Must be "
        "https; any path is dropped.",
    )
    jira_email: str = Field(description="Email of the Atlassian account the API token belongs to.")
    jira_api_token: SecretStr = Field(
        description="API token for that account, from id.atlassian.com. Never logged."
    )
    jira_projects: Annotated[list[str], NoDecode] = Field(
        min_length=1,
        description="Project keys to sync, comma-separated, e.g. `PROJ,OPS` (case-insensitive).",
    )

    jira_poll_interval_seconds: float = Field(
        default=60,
        gt=0,
        description="Seconds between polls. A change in Jira is picked up by the next poll.",
    )
    jira_initial_sync: InitialSync = Field(
        default="lookback",
        description="First start for a project: `now` takes only later changes; `lookback` "
        "also takes issues updated in the last `JIRA_INITIAL_LOOKBACK`; `all` takes every "
        "issue in the project.",
    )
    jira_initial_lookback: timedelta = Field(
        default=timedelta(days=30),
        description="For `lookback`: e.g. `12h`, `30d`, `365d`.",
    )
    jira_page_size: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Issues per search request, at most 100.",
    )

    @field_validator("jira_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("must be an https URL, e.g. https://your-site.atlassian.net")
        host = parts.hostname.lower()
        return f"https://{host}" + (f":{parts.port}" if parts.port else "")

    @field_validator("jira_email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        local, _, domain = value.strip().partition("@")
        if not local or "." not in domain:
            raise ValueError(f"not an email address: {value!r}")
        return value.strip()

    @field_validator("jira_projects", mode="before")
    @classmethod
    def _parse_projects(cls, value: Any) -> Any:
        value = split_csv(value)
        if isinstance(value, list):
            # Upper-cased and de-duplicated, so cursors are stable.
            return list(dict.fromkeys(str(v).upper() for v in value))
        return value

    @field_validator("jira_projects")
    @classmethod
    def _check_projects(cls, value: list[str]) -> list[str]:
        for key in value:
            if not _PROJECT_KEY_RE.match(key):
                raise ValueError(f"not a Jira project key: {key!r}")
        return value

    @field_validator("jira_initial_lookback", mode="before")
    @classmethod
    def _parse_lookback(cls, value: Any) -> Any:
        return parse_duration(value)

    @field_validator("jira_initial_lookback")
    @classmethod
    def _check_lookback(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("must be positive, e.g. 30d or 12h")
        return value

    @property
    def jira_site(self) -> str:
        """The site's host name, used in keys, scopes and cursors."""
        return urlsplit(self.jira_base_url).hostname or ""
