"""Base interface for all message sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from arango_connector.envelope import Envelope

EmitFn = Callable[[Envelope], None]


class SourceAdapter(ABC):
    """A source of messages (Slack, Discord, ...) that emits normalized Envelopes."""

    name: str

    @abstractmethod
    def start(self, emit: EmitFn) -> None:
        """Start listening and call ``emit`` for every new Envelope. Blocks until stopped."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and release resources. Must be safe to call more than once."""
