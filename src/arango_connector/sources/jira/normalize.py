"""Pure mapping from Jira Cloud REST v3 issues and comments to Envelopes. No I/O."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from arango_connector.envelope import Envelope, Scope, make_key
from arango_connector.payloads import CommentPayload, CommentVisibility, IssuePayload
from arango_connector.sources.jira.adf import adf_to_text

SOURCE = "jira"

# Jira ``accountType`` -> payload ``author_type``. ``customer`` is a Service Management
# portal user, which is still a person.
_AUTHOR_TYPES: dict[str, Literal["user", "bot", "system"]] = {
    "atlassian": "user",
    "customer": "user",
    "app": "bot",
}


def parse_jira_datetime(value: Any) -> datetime | None:
    """Parse a Jira timestamp (``2024-05-23T21:33:20.123+0000``) to an aware UTC datetime.

    Returns ``None`` for missing or unparseable values rather than failing the item.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _name(field: Any, attr: str = "name") -> str | None:
    """``field[attr]`` for the ``{"name": ...}`` objects Jira uses for most enumerations."""
    if isinstance(field, dict):
        value = field.get(attr)
        return str(value) if value is not None else None
    return None


def _account(user: Any) -> tuple[str | None, str | None]:
    """``(accountId, displayName)`` of a Jira user object, or ``(None, None)``."""
    if not isinstance(user, dict):
        return None, None
    account_id = user.get("accountId")
    return (str(account_id) if account_id else None), _name(user, "displayName")


def issue_key(site: str, issue_id: str) -> str:
    return make_key(SOURCE, site, "issue", issue_id)


def comment_key(site: str, comment_id: str) -> str:
    return make_key(SOURCE, site, "comment", comment_id)


def embedded_comments(issue: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The comments embedded in a search result, and how many the issue has in total.

    Search results carry only the first page of comments; when the total is larger the
    adapter fetches the rest.
    """
    field = (issue.get("fields") or {}).get("comment")
    if not isinstance(field, dict):
        return [], 0
    comments = [c for c in field.get("comments") or [] if isinstance(c, dict)]
    total = field.get("total")
    return comments, int(total) if isinstance(total, int) else len(comments)


def normalize_issue(
    issue: dict[str, Any], site: str, base_url: str
) -> Envelope[IssuePayload] | None:
    """Map a Jira issue to an Envelope, or return ``None`` if it lacks an id, key or project.

    The Envelope key uses the numeric issue id, which survives moves between projects
    (the ``PROJ-123`` key does not). ``raw`` omits ``fields.comment``: comments are stored
    as documents of their own.
    """
    issue_id = issue.get("id")
    key = issue.get("key")
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    project_id = project.get("id") if isinstance(project, dict) else None
    if not issue_id or not key or not project_id:
        return None
    issue_id, key = str(issue_id), str(key)

    status = fields.get("status")
    category = status.get("statusCategory") if isinstance(status, dict) else None
    parent = fields.get("parent")
    assignee_id, assignee_name = _account(fields.get("assignee"))
    reporter_id, reporter_name = _account(fields.get("reporter"))
    _, comment_total = embedded_comments(issue)

    raw = dict(issue)
    raw["fields"] = {k: v for k, v in fields.items() if k != "comment"}

    return Envelope[IssuePayload](
        key=issue_key(site, issue_id),
        source=SOURCE,
        source_id=issue_id,
        scope=Scope(tenant_id=site, container_id=str(project_id)),
        created_at=parse_jira_datetime(fields.get("created")),
        updated_at=parse_jira_datetime(fields.get("updated")),
        payload=IssuePayload(
            issue_key=key,
            project_key=_name(project, "key"),
            summary=str(fields.get("summary") or ""),
            description=adf_to_text(fields.get("description")),
            issue_type=_name(fields.get("issuetype")),
            status=_name(status),
            status_category=_name(category, "key"),
            priority=_name(fields.get("priority")),
            resolution=_name(fields.get("resolution")),
            resolved_at=parse_jira_datetime(fields.get("resolutiondate")),
            due_date=_parse_date(fields.get("duedate")),
            labels=[str(label) for label in fields.get("labels") or []],
            components=[n for c in fields.get("components") or [] if (n := _name(c))],
            assignee_id=assignee_id,
            assignee_name=assignee_name,
            reporter_id=reporter_id,
            reporter_name=reporter_name,
            parent_id=_name(parent, "id"),
            parent_key=_name(parent, "key"),
            url=f"{base_url}/browse/{key}",
            comment_count=comment_total,
        ),
        raw=raw,
    )


def normalize_comment(
    comment: dict[str, Any], issue: dict[str, Any], site: str
) -> Envelope[CommentPayload] | None:
    """Map a comment on ``issue`` to an Envelope, or ``None`` if either lacks what it needs.

    Comments live under their issue: ``scope.container_id`` is the issue's project and
    ``scope.path`` is ``[issue id]``.
    """
    comment_id = comment.get("id")
    issue_id = issue.get("id")
    project = (issue.get("fields") or {}).get("project") or {}
    project_id = project.get("id") if isinstance(project, dict) else None
    if not comment_id or not issue_id or not project_id:
        return None
    issue_id = str(issue_id)

    author = comment.get("author")
    author_id, author_name = _account(author)
    account_type = author.get("accountType") if isinstance(author, dict) else None
    visibility = comment.get("visibility")
    restricted = (
        CommentVisibility(type=str(visibility["type"]), value=str(visibility["value"]))
        if isinstance(visibility, dict) and visibility.get("type") and visibility.get("value")
        else None
    )

    return Envelope[CommentPayload](
        key=comment_key(site, str(comment_id)),
        source=SOURCE,
        source_id=str(comment_id),
        scope=Scope(tenant_id=site, container_id=str(project_id), path=[issue_id]),
        created_at=parse_jira_datetime(comment.get("created")),
        updated_at=parse_jira_datetime(comment.get("updated")),
        payload=CommentPayload(
            author_id=author_id,
            author_name=author_name,
            author_type=_AUTHOR_TYPES.get(str(account_type), "user") if author else "system",
            text=adf_to_text(comment.get("body")),
            parent_id=issue_id,
            parent_key=str(issue["key"]) if issue.get("key") else None,
            visibility=restricted,
        ),
        raw=comment,
    )
