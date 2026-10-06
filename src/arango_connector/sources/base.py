"""Base interface for all sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from arango_connector.record import Record

EmitFn = Callable[[Record[Any]], None]


class SourceAdapter(ABC):
    """A source of items (Slack, Gmail, ...) that emits normalized Records."""

    name: str

    @abstractmethod
    def start(self, emit: EmitFn) -> None:
        """Start listening and call ``emit`` for every new Record. Blocks until stopped."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and release resources. Must be safe to call more than once."""
