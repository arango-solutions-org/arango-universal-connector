"""In-memory state store, for tests and throwaway runs. Nothing survives a restart."""

from __future__ import annotations

import copy
import threading
from typing import Any

from arango_connector_standalone.state.base import StateStore


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get((namespace, key))
            return copy.deepcopy(value) if value is not None else None

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[(namespace, key)] = copy.deepcopy(value)
