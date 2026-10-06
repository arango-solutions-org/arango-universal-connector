"""Base interface for all sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from arango_connector.envelope import Envelope

EmitFn = Callable[[Envelope[Any]], None]


class SourceAdapter(ABC):
    """A source of items (Slack, Gmail, ...) that emits normalized Envelopes."""

    name: str

    @abstractmethod
    def start(self, emit: EmitFn) -> None:
        """Start listening and call ``emit`` for every new Envelope. Blocks until stopped."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and release resources. Must be safe to call more than once."""
