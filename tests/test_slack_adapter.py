"""Offline tests for the Slack adapter's event handling (no network, no real tokens)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest
from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.errors import SlackApiError

from arango_connector.envelope import Envelope
from arango_connector.sources.slack import adapter as adapter_module
from arango_connector.sources.slack.adapter import SlackSocketModeAdapter

LoadEvent = Callable[[str], dict[str, Any]]
WATCHED = "C0TESTCHAN1"


@pytest.fixture
def slack_adapter(monkeypatch: pytest.MonkeyPatch) -> SlackSocketModeAdapter:
    # Authorize locally so Bolt never calls auth.test over the network.
    def authorize(**_: Any) -> AuthorizeResult:
        return AuthorizeResult(
            enterprise_id=None,
            team_id="T0TESTTEAM",
            bot_token="xoxb-fake",
            bot_id="B0SELFBOT",
            bot_user_id="U0SELFBOT",
        )

    monkeypatch.setattr(adapter_module, "App", lambda token: App(authorize=authorize))
    adapter = SlackSocketModeAdapter("xoxb-fake", "xapp-fake", {WATCHED})
    monkeypatch.setattr(adapter.app.client, "users_info", FakeUsersInfo())
    return adapter


class FakeUsersInfo:
    """Stands in for ``WebClient.users_info``, recording the users looked up."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def __call__(self, *, user: str) -> dict[str, Any]:
        self.calls.append(user)
        if self.error:
            raise SlackApiError(self.error, {"ok": False, "error": self.error})
        return {"ok": True, "user": {"id": user, "real_name": f"Real {user}"}}


def _event_callback(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T0TESTTEAM",
        "api_app_id": "A0TESTAPP",
        "event": event,
        "event_id": "Ev0TEST",
        "event_time": 1716500000,
    }


def test_watched_channel_is_emitted(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("user_message")

    slack_adapter._on_message(event, _event_callback(event))

    assert [e.key for e in emitted] == ["slack_C0TESTCHAN1_1716500000.123456"]
    assert emitted[0].workspace_id == "T0TESTTEAM"


def test_user_message_gets_real_name(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("user_message")

    slack_adapter._on_message(event, _event_callback(event))

    assert emitted[0].author_name == "Real U0TESTUSER1"


def test_user_name_lookup_is_cached(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("user_message")

    slack_adapter._on_message(event, _event_callback(event))
    slack_adapter._on_message(event, _event_callback(event))

    assert [e.author_name for e in emitted] == ["Real U0TESTUSER1"] * 2
    assert slack_adapter.app.client.users_info.calls == ["U0TESTUSER1"]


def test_failed_name_lookup_still_emits_and_retries(
    slack_adapter: SlackSocketModeAdapter,
    load_event: LoadEvent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = FakeUsersInfo(error="missing_scope")
    monkeypatch.setattr(slack_adapter.app.client, "users_info", lookup)
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("user_message")

    slack_adapter._on_message(event, _event_callback(event))
    slack_adapter._on_message(event, _event_callback(event))

    assert [e.author_name for e in emitted] == [None, None]
    assert lookup.calls == ["U0TESTUSER1", "U0TESTUSER1"]


def test_bot_message_skips_name_lookup(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("bot_message")

    slack_adapter._on_message(event, _event_callback(event))

    assert emitted[0].author_name == "deploy-bot"
    assert slack_adapter.app.client.users_info.calls == []


def test_unwatched_channel_is_ignored(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("user_message") | {"channel": "C0OTHERCHAN"}

    slack_adapter._on_message(event, _event_callback(event))

    assert emitted == []


def test_skipped_subtype_is_not_emitted(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("channel_join")

    slack_adapter._on_message(event, _event_callback(event))

    assert emitted == []


def test_edit_is_emitted_against_the_original_key(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    emitted: list[Envelope] = []
    slack_adapter._emit = emitted.append
    event = load_event("message_changed")

    slack_adapter._on_message(event, _event_callback(event))

    assert [e.key for e in emitted] == ["slack_C0TESTCHAN1_1716500000.123456"]
    assert emitted[0].text == "Hello from the test channel (edited)"


def test_emit_failure_is_swallowed(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    def boom(_: Envelope) -> None:
        raise RuntimeError("downstream exploded")

    slack_adapter._emit = boom
    event = load_event("user_message")

    slack_adapter._on_message(event, _event_callback(event))  # must not raise


def test_bolt_dispatch_reaches_handler(
    slack_adapter: SlackSocketModeAdapter, load_event: LoadEvent
) -> None:
    """Bolt injects ``event``/``body`` by argument name into the bound listener."""
    received = threading.Event()
    emitted: list[Envelope] = []

    def emit(env: Envelope) -> None:
        emitted.append(env)
        received.set()

    slack_adapter._emit = emit
    request = BoltRequest(body=_event_callback(load_event("thread_reply")), mode="socket_mode")

    response = slack_adapter.app.dispatch(request)

    assert response.status == 200
    assert received.wait(timeout=5)
    assert emitted[0].thread_parent == "1716500000.123456"


def test_stop_is_idempotent_before_start(slack_adapter: SlackSocketModeAdapter) -> None:
    slack_adapter.stop()
    slack_adapter.stop()
