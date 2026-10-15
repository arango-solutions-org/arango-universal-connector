"""Jira operations shared by every runtime: per-issue comment envelopes.

The standalone service builds its incremental sync on these; a Links job calls them
directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector.sources.jira.client import JiraClient
from arango_connector.sources.jira.normalize import (
    embedded_comments,
    normalize_comment,
    parse_jira_datetime,
)


def comment_envelopes(
    client: JiraClient,
    issue: dict[str, Any],
    site: str,
    *,
    since: datetime | None = None,
) -> Iterator[Envelope[Any]]:
    """Envelopes for the issue's comments, optionally only those updated since ``since``.

    Search results embed only the first page of comments; the rest are fetched.
    """
    comments, total = embedded_comments(issue)
    if total > len(comments):
        comments = list(client.list_comments(str(issue["id"])))
    for comment in comments:
        if since is not None:
            updated = parse_jira_datetime(comment.get("updated") or comment.get("created"))
            if updated is not None and updated < since:
                continue
        envelope = normalize_comment(comment, issue, site)
        if envelope is not None:
            yield envelope


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def scoped_jql(jql: str, projects: Iterable[str]) -> str:
    """``jql`` restricted to ``projects``: ``project in (...) AND (<jql>) [ORDER BY ...]``.

    The caller's query is parenthesised, so its own ``OR`` clauses can't widen the scope;
    a trailing ``ORDER BY`` is kept outside the parentheses, where JQL requires it.
    """
    quoted = ", ".join('"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"' for p in projects)
    scope = f"project in ({quoted})"
    query, order = jql.strip(), ""
    start = _last_order_by(query)
    if start is not None:
        query, order = query[:start].strip(), " " + query[start:].strip()
    return f"{scope} AND ({query}){order}" if query else f"{scope}{order}"


def _last_order_by(jql: str) -> int | None:
    """Where the query's own ``ORDER BY`` starts, ignoring text inside quotes."""
    outside = []  # the query with quoted text blanked out, same length
    quote: str | None = None
    escaped = False
    for char in jql:
        if quote:
            outside.append(" ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        else:
            if char in "\"'":
                quote = char
            outside.append(" " if quote else char)
    matches = list(_ORDER_BY.finditer("".join(outside)))
    return matches[-1].start() if matches else None


def issue_project(issue: dict[str, Any]) -> str | None:
    """The issue's project key, as returned by Jira."""
    project = (issue.get("fields") or {}).get("project") or {}
    key = project.get("key")
    return str(key) if key else None
