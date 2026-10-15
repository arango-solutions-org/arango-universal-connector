"""Runtime configuration, read from the environment and an optional ``.env`` file.

``CoreSettings`` holds what every deployment needs. Each source (and sink) keeps its
own settings class next to its code and loads it in its registry factory, so a
deployment only needs the variables of the source it actually runs.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


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
