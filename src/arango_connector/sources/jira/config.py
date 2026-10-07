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
    # The site, e.g. https://acme.atlassian.net. Anything after the host is dropped.
    jira_base_url: str
    # The Atlassian account the API token belongs to.
    jira_email: str
    jira_api_token: SecretStr
    jira_projects: Annotated[list[str], NoDecode, Field(min_length=1)]

    jira_poll_interval_seconds: float = Field(default=60, gt=0)
    jira_initial_sync: InitialSync = "lookback"
    jira_initial_lookback: timedelta = timedelta(days=30)
    # Issues per search page. Jira caps it at 100 when fields are requested.
    jira_page_size: int = Field(default=100, ge=1, le=100)

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
