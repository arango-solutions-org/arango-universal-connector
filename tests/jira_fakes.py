"""Builders for Jira REST v3 shapes and an in-memory JiraClient for adapter tests."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

SITE = "acme.atlassian.net"
BASE_URL = f"https://{SITE}"
CREATED = datetime(2024, 5, 23, 21, 33, 20, 123000, tzinfo=UTC)

# The embedded comment page size Jira uses in search results (smaller here for tests).
EMBEDDED_COMMENTS = 3


def jira_time(moment: datetime) -> str:
    """Jira's timestamp format: ``2024-05-23T21:33:20.123+0000``."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}+0000"


def adf(*paragraphs: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            for text in paragraphs
        ],
    }


def user(
    account_id: str = "5b10a2844c20165700ede21g",
    name: str = "Ada Lovelace",
    account_type: str = "atlassian",
) -> dict[str, Any]:
    return {
        "accountId": account_id,
        "displayName": name,
        "accountType": account_type,
        "active": True,
    }


def comment(
    comment_id: str = "20001",
    text: str = "Looks good to me",
    *,
    author: dict[str, Any] | None = None,
    created: datetime = CREATED,
    updated: datetime | None = None,
    visibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    built: dict[str, Any] = {
        "self": f"{BASE_URL}/rest/api/3/issue/10001/comment/{comment_id}",
        "id": comment_id,
        "author": author if author is not None else user(),
        "body": adf(text),
        "created": jira_time(created),
        "updated": jira_time(updated or created),
        "jsdPublic": True,
    }
    if visibility is not None:
        built["visibility"] = visibility
    return built


def issue(
    issue_id: str = "10001",
    key: str = "PROJ-1",
    *,
    project: tuple[str, str] = ("10000", "PROJ"),
    summary: str = "Login page times out",
    description: dict[str, Any] | None = None,
    created: datetime = CREATED,
    updated: datetime | None = None,
    comments: list[dict[str, Any]] | None = None,
    comment_total: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    project_id, project_key = project
    comments = comments or []
    base_fields: dict[str, Any] = {
        "summary": summary,
        "description": description if description is not None else adf("Steps to reproduce"),
        "status": {
            "name": "In Progress",
            "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"},
        },
        "issuetype": {"id": "10002", "name": "Bug", "subtask": False},
        "priority": {"id": "3", "name": "Medium"},
        "project": {"id": project_id, "key": project_key, "name": "Project"},
        "assignee": user("5b10ac8d82e05b22cc7d4ef5", "Grace Hopper"),
        "reporter": user(),
        "creator": user(),
        "labels": ["frontend", "auth"],
        "components": [{"id": "10100", "name": "Web"}],
        "resolution": None,
        "resolutiondate": None,
        "duedate": "2024-06-30",
        "created": jira_time(created),
        "updated": jira_time(updated or created),
        "comment": {
            "comments": comments,
            "maxResults": len(comments),
            "total": comment_total if comment_total is not None else len(comments),
            "startAt": 0,
        },
    }
    return {
        "id": issue_id,
        "key": key,
        "self": f"{BASE_URL}/rest/api/3/issue/{issue_id}",
        "fields": base_fields | fields,
    }


def _floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


class FakeJiraClient:
    """An in-memory Jira site behaving like the search and comment APIs.

    Searches compare ``updated`` to the minute, as JQL does, and embed only the first
    ``EMBEDDED_COMMENTS`` comments of each issue. Adding a comment bumps its issue's
    ``updated``, as Jira does.
    """

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.failing_projects: set[str] = set()
        self.searches: list[tuple[str, datetime | None]] = []
        self.comment_fetches: list[str] = []
        self.jqls: list[str] = []
        self.ignore_scope = False

    # -- test setup ------------------------------------------------------------------

    def put_issue(self, built: dict[str, Any]) -> None:
        stored = copy.deepcopy(built)
        self.comments.setdefault(stored["id"], [])
        stored["fields"].pop("comment", None)
        self.issues[stored["id"]] = stored

    def touch(self, issue_id: str, when: datetime, **fields: Any) -> None:
        self.issues[issue_id]["fields"].update(fields, updated=jira_time(when))

    def add_comment(self, issue_id: str, built: dict[str, Any]) -> None:
        self.comments[issue_id].append(copy.deepcopy(built))
        self.touch(issue_id, datetime.fromisoformat(built["updated"]))

    def edit_comment(self, issue_id: str, comment_id: str, text: str, when: datetime) -> None:
        for stored in self.comments[issue_id]:
            if stored["id"] == comment_id:
                stored["body"] = adf(text)
                stored["updated"] = jira_time(when)
        self.touch(issue_id, when)

    # -- JiraClient ------------------------------------------------------------------

    def search_issues(
        self, project: str, updated_since: datetime | None
    ) -> Iterator[list[dict[str, Any]]]:
        self.searches.append((project, updated_since))
        if project in self.failing_projects:
            raise RuntimeError(f"project {project} unavailable")
        since = _floor_minute(updated_since) if updated_since is not None else None
        matches = [
            i
            for i in self.issues.values()
            if i["fields"]["project"]["key"] == project
            and (since is None or datetime.fromisoformat(i["fields"]["updated"]) >= since)
        ]
        matches.sort(key=lambda i: (datetime.fromisoformat(i["fields"]["updated"]), i["key"]))
        for start in range(0, len(matches), self.page_size):
            yield [self._with_comments(i) for i in matches[start : start + self.page_size]]

    def search_jql(self, jql: str) -> Iterator[list[dict[str, Any]]]:
        """Honours only the ``project in (...)`` clause the link adds (unless
        ``ignore_scope`` simulates a Jira that returns more than asked). Records the JQL."""
        self.jqls.append(jql)
        match = re.match(r"project in \(([^)]*)\)", jql)
        projects = set(re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))) if match else set()
        matches = [
            i
            for i in self.issues.values()
            if self.ignore_scope or i["fields"]["project"]["key"] in projects
        ]
        matches.sort(key=lambda i: (datetime.fromisoformat(i["fields"]["updated"]), i["key"]))
        for start in range(0, len(matches), self.page_size):
            yield [self._with_comments(i) for i in matches[start : start + self.page_size]]

    def get_issue(self, key: str) -> dict[str, Any] | None:
        for stored in self.issues.values():
            if stored["key"] == key:
                return self._with_comments(stored)
        return None

    def time_zone(self) -> Any:
        return UTC

    def list_comments(self, issue_id: str) -> Iterator[dict[str, Any]]:
        self.comment_fetches.append(issue_id)
        yield from copy.deepcopy(self.comments.get(issue_id, []))

    def _with_comments(self, stored: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(stored)
        all_comments = self.comments.get(stored["id"], [])
        result["fields"]["comment"] = {
            "comments": copy.deepcopy(all_comments[:EMBEDDED_COMMENTS]),
            "maxResults": EMBEDDED_COMMENTS,
            "total": len(all_comments),
            "startAt": 0,
        }
        return result
