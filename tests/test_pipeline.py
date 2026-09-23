"""Pipeline tests using a fake source and fake sinks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector.pipeline import Pipeline
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import EmitFn, SourceAdapter
from arango_connector.sources.slack.normalize import normalize

LoadEvent = Callable[[str], dict[str, Any]]


class FakeSource(SourceAdapter):
    """Emits a fixed list of envelopes, then returns."""

    name = "fake"

    def __init__(self, envelopes: list[Envelope]) -> None:
        self.envelopes = envelopes
        self.stopped = False

    def start(self, emit: EmitFn) -> None:
        for env in self.envelopes:
            emit(env)

    def stop(self) -> None:
        self.stopped = True


class RecordingSink(Sink):
    """Records every write."""

    def __init__(self) -> None:
        self.setup_calls = 0
        self.writes: list[list[Envelope]] = []
        self.closed = False

    def setup(self) -> None:
        self.setup_calls += 1

    def write(self, envelopes: list[Envelope]) -> None:
        self.writes.append(list(envelopes))

    def close(self) -> None:
        self.closed = True


class FailingSink(RecordingSink):
    """Raises on every write, simulating exhausted retries."""

    def write(self, envelopes: list[Envelope]) -> None:
        raise ConnectionError("arango unreachable")


def _envelopes(load_event: LoadEvent) -> list[Envelope]:
    envs = [normalize(load_event(n), "T0TESTTEAM") for n in ("user_message", "thread_reply")]
    return [e for e in envs if e is not None]


def test_writes_are_forwarded(load_event: LoadEvent, tmp_path: Path) -> None:
    envelopes = _envelopes(load_event)
    sink = RecordingSink()
    pipeline = Pipeline(FakeSource(envelopes), sink, dead_letter_path=tmp_path / "dl.jsonl")

    pipeline.run()

    assert sink.setup_calls == 1
    assert sink.writes == [[envelopes[0]], [envelopes[1]]]
    assert not (tmp_path / "dl.jsonl").exists()


def test_sink_failure_goes_to_dead_letter_and_does_not_raise(
    load_event: LoadEvent, tmp_path: Path
) -> None:
    envelopes = _envelopes(load_event)
    dead_letter = tmp_path / "nested" / "dl.jsonl"
    pipeline = Pipeline(FakeSource(envelopes), FailingSink(), dead_letter_path=dead_letter)

    pipeline.run()  # must not raise

    lines = dead_letter.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [e.to_document() for e in envelopes]


def test_dead_letter_failure_does_not_raise(load_event: LoadEvent, tmp_path: Path) -> None:
    # A directory where the file should be makes the dead-letter write itself fail.
    dead_letter = tmp_path / "dl.jsonl"
    dead_letter.mkdir()
    pipeline = Pipeline(FakeSource(_envelopes(load_event)), FailingSink(), dead_letter)

    pipeline.run()  # must still not raise


def test_shutdown_stops_source_then_closes_sink_once(tmp_path: Path) -> None:
    source = FakeSource([])
    sink = RecordingSink()
    pipeline = Pipeline(source, sink, dead_letter_path=tmp_path / "dl.jsonl")

    pipeline.shutdown()
    pipeline.shutdown()

    assert source.stopped
    assert sink.closed
