"""Unit tests for the pure Jira -> Envelope mapping."""

from __future__ import annotations

import re
from datetime import date, timedelta

from arango_connector.sources.jira.normalize import (
    embedded_comments,
    normalize_comment,
    normalize_issue,
    parse_jira_datetime,
)
from jira_fakes import BASE_URL, CREATED, SITE, comment, issue, jira_time, user

ARANGO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,254}$")


def test_parse_jira_datetime() -> None:
    assert parse_jira_datetime("2024-05-23T21:33:20.123+0000") == CREATED
    assert parse_jira_datetime("2024-05-23T14:33:20.123-0700") == CREATED
    assert parse_jira_datetime("2024-05-23T21:33:20.123") == CREATED  # naive -> UTC
    assert parse_jira_datetime(None) is None
    assert parse_jira_datetime("") is None
    assert parse_jira_datetime("not a date") is None


def test_issue_field_mapping() -> None:
    updated = CREATED + timedelta(hours=1)
    env = normalize_issue(issue(updated=updated, comments=[comment()]), SITE, BASE_URL)

    assert env is not None
    assert env.key == "jira_acme.atlassian.net_issue_10001"
    assert env.kind == "issue"
    assert env.source == "jira"
    assert env.source_id == "10001"
    assert env.scope.tenant_id == SITE
    assert env.scope.container_id == "10000"
    assert env.scope.path == []
    assert env.created_at == CREATED
    assert env.updated_at == updated
    assert env.expires_at is None  # stamped by the pipeline, not here
    p = env.payload
    assert p.issue_key == "PROJ-1"
    assert p.project_key == "PROJ"
    assert p.summary == "Login page times out"
    assert p.description == "Steps to reproduce"
    assert p.issue_type == "Bug"
    assert p.status == "In Progress"
    assert p.status_category == "indeterminate"
    assert p.priority == "Medium"
    assert p.resolution is None and p.resolved_at is None
    assert p.due_date == date(2024, 6, 30)
    assert p.labels == ["frontend", "auth"]
    assert p.components == ["Web"]
    assert (p.assignee_id, p.assignee_name) == ("5b10ac8d82e05b22cc7d4ef5", "Grace Hopper")
    assert (p.reporter_id, p.reporter_name) == ("5b10a2844c20165700ede21g", "Ada Lovelace")
    assert p.parent_id is None and p.parent_key is None
    assert p.url == "https://acme.atlassian.net/browse/PROJ-1"
    assert p.comment_count == 1


def test_issue_raw_keeps_fields_but_not_comments() -> None:
    built = issue(comments=[comment()])
    env = normalize_issue(built, SITE, BASE_URL)
    assert env is not None
    assert "comment" not in env.raw["fields"]
    assert env.raw["fields"]["summary"] == "Login page times out"
    assert "comment" in built["fields"]  # input not mutated


def test_issue_with_nulls_and_parent() -> None:
    built = issue(
        description=None,
        assignee=None,
        priority=None,
        duedate=None,
        labels=[],
        components=[],
        resolution={"name": "Done"},
        resolutiondate=jira_time(CREATED + timedelta(days=2)),
        parent={"id": "10000", "key": "PROJ-0", "fields": {"summary": "Epic"}},
    )
    built["fields"]["description"] = None
    env = normalize_issue(built, SITE, BASE_URL)
    assert env is not None
    p = env.payload
    assert p.description == ""
    assert p.assignee_id is None and p.assignee_name is None
    assert p.priority is None and p.due_date is None
    assert p.resolution == "Done"
    assert p.resolved_at == CREATED + timedelta(days=2)
    assert (p.parent_id, p.parent_key) == ("10000", "PROJ-0")


def test_issue_without_id_or_project_is_skipped() -> None:
    assert normalize_issue({"key": "PROJ-1", "fields": {}}, SITE, BASE_URL) is None
    no_project = issue()
    del no_project["fields"]["project"]
    assert normalize_issue(no_project, SITE, BASE_URL) is None


def test_key_survives_a_move_between_projects() -> None:
    before = normalize_issue(issue(), SITE, BASE_URL)
    after = normalize_issue(issue(key="OTHER-7", project=("10200", "OTHER")), SITE, BASE_URL)
    assert before is not None and after is not None
    assert before.key == after.key
    assert after.scope.container_id == "10200"


def test_keys_are_deterministic_and_arango_safe() -> None:
    first = normalize_issue(issue(), SITE, BASE_URL)
    again = normalize_issue(issue(updated=CREATED + timedelta(days=1)), SITE, BASE_URL)
    c1 = normalize_comment(comment(), issue(), SITE)
    c2 = normalize_comment(comment(updated=CREATED + timedelta(days=1)), issue(), SITE)
    assert first is not None and again is not None and c1 is not None and c2 is not None
    assert first.key == again.key
    assert c1.key == c2.key == "jira_acme.atlassian.net_comment_20001"
    for key in (first.key, c1.key):
        assert ARANGO_KEY_RE.match(key), key


def test_comment_field_mapping() -> None:
    edited = CREATED + timedelta(minutes=5)
    env = normalize_comment(comment(updated=edited), issue(), SITE)

    assert env is not None
    assert env.kind == "comment"
    assert env.source_id == "20001"
    assert env.scope.tenant_id == SITE
    assert env.scope.container_id == "10000"
    assert env.scope.path == ["10001"]
    assert env.created_at == CREATED
    assert env.updated_at == edited
    p = env.payload
    assert p.text == "Looks good to me"
    assert (p.author_id, p.author_name, p.author_type) == (
        "5b10a2844c20165700ede21g",
        "Ada Lovelace",
        "user",
    )
    assert (p.parent_id, p.parent_key) == ("10001", "PROJ-1")
    assert p.visibility is None
    assert env.raw["body"]["type"] == "doc"


def test_comment_author_types() -> None:
    def author_type(author: dict[str, object] | None) -> str:
        built = comment(author=author)
        if author is None:
            del built["author"]
        env = normalize_comment(built, issue(), SITE)
        assert env is not None
        return env.payload.author_type

    assert author_type(user(account_type="app")) == "bot"
    assert author_type(user(account_type="customer")) == "user"
    assert author_type(user(account_type="unknown")) == "user"
    assert author_type(None) == "system"


def test_restricted_comment_keeps_visibility() -> None:
    built = comment(visibility={"type": "role", "value": "Developers", "identifier": "10002"})
    env = normalize_comment(built, issue(), SITE)
    assert env is not None
    assert env.payload.visibility is not None
    assert env.to_document()["payload"]["visibility"] == {"type": "role", "value": "Developers"}


def test_comment_without_id_is_skipped() -> None:
    built = comment()
    del built["id"]
    assert normalize_comment(built, issue(), SITE) is None


def test_documents_are_json_safe() -> None:
    issue_env = normalize_issue(issue(), SITE, BASE_URL)
    comment_env = normalize_comment(comment(), issue(), SITE)
    assert issue_env is not None and comment_env is not None
    doc = issue_env.to_document()
    assert doc["_key"] == issue_env.key
    assert doc["kind"] == "issue"
    assert doc["payload"]["due_date"] == "2024-06-30"
    assert doc["created_at"] == "2024-05-23T21:33:20.123000Z"
    assert comment_env.to_document()["kind"] == "comment"


def test_embedded_comments() -> None:
    comments, total = embedded_comments(issue(comments=[comment()], comment_total=40))
    assert [c["id"] for c in comments] == ["20001"]
    assert total == 40
    assert embedded_comments({"fields": {}}) == ([], 0)


def test_created_at_is_utc() -> None:
    env = normalize_issue(issue(), SITE, BASE_URL)
    assert env is not None and env.created_at is not None
    assert env.created_at.tzinfo is not None
    assert env.created_at.utcoffset() == timedelta(0)
