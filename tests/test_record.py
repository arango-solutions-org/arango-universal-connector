"""Tests for the generic Record and key building."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from arango_connector.payloads import MessagePayload, Payload
from arango_connector.record import SCHEMA_VERSION, Record, Scope, make_key


def _record(created_at: datetime | None = None) -> Record[MessagePayload]:
    return Record[MessagePayload](
        key="slack_C1_1.2",
        source="slack",
        source_id="1.2",
        scope=Scope(tenant_id="T1", container_id="C1"),
        created_at=created_at,
        payload=MessagePayload(author_type="user", text="hi"),
        raw={"text": "hi"},
    )


def test_make_key_joins_parts() -> None:
    assert make_key("slack", "C1", "1716500000.123456") == "slack_C1_1716500000.123456"
    assert make_key("gmail", "a@example.com", "18c2") == "gmail_a@example.com_18c2"


@pytest.mark.parametrize("bad", ["has space", "slash/inside", "", "x" * 300])
def test_make_key_rejects_invalid_arango_keys(bad: str) -> None:
    with pytest.raises(ValueError):
        make_key("src", bad)


def test_kind_comes_from_payload_and_is_serialized() -> None:
    doc = _record().to_document()
    assert doc["kind"] == "message"
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["payload"] == {
        "kind": "message",
        "author_id": None,
        "author_name": None,
        "author_type": "user",
        "text": "hi",
        "thread_id": None,
        "parent_id": None,
    }
    json.dumps(doc)


def test_subclass_payload_fields_survive_base_typed_record() -> None:
    """A Record typed with the base Payload must still write the subclass's fields."""
    typed = _record()
    loose = Record[Payload](**{**typed.model_dump(by_alias=True), "payload": typed.payload})
    assert loose.to_document()["payload"]["text"] == "hi"


def test_retention_base_prefers_created_at() -> None:
    created = datetime(2024, 1, 1, tzinfo=UTC)
    assert _record(created).retention_base == created
    undated = _record(None)
    assert undated.retention_base == undated.ingested_at
