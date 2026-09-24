"""Offline tests for the Slack adapter's event handling (no network, no real tokens)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest
from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult

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
    return SlackSocketModeAdapter("xoxb-fake", "xapp-fake", {WATCHED})


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
