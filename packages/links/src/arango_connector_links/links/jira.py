"""Jira as a Link: search with JQL, read an issue, or export a project's changes.

``JIRA_PROJECTS`` lists the projects jobs may read: every search is limited to them in the
JQL itself, and each issue returned is checked again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arango_connector.envelope import Envelope
from arango_connector.sources.jira.client import (
    JiraAuthError,
    JiraClient,
    JiraError,
    format_jql_time,
)
from arango_connector.sources.jira.config import JiraSettings
from arango_connector.sources.jira.normalize import normalize_issue
from arango_connector.sources.jira.operations import comment_envelopes, issue_project, scoped_jql
from arango_connector_links.link import Example, JobContext, JobError, Link


class _Common(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_comments: bool = Field(default=True, description="Also return each issue's comments.")


class Search(_Common):
    """Issues matching a JQL query, within the link's projects."""

    operation: Literal["search"]
    query: str = Field(description='JQL, e.g. "status = Done ORDER BY updated DESC".')
    max_results: int = Field(default=100, ge=1, le=1000, description="Issues to return.")


class GetIssue(_Common):
    """One issue by key."""

    operation: Literal["get_issue"]
    key: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$", description='e.g. "PROJ-123".')


class Export(_Common):
    """A project's issues updated in a time range, oldest change first."""

    operation: Literal["export"]
    project: str = Field(min_length=1, description="Project key; must be one of the link's.")
    updated_after: datetime | None = Field(default=None, description="Updated at or after.")
    updated_before: datetime | None = Field(default=None, description="Updated before.")
    max_results: int = Field(default=5000, ge=1, le=50000, description="Issues to return.")


JiraInput = Annotated[Search | GetIssue | Export, Field(discriminator="operation")]


class JiraLink(Link[Search | GetIssue | Export]):
    name = "jira"
    description = (
        "Read Jira Cloud issues: search with JQL, fetch one issue, or export a project's "
        "issues updated in a time range. Returns one document per issue and per comment, "
        "as JSONL."
    )
    tags = ("issues", "jira", "atlassian")
    input_type = JiraInput
    examples = (
        Example(
            "search",
            {
                "operation": "search",
                "query": "status = Done ORDER BY updated DESC",
                "max_results": 20,
            },
            "The 20 most recently updated done issues, with comments.",
        ),
        Example(
            "get_issue",
            {"operation": "get_issue", "key": "PROJ-123"},
            "The issue and its comments.",
        ),
        Example(
            "export",
            {"operation": "export", "project": "PROJ", "updated_after": "2026-10-01T00:00:00Z"},
            "PROJ issues changed since 1 October.",
        ),
    )

    def __init__(self, settings: JiraSettings, client_factory: Callable[[], JiraClient]) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._client: JiraClient | None = None

    def run(
        self, job_input: Search | GetIssue | Export, ctx: JobContext
    ) -> Iterator[Envelope[Any]]:
        client = self._jira()
        allowed = list(self.settings.jira_projects)
        try:
            if isinstance(job_input, GetIssue):
                issue = client.get_issue(job_input.key)
                if issue is None or issue_project(issue) not in allowed:
                    # The same answer either way, so a job can't probe other projects.
                    raise JobError(f"issue {job_input.key!r} not found in this link's projects")
                yield from self._issue(client, issue, job_input.include_comments, ctx)
                return

            if isinstance(job_input, Search):
                try:
                    jql = scoped_jql(job_input.query, allowed)
                except ValueError as exc:
                    raise JobError(f"invalid query: {exc}") from None
            else:
                if job_input.project not in allowed:
                    raise JobError(
                        f"project {job_input.project!r} is not one of this link's projects"
                    )
                jql = scoped_jql(self._range(client, job_input), [job_input.project])
            issues = 0
            for page in client.search_jql(jql):
                for issue in page:
                    if issues >= job_input.max_results:
                        return
                    if issue_project(issue) not in allowed:
                        continue  # defence in depth: the JQL already limits projects
                    yield from self._issue(client, issue, job_input.include_comments, ctx)
                    issues += 1
        except JiraAuthError:
            raise JobError("Jira rejected the link's credentials or permissions") from None
        except JiraError as exc:
            if exc.status == 400:
                raise JobError(f"Jira rejected the query: {exc}") from None
            raise

    def _issue(
        self, client: JiraClient, issue: dict[str, Any], comments: bool, ctx: JobContext
    ) -> Iterator[Envelope[Any]]:
        ctx.check()
        envelope = normalize_issue(issue, self.settings.jira_site, self.settings.jira_base_url)
        if envelope is None:
            return
        yield envelope
        if comments:
            yield from comment_envelopes(client, issue, self.settings.jira_site)

    def _range(self, client: JiraClient, job: Export) -> str:
        zone = client.time_zone()
        clauses = []
        if job.updated_after is not None:
            clauses.append(f'updated >= "{format_jql_time(job.updated_after, zone)}"')
        if job.updated_before is not None:
            clauses.append(f'updated < "{format_jql_time(job.updated_before, zone)}"')
        return " AND ".join(clauses) + " ORDER BY updated ASC, key ASC"

    def _jira(self) -> JiraClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client
