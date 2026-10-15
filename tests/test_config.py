"""Config and registry tests: core settings no longer depend on any one source."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from arango_connector.config import ConfigError, load_settings
from arango_connector_standalone.config import CoreSettings
from arango_connector_standalone.registry import build_source
from arango_connector_standalone.retention import RetentionPolicy
from arango_connector_standalone.sources.slack.adapter import SlackSocketModeAdapter
from arango_connector_standalone.state import InMemoryStateStore

SLACK_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_IDS")


class _FakeApp:
    """Stands in for slack_bolt.App so building the adapter makes no network calls."""

    def __init__(self, token: str, **_: Any) -> None:
        self.token = token

    def event(self, _name: str) -> Callable[[Any], Any]:
        return lambda handler: handler


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run from an empty directory so a developer's .env cannot leak in.
    monkeypatch.chdir(tmp_path)
    for var in (
        *SLACK_VARS,
        "SOURCE",
        "ARANGO_HOSTS",
        "RETENTION_DAYS",
        "RETENTION_MODE",
        "RETENTION_AGE_FIELD",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARANGO_PASSWORD", "pw")


def test_core_settings_load_without_any_slack_vars() -> None:
    settings = load_settings(CoreSettings)
    assert settings.source == "slack"
    assert settings.state_collection == "connector_state"
    assert settings.arango_password.get_secret_value() == "pw"


def test_missing_core_var_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARANGO_PASSWORD")
    with pytest.raises(ConfigError, match="ARANGO_PASSWORD: is required"):
        load_settings(CoreSettings)


def test_arango_hosts_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARANGO_HOSTS", "http://a:8529, http://b:8529")
    assert load_settings(CoreSettings).arango_hosts == ["http://a:8529", "http://b:8529"]


def test_slack_vars_required_only_when_slack_is_built() -> None:
    settings = load_settings(CoreSettings)
    with pytest.raises(ConfigError) as exc_info:
        build_source(settings, InMemoryStateStore())
    message = str(exc_info.value)
    for var in SLACK_VARS:
        assert var in message
    assert "ARANGO_PASSWORD" not in message


def test_slack_source_builds_from_its_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1, C2")
    monkeypatch.setattr("arango_connector_standalone.sources.slack.adapter.App", _FakeApp)

    source = build_source(load_settings(CoreSettings), InMemoryStateStore())

    assert isinstance(source, SlackSocketModeAdapter)
    assert source.channel_ids == {"C1", "C2"}


def test_unknown_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE", "carrier-pigeon")
    with pytest.raises(ValueError, match="Unknown SOURCE='carrier-pigeon'"):
        build_source(load_settings(CoreSettings), InMemoryStateStore())


def test_retention_defaults_to_expiry_mode() -> None:
    assert load_settings(CoreSettings).retention_policy() == RetentionPolicy(30, "expiry")


def test_retention_age_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("RETENTION_MODE", "age")
    monkeypatch.setenv("RETENTION_AGE_FIELD", "ingested_at")
    assert load_settings(CoreSettings).retention_policy() == RetentionPolicy(
        7, "age", "ingested_at"
    )


def test_retention_days_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETENTION_DAYS", "36501")
    with pytest.raises(ConfigError, match="RETENTION_DAYS"):
        load_settings(CoreSettings)


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert load_settings(CoreSettings).log_level == "DEBUG"


def test_unknown_log_level_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings(CoreSettings)


def test_invalid_retention_mode_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETENTION_MODE", "forever")
    with pytest.raises(ConfigError, match="RETENTION_MODE"):
        load_settings(CoreSettings)
