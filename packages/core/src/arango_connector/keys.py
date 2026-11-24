"""``key_part``: one key fragment from one or more key values, shared by sources and sinks.

The Databricks normalizer keys a row by its primary key and the graph projection keys
an edge endpoint by a foreign key. Both go through ``key_part``, so a primary key and a
foreign key holding the same value always produce the same fragment.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

_PLAIN_RE = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,128}$")
_HASH_LENGTH = 40


def _component(value: Any) -> str:
    # bool is an int subclass, so it must be rejected before the int branch.
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"key values must be int or str, got {type(value).__name__}")
    text = str(value)
    if not text:
        raise ValueError("key values must be non-empty")
    return text


def key_part(values: Sequence[Any]) -> str:
    """A deterministic, ArangoDB-safe key fragment for ``values``.

    Values are canonicalized (``1`` and ``"1"`` are the same), joined with ``:`` and used
    as is when the result is a short, plain key; otherwise (a component containing ``:``,
    characters ArangoDB rejects, or over 128 characters) the fragment is
    ``h-`` + 40 hex characters of a SHA-256 over the components.
    """
    if not values:
        raise ValueError("at least one key value is required")
    components = [_component(v) for v in values]
    if not any(":" in c for c in components):
        joined = ":".join(components)
        if _PLAIN_RE.match(joined):
            return joined
    digest = hashlib.sha256(json.dumps(components).encode("utf-8")).hexdigest()
    return "h-" + digest[:_HASH_LENGTH]
