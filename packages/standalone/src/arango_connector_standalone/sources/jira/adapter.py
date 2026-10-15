"""Jira Cloud polling source: incremental JQL sync of issues and comments per project."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from arango_connector.sources.jira.client import JiraClient, JiraInterrupted
from arango_connector.sources.jira.config import InitialSync
from arango_connector.sources.jira.normalize import (
    normalize_issue,
    parse_jira_datetime,
)
from arango_connector.sources.jira.operations import comment_envelopes
from arango_connector_standalone.sources.base import EmitFn
from arango_connector_standalone.sources.polling import PollingSourceAdapter
from arango_connector_standalone.state.base import StateStore

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "jira"

# Each search re-reads from this long before the cursor. JQL compares dates to the
# minute, and Jira's search index can trail an update by a few seconds; re-reading
# covers both. Duplicates are harmless: keys are deterministic.
OVERLAP = timedelta(minutes=2)

# Builds the client, given a callable that says whether the adapter is stopping (so a
# long rate-limit wait can end early).
ClientFactory = Callable[[Callable[[], bool]], JiraClient]


@dataclass
class _Window:
    """What one sync of a project reads."""

    # Issues updated at or after this (``None``: every issue in the project).
    issues_since: datetime | None
    # Comments updated at or after this (``None``: every comment of each issue read).
    comments_since: datetime | None
    # Where the cursor stood before this sync (``None`` on a first sync).
    cursor: datetime | None
    initial: bool


class JiraPollingAdapter(PollingSourceAdapter):
    """Polls each configured project for issues updated since its cursor.

    For every updated issue it emits the issue and the comments changed in the same
    window. Jira bumps an issue's ``updated`` when a comment is added or edited, so the
    issue search also finds comment changes. The cursor per project (the newest
    ``updated`` time read) lives in the StateStore and is checkpointed after every page,
    so a restart or a failure resumes rather than starting over. Delivery is
    at-least-once; deterministic keys make repeats harmless.

    TODO(future): webhooks instead of polling, for lower latency and fewer requests.
    TODO(future): deletes (issues and comments) once Envelope/Sink support them.
    TODO(future): custom field mapping and attachments.
    """

    name = "jira"

    def __init__(
        self,
        projects: list[str],
        client_factory: ClientFactory,
        state: StateStore,
        *,
        site: str,
        base_url: str,
        initial_sync: InitialSync = "lookback",
        initial_lookback: timedelta = timedelta(days=30),
        interval_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(interval_seconds)
        self._projects = list(projects)
        self._client_factory = client_factory
        self._client: JiraClient | None = None
        self._state = state
        self._site = site
        self._base_url = base_url
        self._initial_sync = initial_sync
        self._initial_lookback = initial_lookback
        self._clock = clock

    def poll_once(self, emit: EmitFn) -> None:
        """Sync every project. One failing project does not stop the others.

        Raises only if every project failed, which points at something systemic (network,
        credentials) and lets the polling base back off.
        """
        if self._client is None:
            self._client = self._client_factory(lambda: self.stopping)
        failures = 0
        for project in self._projects:
            if self.stopping:
                return
            try:
                self._sync(self._client, project, emit)
            except JiraInterrupted:
                logger.info("jira_sync_interrupted project=%s cursor at last checkpoint", project)
                return
            except Exception:
                failures += 1
                logger.exception("jira_project_sync_failed project=%s", project)
        if failures and failures == len(self._projects):
            raise RuntimeError(f"all {failures} Jira project(s) failed to sync")

    # -- per project ---------------------------------------------------------------

    def _sync(self, client: JiraClient, project: str, emit: EmitFn) -> None:
        started_at = self._clock()
        window = self._window(project, started_at)
        if window is None:
            return

        newest = window.cursor
        issues = comments = skipped = 0
        for page in client.search_issues(project, window.issues_since):
            for issue in page:
                if self.stopping:
                    # The checkpoint after the last full page stands.
                    raise JiraInterrupted(project)
                envelope = normalize_issue(issue, self._site, self._base_url)
                if envelope is None:
                    skipped += 1
                    continue
                emit(envelope)
                issues += 1
                comments += self._emit_comments(client, issue, window.comments_since, emit)
                if envelope.updated_at is not None and (
                    newest is None or envelope.updated_at > newest
                ):
                    newest = envelope.updated_at
            if newest is not None:
                self._save_cursor(project, newest, started_at, initial=window.initial)

        # A complete sync has seen everything up to when it started; idle projects then
        # stop re-reading their newest issue on every poll.
        final = max(newest, started_at) if newest is not None else started_at
        self._save_cursor(project, final, started_at, initial=False)
        if issues or skipped:
            logger.info(
                "jira_project_synced project=%s issues=%s comments=%s skipped=%s",
                project,
                issues,
                comments,
                skipped,
            )
        if window.initial:
            logger.info(
                "jira_project_initialised project=%s initial_sync=%s issues=%s",
                project,
                self._initial_sync,
                issues,
            )

    def _window(self, project: str, started_at: datetime) -> _Window | None:
        """What to read this time, or ``None`` when a first ``now`` sync only sets a cursor."""
        cursor = self._state.get(STATE_NAMESPACE, self._cursor_key(project))
        if cursor is None:
            if self._initial_sync == "now":
                self._save_cursor(project, started_at, started_at, initial=False)
                logger.info(
                    "jira_project_initialised project=%s initial_sync=now since=%s",
                    project,
                    started_at.isoformat(),
                )
                return None
            since = None if self._initial_sync == "all" else started_at - self._initial_lookback
            return _Window(issues_since=since, comments_since=None, cursor=None, initial=True)

        updated_since = parse_jira_datetime(cursor.get("updated_since"))
        if updated_since is None:
            raise ValueError(f"unreadable Jira cursor for {project}: {cursor!r}")
        since = updated_since - OVERLAP
        # An initial sync cut short keeps reading every comment of the issues it has not
        # reached yet; their comments can be older than the cursor.
        initial = bool(cursor.get("initial"))
        return _Window(
            issues_since=since,
            comments_since=None if initial else since,
            cursor=updated_since,
            initial=initial,
        )

    def _emit_comments(
        self,
        client: JiraClient,
        issue: dict[str, Any],
        since: datetime | None,
        emit: EmitFn,
    ) -> int:
        emitted = 0
        for envelope in comment_envelopes(client, issue, self._site, since=since):
            emit(envelope)
            emitted += 1
        return emitted

    # -- cursor --------------------------------------------------------------------

    def _cursor_key(self, project: str) -> str:
        return f"{self._site}:{project}"

    def _save_cursor(
        self, project: str, updated_since: datetime, synced_at: datetime, *, initial: bool
    ) -> None:
        value: dict[str, Any] = {
            "updated_since": updated_since.isoformat(),
            "last_synced_at": synced_at.isoformat(),
        }
        if initial:
            value["initial"] = True
        self._state.put(STATE_NAMESPACE, self._cursor_key(project), value)
