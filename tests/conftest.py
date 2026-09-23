"""Shared test helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


@pytest.fixture
def load_event() -> Callable[[str], dict[str, Any]]:
    """Return a loader for Slack event fixtures by basename."""
    return _load
