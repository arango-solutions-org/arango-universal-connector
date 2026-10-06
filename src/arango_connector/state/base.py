"""Base interface for source state (cursors, checkpoints)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StateStore(ABC):
    """Durable key/value storage for source state, such as a sync cursor.

    Kept separate from ``Sink`` so sinks only ever write envelopes. Values are small JSON
    objects; ``namespace`` is the source name and ``key`` identifies the cursor within it
    (for example one per mailbox).
    """

    def setup(self) -> None:  # noqa: B027 - optional hook
        """Prepare the store. Idempotent."""

    @abstractmethod
    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        """Return the stored value, or ``None`` if nothing is stored yet."""

    @abstractmethod
    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        """Store ``value``, replacing any previous one. Raises on final failure."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""
