"""Tests for the PollingSourceAdapter base loop."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from arango_connector.payloads import MessagePayload
from arango_connector.record import Record, Scope
from arango_connector.sources.base import EmitFn
from arango_connector.sources.polling import PollingSourceAdapter


def _record(n: int) -> Record[MessagePayload]:
    return Record[MessagePayload](
        key=f"fake_{n}",
        source="fake",
        source_id=str(n),
        scope=Scope(container_id="c"),
        created_at=None,
        payload=MessagePayload(author_type="system", text=str(n)),
        raw={},
    )


class ScriptedPoller(PollingSourceAdapter):
    """Runs a script of poll outcomes, then stops itself."""

    name = "fake"

    def __init__(self, script: list[str], interval: float = 0.01, **kw: Any) -> None:
        super().__init__(interval, **kw)
        self.script = list(script)
        self.polls = 0

    def poll_once(self, emit: EmitFn) -> None:
        self.polls += 1
        outcome = self.script.pop(0)
        if not self.script:
            self.stop()
        if outcome == "fail":
            raise RuntimeError("api down")
        emit(_record(self.polls))


def test_polls_until_stopped_and_emits() -> None:
    emitted: list[Record[Any]] = []
    poller = ScriptedPoller(["ok", "ok", "ok"])

    poller.start(emitted.append)

    assert poller.polls == 3
    assert [r.key for r in emitted] == ["fake_1", "fake_2", "fake_3"]


def test_failed_poll_does_not_stop_the_loop() -> None:
    emitted: list[Record[Any]] = []
    poller = ScriptedPoller(["fail", "fail", "ok"])

    poller.start(emitted.append)  # must not raise

    assert poller.polls == 3
    assert [r.key for r in emitted] == ["fake_3"]


def test_backoff_grows_and_is_capped_then_resets() -> None:
    poller = ScriptedPoller(["ok"], interval=10, max_backoff_seconds=35)

    assert poller._next_delay() == 10
    delays = []
    for _ in range(4):
        poller._consecutive_failures += 1
        delays.append(poller._next_delay())
    assert delays == [10, 20, 35, 35]

    poller._consecutive_failures = 0
    assert poller._next_delay() == 10


def test_stop_interrupts_a_long_wait() -> None:
    poller = ScriptedPoller(["ok", "ok"], interval=3600)
    thread = threading.Thread(target=poller.start, args=(lambda _: None,))
    thread.start()

    poller.stop()
    poller.stop()  # idempotent
    thread.join(timeout=5)

    assert not thread.is_alive()


def test_interval_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ScriptedPoller(["ok"], interval=0)
