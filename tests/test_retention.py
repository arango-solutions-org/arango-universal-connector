"""Unit tests for RetentionPolicy: TTL index choice, stamping and dead-letter expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arango_connector.envelope import Envelope, Scope
from arango_connector.payloads import MessagePayload
from arango_connector.retention import RetentionPolicy

POSTED = datetime(2024, 5, 1, tzinfo=UTC)
INGESTED = datetime(2024, 5, 3, tzinfo=UTC)


def _envelope(created_at: datetime | None = POSTED) -> Envelope[MessagePayload]:
    return Envelope[MessagePayload](
        key="k",
        source="s",
        source_id="1",
        scope=Scope(container_id="c"),
        created_at=created_at,
        ingested_at=INGESTED,
        payload=MessagePayload(author_type="user"),
        raw={},
    )


def test_ttl_index_per_mode() -> None:
    assert RetentionPolicy(0).ttl_index() is None
    assert RetentionPolicy(0, mode="age").ttl_index() is None
    assert RetentionPolicy(30).ttl_index() == ("expires_at", 0)
    assert RetentionPolicy(30, mode="age").ttl_index() == ("created_at", 30 * 86400)
    assert RetentionPolicy(7, mode="age", age_field="ingested_at").ttl_index() == (
        "ingested_at",
        7 * 86400,
    )


def test_negative_days_rejected() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(-1)


def test_expiry_mode_stamps_from_created_at() -> None:
    stamped = RetentionPolicy(30).stamp(_envelope())
    assert stamped.expires_at == POSTED + timedelta(days=30)


def test_expiry_mode_falls_back_to_ingested_at() -> None:
    stamped = RetentionPolicy(7).stamp(_envelope(created_at=None))
    assert stamped.expires_at == INGESTED + timedelta(days=7)


@pytest.mark.parametrize("policy", [RetentionPolicy(0), RetentionPolicy(30, mode="age")])
def test_no_stamp_when_disabled_or_age_mode(policy: RetentionPolicy) -> None:
    already = _envelope().model_copy(update={"expires_at": POSTED})
    assert policy.stamp(already).expires_at is None


def test_expiry_of_prefers_stored_deadline() -> None:
    doc = {"created_at": "2024-05-01T00:00:00Z", "expires_at": "2025-01-01T00:00:00Z"}
    assert RetentionPolicy(30).expiry_of(doc) == datetime(2025, 1, 1, tzinfo=UTC)


def test_expiry_of_derives_deadline_for_docs_without_one() -> None:
    doc = {"created_at": None, "ingested_at": "2024-05-03T00:00:00"}  # naive -> UTC
    assert RetentionPolicy(30).expiry_of(doc) == INGESTED + timedelta(days=30)


def test_expiry_of_in_age_mode_ignores_expires_at() -> None:
    doc = {"created_at": "2024-05-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z"}
    assert RetentionPolicy(30, mode="age").expiry_of(doc) == POSTED + timedelta(days=30)


def test_expiry_of_missing_age_field_never_expires() -> None:
    assert RetentionPolicy(30, mode="age").expiry_of({"ingested_at": "2024-05-03"}) is None


def test_expiry_of_disabled_is_none() -> None:
    assert RetentionPolicy(0).expiry_of({"expires_at": "2000-01-01T00:00:00Z"}) is None


def test_expiry_of_unparseable_date_raises() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(30).expiry_of({"expires_at": "not a date"})
