"""The Slack and Jira links: operations, scoping and error handling, against fakes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from arango_connector.sources.jira.config import JiraSettings
from arango_connector.sources.slack.config import SlackApiSettings
from arango_connector_links.link import RESULT_FILE, JobContext, JobError, Link
from arango_connector_links.links.jira import JiraLink
from arango_connector_links.links.slack import SlackLink
from arango_connector_links.runtime import LinkRuntime
from jira_fakes import BASE_URL, FakeJiraClient, comment, issue
from links_fakes import S, SidecarServer


def run(link: Link[Any], **job: Any) -> list[Any]:
    return list(link.run(link.parse_input(json.dumps(job).encode()), JobContext(job_id="j")))


# -- slack ----------------------------------------------------------------------------

CHAN = "C0TESTCHAN1"


def slack_message(ts: str, text: str = "hi", user: str = "U1", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "ts": ts, "user": user, "text": text, **extra}


class FakeSlack:
    """``WebClient`` stand-in: cursor-paged history and replies, users.info, auth.test."""

    def __init__(self, page_size: int = 2) -> None:
        self.history: list[dict[str, Any]] = []
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: str | None = None

    def _page(self, items: list[dict[str, Any]], cursor: str | None) -> dict[str, Any]:
        start = int(cursor or 0)
        end = start + self.page_size
        more = end < len(items)
        return {
            "messages": items[start:end],
            "response_metadata": {"next_cursor": str(end) if more else ""},
        }

    def conversations_history(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("history", kw))
        if self.error:
            raise SlackApiError(self.error, {"ok": False, "error": self.error})
        items = [
            m
            for m in self.history
            if ("oldest" not in kw or float(m["ts"]) >= float(kw["oldest"]))
            and ("latest" not in kw or float(m["ts"]) <= float(kw["latest"]))
        ]
        return self._page(items, kw.get("cursor"))

    def conversations_replies(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("replies", kw))
        if kw["ts"] not in self.threads:
            raise SlackApiError("x", {"ok": False, "error": "thread_not_found"})
        return self._page(self.threads[kw["ts"]], kw.get("cursor"))

    def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"real_name": f"Real {user}"}}

    def auth_test(self) -> dict[str, Any]:
        return {"team_id": "T0TEAM"}


def slack_link(slack: FakeSlack) -> SlackLink:
    settings = SlackApiSettings.model_construct(slack_channel_ids={CHAN})
    return SlackLink(settings, lambda: slack)


def test_channel_history_pages_and_fills_names() -> None:
    slack = FakeSlack()
    slack.history = [slack_message(f"17165000{n:02d}.000100") for n in range(5)]
    result = run(slack_link(slack), operation="channel_history", channel=CHAN)
    assert len(result) == 5
    assert result[0].scope.tenant_id == "T0TEAM"
    assert result[0].scope.container_id == CHAN
    assert result[0].payload.author_name == "Real U1"
    assert len([c for c in slack.calls if c[0] == "history"]) == 3  # 2 + 2 + 1


def test_channel_history_time_range_and_limit() -> None:
    slack = FakeSlack()
    slack.history = [slack_message(f"{1716500000 + n}.000000") for n in range(10)]
    result = run(
        slack_link(slack),
        operation="channel_history",
        channel=CHAN,
        oldest=datetime.fromtimestamp(1716500003, tz=UTC).isoformat(),
        max_results=2,
    )
    assert [e.source_id for e in result] == ["1716500003.000000", "1716500004.000000"]
    assert slack.calls[0][1]["oldest"] == "1716500003.000000"


def test_joins_and_other_subtypes_are_skipped() -> None:
    slack = FakeSlack()
    slack.history = [
        slack_message("1.000001"),
        slack_message("2.000001", subtype="channel_join"),
        slack_message("3.000001", subtype="bot_message", bot_id="B1", username="deploy-bot"),
    ]
    result = run(slack_link(slack), operation="channel_history", channel=CHAN)
    assert [e.source_id for e in result] == ["1.000001", "3.000001"]
    assert result[1].payload.author_name == "deploy-bot"


def test_get_thread() -> None:
    slack = FakeSlack()
    root = "1716500000.123456"
    slack.threads[root] = [
        slack_message(root, "root", thread_ts=root),
        slack_message("1716500100.000200", "reply", thread_ts=root),
    ]
    result = run(slack_link(slack), operation="get_thread", channel=CHAN, thread_ts=root)
    assert [e.payload.text for e in result] == ["root", "reply"]
    assert result[1].payload.parent_id == root


def test_slack_channel_scope() -> None:
    with pytest.raises(JobError, match="not one of this link's channels"):
        run(slack_link(FakeSlack()), operation="channel_history", channel="C0OTHER")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("not_in_channel", "bot is not a member"),
        ("channel_not_found", "channel not found"),
        ("missing_scope", "lacks a scope"),
    ],
)
def test_slack_access_errors_become_job_errors(error: str, message: str) -> None:
    slack = FakeSlack()
    slack.error = error
    with pytest.raises(JobError, match=message):
        run(slack_link(slack), operation="channel_history", channel=CHAN)


def test_unknown_thread() -> None:
    with pytest.raises(JobError, match="thread not found"):
        run(slack_link(FakeSlack()), operation="get_thread", channel=CHAN, thread_ts="9.9")


# -- jira -------------------------------------------------------------------------------


def jira_link(jira: FakeJiraClient, projects: tuple[str, ...] = ("PROJ",)) -> JiraLink:
    settings = JiraSettings.model_construct(
        jira_base_url=BASE_URL, jira_projects=list(projects), jira_page_size=50
    )
    return JiraLink(settings, lambda: jira)


@pytest.fixture
def jira() -> FakeJiraClient:
    fake = FakeJiraClient()
    fake.put_issue(issue("1", "PROJ-1"))
    fake.put_issue(issue("2", "PROJ-2"))
    fake.put_issue(issue("3", "SECRET-1", project=("20000", "SECRET")))
    fake.add_comment("1", comment("100", "first"))
    return fake


def test_search_is_scoped_in_the_jql(jira: FakeJiraClient) -> None:
    result = run(jira_link(jira), operation="search", query="assignee = x OR project = SECRET")
    assert jira.jqls == ['project in ("PROJ") AND (assignee = x OR project = SECRET)']
    assert sorted(e.source_id for e in result if e.kind == "issue") == ["1", "2"]
    assert [e.source_id for e in result if e.kind == "comment"] == ["100"]


def test_results_outside_the_scope_are_dropped_even_if_jira_returns_them(
    jira: FakeJiraClient,
) -> None:
    jira.ignore_scope = True
    result = run(jira_link(jira), operation="search", query="x")
    assert "3" not in [e.source_id for e in result]


def test_search_limit_and_comments_toggle(jira: FakeJiraClient) -> None:
    result = run(
        jira_link(jira), operation="search", query="x", max_results=1, include_comments=False
    )
    assert [e.kind for e in result] == ["issue"]


def test_get_issue(jira: FakeJiraClient) -> None:
    result = run(jira_link(jira), operation="get_issue", key="PROJ-1")
    assert [e.kind for e in result] == ["issue", "comment"]


@pytest.mark.parametrize("key", ["SECRET-1", "PROJ-999"])
def test_get_issue_outside_scope_or_missing_looks_the_same(jira: FakeJiraClient, key: str) -> None:
    with pytest.raises(JobError, match="not found in this link's projects"):
        run(jira_link(jira), operation="get_issue", key=key)


def test_get_issue_key_is_validated(jira: FakeJiraClient) -> None:
    with pytest.raises(JobError, match="invalid input"):
        jira_link(jira).parse_input(b'{"operation": "get_issue", "key": "../../myself"}')


def test_export_builds_a_range_query(jira: FakeJiraClient) -> None:
    run(
        jira_link(jira),
        operation="export",
        project="PROJ",
        updated_after="2026-10-01T00:00:00Z",
        updated_before="2026-10-15T12:30:00Z",
    )
    assert jira.jqls == [
        'project in ("PROJ") AND (updated >= "2026/10/01 00:00" AND '
        'updated < "2026/10/15 12:30") ORDER BY updated ASC, key ASC'
    ]


def test_export_project_scope(jira: FakeJiraClient) -> None:
    with pytest.raises(JobError, match="not one of this link's projects"):
        run(jira_link(jira), operation="export", project="SECRET")


# -- through the runtime ----------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[SidecarServer]:
    s = SidecarServer()
    yield s
    s.close()


def test_jira_job_end_to_end(jira: FakeJiraClient, server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"operation": "get_issue", "key": "PROJ-1"}')
    LinkRuntime(jira_link(jira), server.stub).run_once()
    assert job.state == S.JOB_STATE_COMPLETED
    kinds = [json.loads(line)["kind"] for line in job.files[RESULT_FILE].splitlines()]
    assert kinds == ["issue", "comment"]


def test_slack_job_end_to_end(server: SidecarServer) -> None:
    slack = FakeSlack()
    slack.history = [slack_message("1.000001")]
    job = server.sidecar.submit(
        json.dumps({"operation": "channel_history", "channel": CHAN}).encode()
    )
    LinkRuntime(slack_link(slack), server.stub).run_once()
    assert job.state == S.JOB_STATE_COMPLETED
    assert json.loads(job.files[RESULT_FILE])["source"] == "slack"
