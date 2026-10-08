"""Adapter tests: incremental sync against an in-memory Jira and state store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arango_connector.envelope import Envelope
from arango_connector.sources.jira.adapter import OVERLAP, STATE_NAMESPACE, JiraPollingAdapter
from arango_connector.sources.jira.client import JiraClient, JiraInterrupted
from arango_connector.state import InMemoryStateStore
from jira_fakes import BASE_URL, EMBEDDED_COMMENTS, SITE, FakeJiraClient, comment, issue

NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def make_adapter(
    jira: FakeJiraClient,
    projects: tuple[str, ...] = ("PROJ",),
    state: InMemoryStateStore | None = None,
    clock: Clock | None = None,
    **kw: Any,
) -> tuple[JiraPollingAdapter, InMemoryStateStore]:
    state = state or InMemoryStateStore()

    def factory(_should_stop: Callable[[], bool]) -> JiraClient:
        return jira

    adapter = JiraPollingAdapter(
        list(projects),
        factory,
        state,
        site=SITE,
        base_url=BASE_URL,
        clock=clock or Clock(),
        **kw,
    )
    return adapter, state


def poll(adapter: JiraPollingAdapter) -> list[Envelope[Any]]:
    emitted: list[Envelope[Any]] = []
    adapter.poll_once(emitted.append)
    return emitted


def keys(envelopes: list[Envelope[Any]]) -> list[str]:
    return [f"{e.kind}:{e.source_id}" for e in envelopes]


def cursor(state: InMemoryStateStore, project: str = "PROJ") -> dict[str, Any] | None:
    return state.get(STATE_NAMESPACE, f"{SITE}:{project}")


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def seeded() -> FakeJiraClient:
    """Three PROJ issues of different ages and one in another project."""
    jira = FakeJiraClient()
    jira.put_issue(issue("1", "PROJ-1", created=ago(days=400), updated=ago(days=90)))
    jira.put_issue(issue("2", "PROJ-2", created=ago(days=400), updated=ago(days=10)))
    jira.put_issue(issue("3", "PROJ-3", created=ago(days=5), updated=ago(days=1)))
    jira.put_issue(issue("9", "OTHER-1", project=("10200", "OTHER"), updated=ago(days=1)))
    jira.add_comment("2", comment("c-old", created=ago(days=200)))
    jira.touch("2", ago(days=10))  # the comment predates the issue's last update
    return jira


# -- first start -----------------------------------------------------------------


def test_first_start_lookback_reads_recent_issues_with_all_their_comments() -> None:
    jira = seeded()
    adapter, state = make_adapter(jira, initial_lookback=timedelta(days=30))

    emitted = poll(adapter)

    # Old comment on a recently updated issue is still read on the first sync.
    assert keys(emitted) == ["issue:2", "comment:c-old", "issue:3"]
    assert jira.searches == [("PROJ", NOW - timedelta(days=30))]
    stored = cursor(state)
    assert stored == {"updated_since": NOW.isoformat(), "last_synced_at": NOW.isoformat()}


def test_first_start_all_reads_every_issue() -> None:
    jira = seeded()
    adapter, _ = make_adapter(jira, initial_sync="all")

    assert keys(poll(adapter)) == ["issue:1", "issue:2", "comment:c-old", "issue:3"]
    assert jira.searches == [("PROJ", None)]


def test_first_start_now_only_records_a_cursor_then_picks_up_changes() -> None:
    jira = seeded()
    clock = Clock()
    adapter, state = make_adapter(jira, initial_sync="now", clock=clock)

    assert poll(adapter) == []
    assert jira.searches == []
    assert cursor(state) == {"updated_since": NOW.isoformat(), "last_synced_at": NOW.isoformat()}

    jira.touch("1", clock.advance(minutes=1), summary="Reopened")
    clock.advance(minutes=1)
    emitted = poll(adapter)
    assert keys(emitted) == ["issue:1"]
    assert emitted[0].payload.summary == "Reopened"


# -- incremental sync --------------------------------------------------------------


def test_new_comment_and_edit_are_picked_up_without_re_emitting_old_comments() -> None:
    jira = seeded()
    clock = Clock()
    adapter, state = make_adapter(jira, clock=clock)
    poll(adapter)

    jira.add_comment("2", comment("c-new", "Fixed in 1.2", created=clock.advance(minutes=3)))
    clock.advance(minutes=1)
    emitted = poll(adapter)
    assert keys(emitted) == ["issue:2", "comment:c-new"]
    assert emitted[1].payload.text == "Fixed in 1.2"

    jira.edit_comment("2", "c-new", "Fixed in 1.3", clock.advance(minutes=5))
    clock.advance(minutes=1)
    emitted = poll(adapter)
    assert keys(emitted) == ["issue:2", "comment:c-new"]
    assert emitted[1].payload.text == "Fixed in 1.3"
    assert emitted[1].updated_at == clock.now - timedelta(minutes=1)
    stored = cursor(state)
    assert stored is not None and stored["updated_since"] == clock.now.isoformat()


def test_search_re_reads_an_overlap_before_the_cursor() -> None:
    jira = seeded()
    clock = Clock()
    adapter, _ = make_adapter(jira, clock=clock)
    poll(adapter)
    clock.advance(minutes=10)

    poll(adapter)

    assert jira.searches[-1] == ("PROJ", NOW - OVERLAP)


def test_late_indexed_update_within_the_overlap_is_not_missed() -> None:
    jira = seeded()
    clock = Clock()
    adapter, _ = make_adapter(jira, clock=clock)
    poll(adapter)

    # Updated 30s before the last sync started, but only searchable afterwards.
    jira.touch("1", NOW - timedelta(seconds=30), summary="Late")
    clock.advance(minutes=1)
    emitted = poll(adapter)
    assert keys(emitted) == ["issue:1"]


def test_idle_project_stops_re_emitting_once_past_the_overlap() -> None:
    jira = seeded()
    clock = Clock()
    adapter, _ = make_adapter(jira, clock=clock)
    poll(adapter)
    clock.advance(minutes=10)
    assert poll(adapter) == []


def test_re_reading_emits_the_same_keys() -> None:
    jira = seeded()
    first, _ = make_adapter(jira, initial_sync="all")
    again, _ = make_adapter(jira, initial_sync="all")  # a fresh cursor: full re-sync
    one = poll(first)
    two = poll(again)
    assert [e.key for e in one] == [e.key for e in two]
    assert len({e.key for e in one}) == len(one)


def test_truncated_comments_are_fetched_in_full() -> None:
    jira = FakeJiraClient()
    jira.put_issue(issue("5", "PROJ-5", updated=ago(hours=1)))
    for n in range(EMBEDDED_COMMENTS + 2):
        jira.add_comment("5", comment(f"c{n}", created=ago(minutes=50 - n)))
    adapter, _ = make_adapter(jira)

    emitted = poll(adapter)

    assert jira.comment_fetches == ["5"]
    assert [e.source_id for e in emitted if e.kind == "comment"] == [
        f"c{n}" for n in range(EMBEDDED_COMMENTS + 2)
    ]


def test_embedded_comments_need_no_extra_request() -> None:
    jira = FakeJiraClient()
    jira.put_issue(issue("5", "PROJ-5", updated=ago(hours=1)))
    jira.add_comment("5", comment("c1", created=ago(minutes=10)))
    adapter, _ = make_adapter(jira)
    poll(adapter)
    assert jira.comment_fetches == []


# -- checkpoints, stopping and failures --------------------------------------------


def test_cursor_is_checkpointed_after_each_page() -> None:
    jira = seeded()  # the fake pages two issues at a time
    adapter, state = make_adapter(jira, initial_sync="all")
    saved: list[dict[str, Any]] = []
    original_put = state.put

    def recording_put(namespace: str, key: str, value: dict[str, Any]) -> None:
        saved.append(value)
        original_put(namespace, key, value)

    state.put = recording_put  # type: ignore[method-assign]
    poll(adapter)

    assert [v["updated_since"] for v in saved] == [
        ago(days=10).isoformat(),  # after page [PROJ-1, PROJ-2]
        ago(days=1).isoformat(),  # after page [PROJ-3]
        NOW.isoformat(),  # sync complete
    ]
    assert [v.get("initial", False) for v in saved] == [True, True, False]


def test_stop_mid_sync_keeps_the_last_checkpoint_and_resumes_with_all_comments() -> None:
    jira = seeded()
    jira.add_comment("3", comment("c3", created=ago(days=3)))
    jira.touch("3", ago(days=1))
    adapter, state = make_adapter(jira, initial_sync="all")
    emitted: list[Envelope[Any]] = []

    def emit(envelope: Envelope[Any]) -> None:
        emitted.append(envelope)
        if envelope.source_id == "2":  # last issue of the first page
            adapter.stop()

    adapter.poll_once(emit)
    stored = cursor(state)
    assert stored is not None
    assert stored == {
        "updated_since": ago(days=10).isoformat(),
        "last_synced_at": NOW.isoformat(),
        "initial": True,
    }

    # A restart resumes the initial sync: PROJ-3's older comment is still read.
    resumed, _ = make_adapter(jira, initial_sync="all", state=state)
    assert keys(poll(resumed)) == ["issue:2", "comment:c-old", "issue:3", "comment:c3"]
    final = cursor(state)
    assert final is not None and "initial" not in final


def test_interrupted_rate_limit_wait_leaves_the_cursor(caplog: pytest.LogCaptureFixture) -> None:
    jira = seeded()

    def interrupted(*_: Any) -> Any:
        raise JiraInterrupted("/rest/api/3/search/jql")

    jira.search_issues = interrupted  # type: ignore[method-assign]
    adapter, state = make_adapter(jira)
    with caplog.at_level(logging.INFO):
        poll(adapter)  # must not raise
    assert cursor(state) is None
    assert "jira_sync_interrupted" in caplog.text


def test_one_failing_project_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    jira = seeded()
    jira.failing_projects.add("PROJ")
    adapter, state = make_adapter(jira, projects=("PROJ", "OTHER"))

    emitted = poll(adapter)  # must not raise

    assert keys(emitted) == ["issue:9"]
    assert cursor(state, "PROJ") is None
    assert cursor(state, "OTHER") is not None
    assert "jira_project_sync_failed project=PROJ" in caplog.text


def test_all_projects_failing_raises_so_the_poller_backs_off() -> None:
    jira = seeded()
    jira.failing_projects.update({"PROJ", "OTHER"})
    adapter, _ = make_adapter(jira, projects=("PROJ", "OTHER"))
    with pytest.raises(RuntimeError, match="all 2 Jira project"):
        poll(adapter)


def test_client_is_built_once_with_the_stop_flag() -> None:
    jira = seeded()
    flags: list[Callable[[], bool]] = []

    def factory(should_stop: Callable[[], bool]) -> JiraClient:
        flags.append(should_stop)
        return jira

    adapter = JiraPollingAdapter(
        ["PROJ"], factory, InMemoryStateStore(), site=SITE, base_url=BASE_URL, clock=Clock()
    )
    poll(adapter)
    poll(adapter)
    assert len(flags) == 1
    assert flags[0]() is False
    adapter.stop()
    assert flags[0]() is True


def test_unreadable_cursor_fails_the_project() -> None:
    jira = seeded()
    state = InMemoryStateStore()
    state.put(STATE_NAMESPACE, f"{SITE}:PROJ", {"updated_since": "garbage"})
    adapter, _ = make_adapter(jira, state=state)
    with pytest.raises(RuntimeError):
        poll(adapter)
