"""Unit tests for the pure Slack -> Envelope mapping."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from arango_connector.sources.slack.normalize import normalize, slack_ts_to_datetime

TEAM_ID = "T0TESTTEAM"
LoadEvent = Callable[[str], dict[str, Any]]

# ArangoDB _key allowed characters.
ARANGO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,254}$")


def test_user_message_field_mapping(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    env = normalize(event, TEAM_ID)

    assert env is not None
    assert env.key == "slack_C0TESTCHAN1_1716500000.123456"
    assert env.schema_version == 1
    assert env.source == "slack"
    assert env.workspace_id == TEAM_ID
    assert env.channel_id == "C0TESTCHAN1"
    assert env.author_id == "U0TESTUSER1"
    assert env.author_type == "user"
    assert env.text == "Hello from the test channel :wave:"
    assert env.source_ts == "1716500000.123456"
    assert env.created_at == datetime(2024, 5, 23, 21, 33, 20, 123456, tzinfo=UTC)
    assert env.thread_parent is None
    assert env.ingested_at.tzinfo is not None


def test_key_format_is_arango_safe(load_event: LoadEvent) -> None:
    for name in ("user_message", "thread_reply", "bot_message"):
        env = normalize(load_event(name), TEAM_ID)
        assert env is not None
        assert env.key == f"slack_{env.channel_id}_{env.source_ts}"
        assert ARANGO_KEY_RE.match(env.key), env.key


def test_thread_reply_sets_thread_parent(load_event: LoadEvent) -> None:
    env = normalize(load_event("thread_reply"), TEAM_ID)
    assert env is not None
    assert env.thread_parent == "1716500000.123456"
    assert env.author_id == "U0TESTUSER2"


def test_thread_parent_is_none_when_thread_ts_equals_ts(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    event["thread_ts"] = event["ts"]  # the thread's root message
    env = normalize(event, TEAM_ID)
    assert env is not None
    assert env.thread_parent is None


def test_thread_broadcast_is_ingested(load_event: LoadEvent) -> None:
    event = load_event("thread_reply")
    event["subtype"] = "thread_broadcast"
    env = normalize(event, TEAM_ID)
    assert env is not None
    assert env.author_type == "user"
    assert env.thread_parent == "1716500000.123456"


def test_bot_message(load_event: LoadEvent) -> None:
    env = normalize(load_event("bot_message"), TEAM_ID)
    assert env is not None
    assert env.author_type == "bot"
    assert env.author_id == "B0TESTBOT1"
    assert env.text == "Deploy finished successfully"


def test_app_bot_without_subtype_is_bot(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    event["bot_id"] = "B0TESTBOT2"
    env = normalize(event, TEAM_ID)
    assert env is not None
    assert env.author_type == "bot"
    assert env.author_id == "B0TESTBOT2"


@pytest.mark.parametrize("name", ["message_changed", "channel_join"])
def test_skipped_subtypes_return_none(load_event: LoadEvent, name: str) -> None:
    assert normalize(load_event(name), TEAM_ID) is None


@pytest.mark.parametrize("subtype", ["message_deleted", "channel_leave", "channel_topic"])
def test_other_subtypes_return_none(load_event: LoadEvent, subtype: str) -> None:
    event = load_event("user_message")
    event["subtype"] = subtype
    assert normalize(event, TEAM_ID) is None


def test_non_message_type_returns_none(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    event["type"] = "reaction_added"
    assert normalize(event, TEAM_ID) is None


@pytest.mark.parametrize("field", ["ts", "channel"])
def test_missing_required_fields_return_none(load_event: LoadEvent, field: str) -> None:
    event = load_event("user_message")
    del event[field]
    assert normalize(event, TEAM_ID) is None


def test_missing_text_becomes_empty_string(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    del event["text"]
    env = normalize(event, TEAM_ID)
    assert env is not None
    assert env.text == ""


def test_raw_equals_input_and_input_not_mutated(load_event: LoadEvent) -> None:
    event = load_event("user_message")
    original = copy.deepcopy(event)
    env = normalize(event, TEAM_ID)
    assert env is not None
    assert env.raw == original
    assert event == original


def test_to_document_is_json_safe_with_key_alias(load_event: LoadEvent) -> None:
    env = normalize(load_event("thread_reply"), TEAM_ID)
    assert env is not None
    doc = env.to_document()

    assert doc["_key"] == env.key
    assert "key" not in doc
    assert doc["created_at"] == "2024-05-23T21:35:00.000200Z"
    assert isinstance(doc["ingested_at"], str)
    datetime.fromisoformat(doc["ingested_at"])
    json.dumps(doc)  # must not raise


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("1716500000.123456", datetime(2024, 5, 23, 21, 33, 20, 123456, tzinfo=UTC)),
        ("1716500000.000001", datetime(2024, 5, 23, 21, 33, 20, 1, tzinfo=UTC)),
        ("1716500000", datetime(2024, 5, 23, 21, 33, 20, tzinfo=UTC)),
    ],
)
def test_slack_ts_to_datetime(ts: str, expected: datetime) -> None:
    assert slack_ts_to_datetime(ts) == expected
