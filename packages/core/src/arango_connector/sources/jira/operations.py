"""Jira operations shared by every runtime: per-issue comment envelopes.

The standalone service builds its incremental sync on these; a Links job calls them
directly.
"""

from __future__ import annotations

from collections.abc import Iterator
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
