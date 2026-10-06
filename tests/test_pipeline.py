"""Pipeline tests using a fake source and fake sinks."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector.pipeline import Pipeline
from arango_connector.retention import RetentionPolicy
from arango_connector.sinks.base import Sink
from arango_connector.sources.base import EmitFn, SourceAdapter
from arango_connector.sources.slack.normalize import normalize
from arango_connector.state import InMemoryStateStore

LoadEvent = Callable[[str], dict[str, Any]]


class FakeSource(SourceAdapter):
    """Emits a fixed list of envelopes, then returns."""

    name = "fake"

    def __init__(self, envelopes: list[Envelope[Any]]) -> None:
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
        self.writes: list[list[Envelope[Any]]] = []
        self.closed = False

    def setup(self) -> None:
        self.setup_calls += 1

    def write(self, envelopes: list[Envelope[Any]]) -> None:
        self.writes.append(list(envelopes))

    def close(self) -> None:
        self.closed = True


class FailingSink(RecordingSink):
    """Raises on every write, simulating exhausted retries."""

    def write(self, envelopes: list[Envelope[Any]]) -> None:
        raise ConnectionError("arango unreachable")


def _envelopes(load_event: LoadEvent) -> list[Envelope[Any]]:
    envs = [normalize(load_event(n), "T0TESTTEAM") for n in ("user_message", "thread_reply")]
    return [e for e in envs if e is not None]


def test_writes_are_forwarded(load_event: LoadEvent, tmp_path: Path) -> None:
    envelopes = _envelopes(load_event)
    sink = RecordingSink()
    pipeline = Pipeline(FakeSource(envelopes), sink, dead_letter_path=tmp_path / "dl.jsonl")

    pipeline.run()

    assert sink.setup_calls == 1
    assert [[r.key for r in batch] for batch in sink.writes] == [
        [envelopes[0].key],
        [envelopes[1].key],
    ]
    assert not (tmp_path / "dl.jsonl").exists()


def test_expires_at_is_stamped_from_created_at(load_event: LoadEvent, tmp_path: Path) -> None:
    envelopes = _envelopes(load_event)
    sink = RecordingSink()
    pipeline = Pipeline(
        FakeSource(envelopes), sink, tmp_path / "dl.jsonl", retention=RetentionPolicy(30)
    )

    pipeline.run()

    written = [batch[0] for batch in sink.writes]
    for original, stamped in zip(envelopes, written, strict=True):
        assert original.created_at is not None
        assert stamped.expires_at == original.created_at + timedelta(days=30)
    pipeline.shutdown()


def test_age_mode_does_not_stamp_expires_at(load_event: LoadEvent, tmp_path: Path) -> None:
    sink = RecordingSink()
    policy = RetentionPolicy(30, mode="age")
    pipeline = Pipeline(FakeSource(_envelopes(load_event)), sink, tmp_path / "dl.jsonl", policy)

    pipeline.run()

    assert all(batch[0].expires_at is None for batch in sink.writes)
    pipeline.shutdown()


def test_no_expires_at_when_retention_disabled(load_event: LoadEvent, tmp_path: Path) -> None:
    sink = RecordingSink()
    pipeline = Pipeline(FakeSource(_envelopes(load_event)), sink, tmp_path / "dl.jsonl")

    pipeline.run()

    assert all(batch[0].expires_at is None for batch in sink.writes)


class RecordingState(InMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.setup_calls = 0
        self.closed = False

    def setup(self) -> None:
        self.setup_calls += 1

    def close(self) -> None:
        self.closed = True


def test_state_store_is_set_up_and_closed(tmp_path: Path) -> None:
    state = RecordingState()
    pipeline = Pipeline(FakeSource([]), RecordingSink(), tmp_path / "dl.jsonl", state=state)

    pipeline.run()
    pipeline.shutdown()

    assert state.setup_calls == 1
    assert state.closed


def test_sink_failure_goes_to_dead_letter_and_does_not_raise(
    load_event: LoadEvent, tmp_path: Path
) -> None:
    envelopes = _envelopes(load_event)
    dead_letter = tmp_path / "nested" / "dl.jsonl"
    pipeline = Pipeline(FakeSource(envelopes), FailingSink(), dead_letter_path=dead_letter)

    pipeline.run()  # must not raise

    lines = dead_letter.read_text().splitlines()
    assert [json.loads(line)["_key"] for line in lines] == [r.key for r in envelopes]
    assert json.loads(lines[0])["payload"]["text"] == envelopes[0].payload.text


def test_dead_letter_failure_does_not_raise(load_event: LoadEvent, tmp_path: Path) -> None:
    # A directory where the file should be makes the dead-letter write itself fail.
    dead_letter = tmp_path / "dl.jsonl"
    dead_letter.mkdir()
    pipeline = Pipeline(FakeSource(_envelopes(load_event)), FailingSink(), dead_letter)

    pipeline.run()  # must still not raise


def _aged_line(env: Envelope[Any], days_ago: float) -> str:
    """One dead-letter line for ``env``, as if the message was posted N days ago."""
    doc = env.to_document()
    created_at = datetime.now(UTC) - timedelta(days=days_ago)
    doc["created_at"] = created_at.isoformat().replace("+00:00", "Z")
    return json.dumps(doc)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _keys(path: Path) -> list[str]:
    return [json.loads(line)["_key"] for line in path.read_text().splitlines()]


def test_prune_drops_entries_past_retention(load_event: LoadEvent, tmp_path: Path) -> None:
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    _write_lines(dead_letter, [_aged_line(old, 40), _aged_line(recent, 2)])
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(30))

    assert pipeline.prune_dead_letter() == 1

    assert _keys(dead_letter) == [recent.key]
    assert not (tmp_path / "dl.jsonl.tmp").exists()


def test_prune_is_a_no_op_when_retention_disabled(load_event: LoadEvent, tmp_path: Path) -> None:
    old, _ = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    _write_lines(dead_letter, [_aged_line(old, 400)])
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(0))

    assert pipeline.prune_dead_letter() == 0
    assert _keys(dead_letter) == [old.key]


def test_prune_leaves_file_untouched_when_nothing_expires(
    load_event: LoadEvent, tmp_path: Path
) -> None:
    _, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    _write_lines(dead_letter, [_aged_line(recent, 1)])
    before = dead_letter.read_text()
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(30))

    assert pipeline.prune_dead_letter() == 0
    assert dead_letter.read_text() == before


def test_prune_keeps_unreadable_lines(load_event: LoadEvent, tmp_path: Path) -> None:
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    _write_lines(
        dead_letter,
        [_aged_line(old, 40), "{truncated half-written line", _aged_line(recent, 1)],
    )
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(30))

    assert pipeline.prune_dead_letter() == 1

    lines = dead_letter.read_text().splitlines()
    assert lines[0] == "{truncated half-written line"
    assert json.loads(lines[1])["_key"] == recent.key


def test_prune_handles_missing_file(tmp_path: Path) -> None:
    pipeline = Pipeline(
        FakeSource([]), RecordingSink(), tmp_path / "absent.jsonl", retention=RetentionPolicy(30)
    )
    assert pipeline.prune_dead_letter() == 0


def test_run_prunes_at_startup_and_shutdown_stops_the_thread(
    load_event: LoadEvent, tmp_path: Path
) -> None:
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    _write_lines(dead_letter, [_aged_line(old, 40), _aged_line(recent, 1)])
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(30))

    pipeline.run()
    assert _keys(dead_letter) == [recent.key]

    pipeline.shutdown()
    assert pipeline._prune_thread is not None
    assert not pipeline._prune_thread.is_alive()


def test_no_prune_thread_when_retention_disabled(tmp_path: Path) -> None:
    pipeline = Pipeline(
        FakeSource([]), RecordingSink(), tmp_path / "dl.jsonl", retention=RetentionPolicy(0)
    )
    pipeline.run()
    pipeline.shutdown()
    assert pipeline._prune_thread is None


def test_shutdown_stops_source_then_closes_sink_once(tmp_path: Path) -> None:
    source = FakeSource([])
    sink = RecordingSink()
    pipeline = Pipeline(source, sink, dead_letter_path=tmp_path / "dl.jsonl")

    pipeline.shutdown()
    pipeline.shutdown()

    assert source.stopped
    assert sink.closed


def test_prune_falls_back_to_ingested_at(load_event: LoadEvent, tmp_path: Path) -> None:
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    stale = old.to_document() | {
        "created_at": None,
        "ingested_at": (datetime.now(UTC) - timedelta(days=40)).isoformat(),
    }
    _write_lines(dead_letter, [json.dumps(stale), _aged_line(recent, 1)])
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, retention=RetentionPolicy(30))

    assert pipeline.prune_dead_letter() == 1
    assert _keys(dead_letter) == [recent.key]


def test_prune_honours_a_stored_expires_at(load_event: LoadEvent, tmp_path: Path) -> None:
    """In expiry mode the deadline written with the line wins over the current window."""
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    kept_by_deadline = json.loads(_aged_line(old, 40)) | {"expires_at": future}
    _write_lines(dead_letter, [json.dumps(kept_by_deadline), _aged_line(recent, 40)])
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, RetentionPolicy(30))

    assert pipeline.prune_dead_letter() == 1
    assert _keys(dead_letter) == [old.key]


def test_prune_in_age_mode_uses_the_age_field(load_event: LoadEvent, tmp_path: Path) -> None:
    old, recent = _envelopes(load_event)
    dead_letter = tmp_path / "dl.jsonl"
    long_ago = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    # Posted recently but ingested long ago: only ingested_at should count.
    stale = json.loads(_aged_line(old, 1)) | {"ingested_at": long_ago}
    fresh = json.loads(_aged_line(recent, 40)) | {"ingested_at": datetime.now(UTC).isoformat()}
    _write_lines(dead_letter, [json.dumps(stale), json.dumps(fresh)])
    policy = RetentionPolicy(30, mode="age", age_field="ingested_at")
    pipeline = Pipeline(FakeSource([]), RecordingSink(), dead_letter, policy)

    assert pipeline.prune_dead_letter() == 1
    assert _keys(dead_letter) == [recent.key]
