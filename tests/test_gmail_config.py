"""Gmail settings, credentials and registry wiring. No network."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arango_connector.config import ConfigError, CoreSettings, load_settings
from arango_connector.registry import build_source
from arango_connector.sources.gmail.adapter import GmailPollingAdapter
from arango_connector.sources.gmail.auth import SCOPES, build_credentials
from arango_connector.sources.gmail.config import GmailSettings, parse_duration
from arango_connector.state import InMemoryStateStore

GMAIL_VARS = (
    "GMAIL_AUTH_MODE",
    "GMAIL_SERVICE_ACCOUNT_FILE",
    "GMAIL_OAUTH_TOKEN_DIR",
    "GMAIL_MAILBOXES",
    "GMAIL_LABELS",
    "GMAIL_EXCLUDE_LABELS",
    "GMAIL_INITIAL_SYNC",
    "GMAIL_INITIAL_LOOKBACK",
    "GMAIL_KEY_MODE",
    "GMAIL_RAW_MODE",
    "GMAIL_STORE_BODY",
    "GMAIL_STORE_BCC",
    "SOURCE",
)


@pytest.fixture
def sa_file(tmp_path: Path) -> Path:
    """A syntactically valid service account key (never used against Google)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "sa.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "test",
                "private_key_id": "abc",
                "private_key": pem,
                "client_email": "connector@test.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return path


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in GMAIL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARANGO_PASSWORD", "pw")


def _token_dir(tmp_path: Path, *mailboxes: str) -> Path:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    for mailbox in mailboxes:
        (token_dir / f"{mailbox}.json").write_text(
            json.dumps(
                {
                    "refresh_token": "1//refresh",
                    "client_id": "cid.apps.googleusercontent.com",
                    "client_secret": "secret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            )
        )
    return token_dir


def test_defaults(monkeypatch: pytest.MonkeyPatch, sa_file: Path) -> None:
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(sa_file))
    monkeypatch.setenv("GMAIL_MAILBOXES", " Ada@Example.com, bob@example.com ,ada@example.com")

    s = load_settings(GmailSettings)

    assert s.gmail_auth_mode == "service_account"
    assert s.gmail_mailboxes == ["ada@example.com", "bob@example.com"]  # lowered, deduped
    assert s.gmail_labels == ["INBOX", "SENT"]
    assert s.gmail_exclude_labels == ["SPAM", "TRASH", "DRAFT"]
    assert s.gmail_poll_interval_seconds == 60
    assert s.gmail_initial_sync == "now"
    assert s.gmail_initial_lookback == timedelta(hours=24)
    assert s.gmail_key_mode == "hashed"
    assert s.gmail_raw_mode == "stripped"
    assert s.gmail_store_body and s.gmail_store_bcc


def test_mailboxes_required() -> None:
    with pytest.raises(ConfigError, match="GMAIL_MAILBOXES: is required"):
        load_settings(GmailSettings)


def test_invalid_mailbox_is_named(monkeypatch: pytest.MonkeyPatch, sa_file: Path) -> None:
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(sa_file))
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com,not-an-address")
    with pytest.raises(ConfigError, match="GMAIL_MAILBOXES: not an email address"):
        load_settings(GmailSettings)


def test_service_account_file_required_and_must_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com")
    with pytest.raises(ConfigError, match="GMAIL_SERVICE_ACCOUNT_FILE is required"):
        load_settings(GmailSettings)

    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(ConfigError, match="GMAIL_SERVICE_ACCOUNT_FILE does not exist"):
        load_settings(GmailSettings)


def test_oauth_needs_a_token_per_mailbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GMAIL_AUTH_MODE", "oauth")
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com,bob@example.com")
    with pytest.raises(ConfigError, match="GMAIL_OAUTH_TOKEN_DIR is required"):
        load_settings(GmailSettings)

    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_DIR", str(_token_dir(tmp_path, "ada@example.com")))
    with pytest.raises(ConfigError, match=r"no token file .* for: bob@example.com"):
        load_settings(GmailSettings)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90m", timedelta(minutes=90)),
        ("24h", timedelta(hours=24)),
        ("7D", timedelta(days=7)),
        ("3600s", timedelta(seconds=3600)),
        ("120", timedelta(seconds=120)),
    ],
)
def test_parse_duration(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


def test_lookback_must_be_positive(monkeypatch: pytest.MonkeyPatch, sa_file: Path) -> None:
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(sa_file))
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com")
    monkeypatch.setenv("GMAIL_INITIAL_LOOKBACK", "0h")
    with pytest.raises(ConfigError, match="GMAIL_INITIAL_LOOKBACK: must be positive"):
        load_settings(GmailSettings)


@pytest.mark.parametrize("var", ["GMAIL_KEY_MODE", "GMAIL_RAW_MODE", "GMAIL_AUTH_MODE"])
def test_invalid_choices_are_named(
    monkeypatch: pytest.MonkeyPatch, sa_file: Path, var: str
) -> None:
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(sa_file))
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com")
    monkeypatch.setenv(var, "bogus")
    with pytest.raises(ConfigError, match=var):
        load_settings(GmailSettings)


# -- credentials -------------------------------------------------------------------


def test_service_account_credentials_impersonate_the_mailbox(sa_file: Path) -> None:
    creds = build_credentials("service_account", "ada@example.com", service_account_file=sa_file)
    assert creds._subject == "ada@example.com"
    assert creds._scopes == SCOPES


def test_oauth_credentials_load_the_mailbox_token(tmp_path: Path) -> None:
    token_dir = _token_dir(tmp_path, "ada@example.com")
    creds = build_credentials("oauth", "Ada@Example.com", token_dir=token_dir)
    assert creds.refresh_token == "1//refresh"
    assert creds.scopes == SCOPES


def test_only_the_readonly_scope_is_requested() -> None:
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


# -- registry ----------------------------------------------------------------------


def test_gmail_source_builds_without_slack_vars_or_network(
    monkeypatch: pytest.MonkeyPatch, sa_file: Path
) -> None:
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_IDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SOURCE", "gmail")
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", str(sa_file))
    monkeypatch.setenv("GMAIL_MAILBOXES", "ada@example.com")
    monkeypatch.setenv("GMAIL_POLL_INTERVAL_SECONDS", "30")

    source = build_source(load_settings(CoreSettings), InMemoryStateStore())

    assert isinstance(source, GmailPollingAdapter)
    assert source.name == "gmail"
    assert source.interval_seconds == 30


def test_gmail_config_errors_surface_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE", "gmail")
    with pytest.raises(ConfigError, match="GMAIL_MAILBOXES"):
        build_source(load_settings(CoreSettings), InMemoryStateStore())
