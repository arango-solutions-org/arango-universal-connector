"""Base interface for all sinks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from arango_connector.record import Record


class Sink(ABC):
    """A destination that persists Records."""

    @abstractmethod
    def setup(self) -> None:
        """Ensure the destination is ready (databases, collections, indexes). Idempotent."""

    @abstractmethod
    def write(self, records: list[Record[Any]]) -> None:
        """Persist records. Takes a list so batching can be added later.

        Raises on failure after any internal retries have been exhausted.
        """

    @abstractmethod
    def close(self) -> None:
        """Release connections and other resources."""
