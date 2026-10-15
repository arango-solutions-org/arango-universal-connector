"""Jira settings and registry wiring. No network."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from arango_connector.config import ConfigError, load_settings
from arango_connector.sources.jira.config import JiraSettings
from arango_connector_standalone.config import CoreSettings
from arango_connector_standalone.registry import build_source
from arango_connector_standalone.sources.jira.adapter import JiraPollingAdapter
from arango_connector_standalone.state import InMemoryStateStore

JIRA_VARS = (
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_PROJECTS",
    "JIRA_POLL_INTERVAL_SECONDS",
    "JIRA_INITIAL_SYNC",
    "JIRA_INITIAL_LOOKBACK",
    "JIRA_PAGE_SIZE",
    "SOURCE",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in JIRA_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARANGO_PASSWORD", "pw")


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-token")
    monkeypatch.setenv("JIRA_PROJECTS", "proj, OPS ,PROJ")


@pytest.mark.usefixtures("required")
def test_defaults() -> None:
    s = load_settings(JiraSettings)

    assert s.jira_base_url == "https://acme.atlassian.net"
    assert s.jira_site == "acme.atlassian.net"
    assert s.jira_email == "bot@example.com"
    assert s.jira_api_token.get_secret_value() == "secret-token"
    assert s.jira_projects == ["PROJ", "OPS"]  # upper-cased, deduped
    assert s.jira_poll_interval_seconds == 60
    assert s.jira_initial_sync == "lookback"
    assert s.jira_initial_lookback == timedelta(days=30)
    assert s.jira_page_size == 100


def test_required_variables_are_named() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(JiraSettings)
    message = str(info.value)
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECTS"):
        assert f"{var}: is required" in message


@pytest.mark.usefixtures("required")
def test_token_is_never_in_the_repr() -> None:
    assert "secret-token" not in repr(load_settings(JiraSettings))


@pytest.mark.usefixtures("required")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Acme.Atlassian.net/", "https://acme.atlassian.net"),
        ("https://acme.atlassian.net/jira/software/projects", "https://acme.atlassian.net"),
        ("  https://jira.example.com:8443  ", "https://jira.example.com:8443"),
    ],
)
def test_base_url_is_normalised(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", raw)
    assert load_settings(JiraSettings).jira_base_url == expected


@pytest.mark.usefixtures("required")
@pytest.mark.parametrize("raw", ["http://acme.atlassian.net", "acme.atlassian.net", "https://"])
def test_base_url_must_be_https(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", raw)
    with pytest.raises(ConfigError, match="JIRA_BASE_URL: must be an https URL"):
        load_settings(JiraSettings)


@pytest.mark.usefixtures("required")
def test_invalid_project_key_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_PROJECTS", "PROJ,not a key")
    with pytest.raises(ConfigError, match="JIRA_PROJECTS: not a Jira project key"):
        load_settings(JiraSettings)


@pytest.mark.usefixtures("required")
def test_invalid_email_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "bot")
    with pytest.raises(ConfigError, match="JIRA_EMAIL: not an email address"):
        load_settings(JiraSettings)


@pytest.mark.usefixtures("required")
def test_lookback_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_INITIAL_LOOKBACK", "12h")
    assert load_settings(JiraSettings).jira_initial_lookback == timedelta(hours=12)
    monkeypatch.setenv("JIRA_INITIAL_LOOKBACK", "0d")
    with pytest.raises(ConfigError, match="JIRA_INITIAL_LOOKBACK: must be positive"):
        load_settings(JiraSettings)


@pytest.mark.usefixtures("required")
def test_page_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_PAGE_SIZE", "500")
    with pytest.raises(ConfigError, match="JIRA_PAGE_SIZE"):
        load_settings(JiraSettings)


@pytest.mark.usefixtures("required")
def test_registry_builds_the_jira_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE", "jira")
    adapter = build_source(load_settings(CoreSettings), InMemoryStateStore())
    assert isinstance(adapter, JiraPollingAdapter)
    assert adapter.name == "jira"
    assert adapter.interval_seconds == 60


def test_registry_reports_missing_jira_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE", "jira")
    with pytest.raises(ConfigError, match="JIRA_PROJECTS"):
        build_source(load_settings(CoreSettings), InMemoryStateStore())
