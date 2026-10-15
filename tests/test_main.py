"""The entrypoint: a signal stops the source, and the sink stays open until it has."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from arango_connector.envelope import Envelope
from arango_connector.sources.slack.normalize import normalize
from arango_connector_standalone import main as main_module
from arango_connector_standalone.sinks.base import Sink
from arango_connector_standalone.sources.base import EmitFn, SourceAdapter
from arango_connector_standalone.state import InMemoryStateStore


class ClosableSink(Sink):
    def __init__(self) -> None:
        self.written: list[Envelope[Any]] = []
        self.closed = False

    def setup(self) -> None:
        pass

    def write(self, envelopes: list[Envelope[Any]]) -> None:
        if self.closed:
            raise RuntimeError("sink closed")
        self.written.extend(envelopes)

    def close(self) -> None:
        self.closed = True


class SignalledSource(SourceAdapter):
    """Receives SIGTERM mid-sync, then emits the item it was already handling."""

    name = "fake"

    def __init__(self, envelope: Envelope[Any]) -> None:
        self.envelope = envelope
        self.stopped = False

    def start(self, emit: EmitFn) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        emit(self.envelope)

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def restore_process_state() -> Iterator[None]:
    """main() installs signal handlers and sets the umask for the whole process."""
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    umask = os.umask(0o022)
    os.umask(umask)
    yield
    for sig, handler in handlers.items():
        signal.signal(sig, handler)
    os.umask(umask)


def test_signal_stops_the_source_without_closing_the_sink_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    load_event: Callable[[str], dict[str, Any]],
    restore_process_state: None,
) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env
    monkeypatch.setenv("ARANGO_PASSWORD", "pw")
    monkeypatch.setenv("RETENTION_DAYS", "0")
    dead_letter = tmp_path / "dl.jsonl"
    monkeypatch.setenv("DEAD_LETTER_PATH", str(dead_letter))

    envelope = normalize(load_event("user_message"), "T0TESTTEAM")
    assert envelope is not None
    source = SignalledSource(envelope)
    sink = ClosableSink()
    monkeypatch.setattr(main_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(main_module, "build_state_store", lambda _s: InMemoryStateStore())
    monkeypatch.setattr(main_module, "build_source", lambda _s, _state: source)
    monkeypatch.setattr(main_module, "build_sink", lambda _s: sink)

    assert main_module.main() == 0

    assert source.stopped
    assert sink.written == [envelope]
    assert sink.closed
    assert not dead_letter.exists()
