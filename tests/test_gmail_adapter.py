"""Adapter tests: incremental sync against an in-memory Gmail and state store."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arango_connector.envelope import Envelope
from arango_connector.sources.gmail.client import GmailClient
from arango_connector.sources.gmail.normalize import NormalizeOptions, mailbox_key
from arango_connector_standalone.sources.gmail.adapter import (
    GAP_OVERLAP,
    STATE_NAMESPACE,
    GmailPollingAdapter,
    LabelFilter,
    resolve_labels,
)
from arango_connector_standalone.state import InMemoryStateStore
from gmail_fakes import FakeGmailClient, message

ADA = "ada@example.com"
BOB = "bob@example.com"
NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_adapter(
    clients: dict[str, Any],
    state: InMemoryStateStore | None = None,
    clock: Clock | None = None,
    **kw: Any,
) -> tuple[GmailPollingAdapter, InMemoryStateStore]:
    state = state or InMemoryStateStore()
    defaults: dict[str, Any] = {
        "include_labels": ["INBOX", "SENT"],
        "exclude_labels": ["SPAM", "TRASH", "DRAFT"],
        "options": NormalizeOptions(key_mode="plain"),
    }

    def factory(mailbox: str) -> GmailClient:
        client = clients[mailbox]
        if isinstance(client, Exception):
            raise client
        result: GmailClient = client
        return result

    adapter = GmailPollingAdapter(
        list(clients), factory, state, clock=clock or Clock(), **(defaults | kw)
    )
    return adapter, state


def poll(adapter: GmailPollingAdapter) -> list[Envelope[Any]]:
    emitted: list[Envelope[Any]] = []
    adapter.poll_once(emitted.append)
    return emitted


def ids(envelopes: list[Envelope[Any]]) -> list[str]:
    return [e.source_id for e in envelopes]


def received(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


# -- first start ---------------------------------------------------------------


def test_first_start_now_ingests_nothing_old_then_picks_up_new_mail() -> None:
    gmail = FakeGmailClient()
    gmail.deliver(message("old", received=received(1)))
    adapter, state = make_adapter({ADA: gmail})

    assert poll(adapter) == []
    assert state.get(STATE_NAMESPACE, ADA) == {
        "history_id": str(gmail.history_id),
        "last_synced_at": NOW.isoformat(),
    }

    gmail.deliver(message("new"))
    assert ids(poll(adapter)) == ["new"]


def test_first_start_lookback_ingests_the_window_only() -> None:
    gmail = FakeGmailClient()
    gmail.deliver(message("recent", received=received(0.5)))
    gmail.deliver(message("too-old", received=received(3)))
    adapter, _ = make_adapter(
        {ADA: gmail}, initial_sync="lookback", initial_lookback=timedelta(days=1)
    )

    assert ids(poll(adapter)) == ["recent"]
    assert gmail.queries == [f"after:{int((NOW - timedelta(days=1)).timestamp())}"]
    # History from before the cursor is not replayed on the next poll.
    assert poll(adapter) == []


def test_restart_resumes_from_the_stored_cursor() -> None:
    gmail = FakeGmailClient()
    adapter, state = make_adapter({ADA: gmail})
    poll(adapter)
    gmail.deliver(message("while-down"))

    restarted, _ = make_adapter({ADA: gmail}, state=state)

    assert ids(poll(restarted)) == ["while-down"]


# -- incremental sync ------------------------------------------------------------


def test_history_is_paged_and_each_message_emitted_once() -> None:
    gmail = FakeGmailClient(page_size=2)
    adapter, _ = make_adapter({ADA: gmail})
    poll(adapter)
    for n in range(5):
        gmail.deliver(message(f"m{n}"))
    gmail.relabel("m1", add=("STARRED",))  # same message again in a later record

    assert ids(poll(adapter)) == ["m0", "m1", "m2", "m3", "m4"]
    assert poll(adapter) == []


def test_label_filter_includes_and_excludes() -> None:
    gmail = FakeGmailClient()
    adapter, _ = make_adapter({ADA: gmail})
    poll(adapter)
    gmail.deliver(message("inbox", labels=("INBOX",)))
    gmail.deliver(message("sent", labels=("SENT",)))
    gmail.deliver(message("spam", labels=("INBOX", "SPAM")))
    gmail.deliver(message("draft", labels=("DRAFT",)))
    gmail.deliver(message("archived", labels=("IMPORTANT",)))

    assert ids(poll(adapter)) == ["inbox", "sent"]


def test_message_gaining_a_watched_label_is_ingested_with_updated_at() -> None:
    gmail = FakeGmailClient()
    clock = Clock()
    adapter, _ = make_adapter({ADA: gmail}, clock=clock)
    poll(adapter)
    gmail.deliver(message("archived", labels=("IMPORTANT",)))
    assert poll(adapter) == []

    clock.now = NOW + timedelta(minutes=5)
    gmail.relabel("archived", add=("INBOX",))
    [env] = poll(adapter)

    assert env.source_id == "archived"
    assert env.payload.label_ids == ["IMPORTANT", "INBOX"]
    assert env.updated_at == clock.now


def test_label_change_rewrites_current_labels() -> None:
    gmail = FakeGmailClient()
    adapter, _ = make_adapter({ADA: gmail})
    poll(adapter)
    gmail.deliver(message("m", labels=("INBOX", "UNREAD")))
    [first] = poll(adapter)
    assert first.updated_at is None

    gmail.relabel("m", remove=("UNREAD",))
    [second] = poll(adapter)

    assert second.key == first.key
    assert second.payload.label_ids == ["INBOX"]


def test_message_leaving_watched_labels_is_not_re_emitted() -> None:
    gmail = FakeGmailClient()
    adapter, _ = make_adapter({ADA: gmail})
    poll(adapter)
    gmail.deliver(message("m"))
    poll(adapter)

    gmail.relabel("m", add=("TRASH",))
    assert poll(adapter) == []  # deletes are out of scope; nothing is written


def test_deleted_message_is_skipped() -> None:
    gmail = FakeGmailClient()
    adapter, _ = make_adapter({ADA: gmail})
    poll(adapter)
    gmail.deliver(message("gone"))
    gmail.deliver(message("kept"))
    gmail.delete("gone")

    assert ids(poll(adapter)) == ["kept"]


# -- labels ------------------------------------------------------------------------


def test_labels_can_be_configured_by_name() -> None:
    gmail = FakeGmailClient(user_labels={"Label_7": "Receipts"})
    adapter, _ = make_adapter({ADA: gmail}, include_labels=["receipts"])
    poll(adapter)
    gmail.deliver(message("receipt", labels=("Label_7",)))
    gmail.deliver(message("inbox", labels=("INBOX",)))

    assert ids(poll(adapter)) == ["receipt"]


def test_unknown_labels_are_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    adapter, _ = make_adapter({ADA: FakeGmailClient()}, include_labels=["INBOX", "Nope"])
    with caplog.at_level(logging.WARNING):
        poll(adapter)
    assert "gmail_label_unknown mailbox=ada@example.com label=Nope" in caplog.text


def test_resolve_labels_matches_ids_and_names() -> None:
    available = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Work/Clients"}]
    resolved, unknown = resolve_labels(["INBOX", "work/clients", "Label_1", "x"], available)
    assert resolved == {"INBOX", "Label_1"}
    assert unknown == ["x"]


def test_label_filter() -> None:
    f = LabelFilter(frozenset({"INBOX"}), frozenset({"SPAM"}))
    assert f.matches(["INBOX", "UNREAD"])
    assert not f.matches(["INBOX", "SPAM"])
    assert not f.matches(["UNREAD"])


# -- recovery ----------------------------------------------------------------------


def test_expired_history_catches_up_by_date_and_resets_the_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gmail = FakeGmailClient()
    clock = Clock()
    adapter, state = make_adapter({ADA: gmail}, clock=clock)
    poll(adapter)  # cursor at NOW

    gmail.deliver(message("during-gap", received=NOW + timedelta(days=2)))
    gmail.deliver(message("before-cursor", received=NOW - timedelta(days=2)))
    gmail.expired_before = gmail.history_id + 1  # every stored cursor is now too old
    clock.now = NOW + timedelta(days=10)

    with caplog.at_level(logging.WARNING):
        assert ids(poll(adapter)) == ["during-gap"]

    assert "gmail_history_gap" in caplog.text
    assert gmail.queries == [f"after:{int((NOW - GAP_OVERLAP).timestamp())}"]
    cursor = state.get(STATE_NAMESPACE, ADA)
    assert cursor == {"history_id": str(gmail.history_id), "last_synced_at": clock.now.isoformat()}

    gmail.expired_before = None
    gmail.deliver(message("after-recovery"))
    assert ids(poll(adapter)) == ["after-recovery"]


def test_failed_fetch_leaves_the_cursor_so_the_next_poll_retries() -> None:
    gmail = FakeGmailClient()
    adapter, state = make_adapter({ADA: gmail, BOB: FakeGmailClient(BOB)})
    poll(adapter)
    before = state.get(STATE_NAMESPACE, ADA)
    gmail.deliver(message("a"))
    gmail.deliver(message("b"))
    gmail.fail_get.add("b")

    assert ids(poll(adapter)) == ["a"]  # "a" emitted, then the mailbox sync failed
    assert state.get(STATE_NAMESPACE, ADA) == before

    gmail.fail_get.clear()
    assert ids(poll(adapter)) == ["a", "b"]  # at-least-once: "a" again, same key


def test_one_failing_mailbox_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    bob = FakeGmailClient(BOB)
    adapter, _ = make_adapter({ADA: PermissionError("delegation not granted"), BOB: bob})
    poll(adapter)
    bob.deliver(message("for-bob"))

    with caplog.at_level(logging.ERROR):
        assert ids(poll(adapter)) == ["for-bob"]
    assert "gmail_mailbox_sync_failed mailbox=ada@example.com" in caplog.text


def test_all_mailboxes_failing_raises_so_the_poller_backs_off() -> None:
    adapter, _ = make_adapter({ADA: PermissionError("no"), BOB: PermissionError("no")})
    with pytest.raises(RuntimeError, match="all 2 Gmail mailbox"):
        poll(adapter)


def test_stopping_mid_sync_keeps_the_cursor() -> None:
    gmail = FakeGmailClient()
    adapter, state = make_adapter({ADA: gmail})
    poll(adapter)
    before = state.get(STATE_NAMESPACE, ADA)
    gmail.deliver(message("a"))
    gmail.deliver(message("b"))

    emitted: list[Envelope[Any]] = []

    def emit_then_stop(env: Envelope[Any]) -> None:
        emitted.append(env)
        adapter.stop()

    adapter.poll_once(emit_then_stop)

    assert ids(emitted) == ["a"]
    assert state.get(STATE_NAMESPACE, ADA) == before


# -- keys and privacy ------------------------------------------------------------------


def test_hashed_mode_keeps_the_address_out_of_keys_and_cursors() -> None:
    gmail = FakeGmailClient()
    adapter, state = make_adapter({ADA: gmail}, options=NormalizeOptions(key_mode="hashed"))
    poll(adapter)
    gmail.deliver(message("m"))

    [env] = poll(adapter)

    hashed = mailbox_key(ADA, "hashed")
    assert env.key == f"gmail_{hashed}_m"
    assert ADA not in env.key
    assert state.get(STATE_NAMESPACE, hashed) is not None
    assert state.get(STATE_NAMESPACE, ADA) is None


# -- thread matching -------------------------------------------------------------------

TEST = "Label_test"


def _thread_adapter(gmail: FakeGmailClient, **kw: Any) -> GmailPollingAdapter:
    adapter, _ = make_adapter({ADA: gmail}, include_labels=["test"], **kw)
    poll(adapter)  # initialise the cursor
    return adapter


def _gmail() -> FakeGmailClient:
    return FakeGmailClient(user_labels={TEST: "test"})


def test_reply_in_a_labelled_thread_is_ingested_in_thread_mode() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail)
    gmail.deliver(message("orig", labels=("INBOX", TEST)))
    assert ids(poll(adapter)) == ["orig"]

    gmail.deliver(message("reply", thread_id="orig", labels=("INBOX", "UNREAD")))
    [env] = poll(adapter)

    assert env.source_id == "reply"
    assert env.payload.thread_id == "orig"
    assert env.updated_at is None  # a new message, not a label change


def test_reply_is_skipped_in_message_mode() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail, label_match="message")
    gmail.deliver(message("orig", labels=("INBOX", TEST)))
    gmail.deliver(message("reply", thread_id="orig", labels=("INBOX",)))

    assert ids(poll(adapter)) == ["orig"]
    assert gmail.thread_gets == []  # no thread lookups in message mode


def test_excluded_reply_in_a_labelled_thread_is_still_skipped() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail)
    gmail.deliver(message("orig", labels=("INBOX", TEST)))
    gmail.deliver(message("spam-reply", thread_id="orig", labels=("SPAM",)))

    assert ids(poll(adapter)) == ["orig"]


def test_a_labelled_message_in_spam_does_not_qualify_its_thread() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail)
    gmail.deliver(message("bait", labels=("SPAM", TEST)))
    gmail.deliver(message("reply", thread_id="bait", labels=("INBOX",)))

    assert poll(adapter) == []


def test_labelling_one_message_pulls_in_its_whole_thread() -> None:
    gmail = _gmail()
    clock = Clock()
    adapter = _thread_adapter(gmail, clock=clock)
    gmail.deliver(message("first", labels=("INBOX",)))
    gmail.deliver(message("second", thread_id="first", labels=("INBOX",)))
    gmail.deliver(message("third", thread_id="first", labels=("SENT",)))
    assert poll(adapter) == []
    gmail.thread_gets.clear()

    clock.now = NOW + timedelta(minutes=1)
    gmail.relabel("third", add=(TEST,))
    envs = poll(adapter)

    assert sorted(ids(envs)) == ["first", "second", "third"]
    assert all(e.updated_at == clock.now for e in envs)
    assert gmail.thread_gets == ["first"]  # one lookup for the thread, cached for the poll


def test_read_toggle_on_a_labelled_message_does_not_re_read_the_thread() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail)
    gmail.deliver(message("orig", labels=("INBOX", "UNREAD", TEST)))
    gmail.deliver(message("reply", thread_id="orig", labels=("INBOX",)))
    poll(adapter)
    gmail.gets.clear()

    gmail.relabel("orig", remove=("UNREAD",))

    assert ids(poll(adapter)) == ["orig"]
    assert gmail.gets == ["orig"]


def test_unlabelled_threads_are_looked_up_once_per_poll() -> None:
    gmail = _gmail()
    adapter = _thread_adapter(gmail)
    gmail.deliver(message("a", labels=("INBOX",)))
    gmail.deliver(message("b", thread_id="a", labels=("INBOX",)))

    assert poll(adapter) == []
    assert gmail.thread_gets == ["a"]
