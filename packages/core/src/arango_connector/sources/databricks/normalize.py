"""Pure mapping from Delta table rows to Envelopes. No I/O."""

from __future__ import annotations

import base64
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from arango_connector.envelope import Envelope, Scope, make_key
from arango_connector.keys import key_part
from arango_connector.payloads import RowPayload
from arango_connector.sources.databricks.config import TableSpec

SOURCE = "databricks"
# ``raw._change_type`` for rows read from a snapshot rather than the change feed.
SNAPSHOT = "snapshot"


def to_json_safe(value: Any) -> Any:
    """Convert a row value to plain JSON: decimals as lossless strings, datetimes as UTC
    ISO 8601, bytes as base64, NaN and infinities as ``None``. Raises ``TypeError`` on
    anything else."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, list | tuple):
        return [to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    raise TypeError(f"unsupported column value type: {type(value).__name__}")


def normalize_row(
    row: dict[str, Any],
    table: TableSpec,
    workspace: str,
    change_type: str,
    commit_version: int,
    commit_timestamp: datetime,
) -> Envelope[RowPayload] | None:
    """Map one row to an Envelope, or ``None`` if a primary key column is missing, null or
    not usable as a key (see ``key_part``)."""
    columns = {str(k): to_json_safe(v) for k, v in row.items()}
    primary_key = {c: columns.get(c) for c in table.primary_key}
    if any(v is None for v in primary_key.values()):
        return None
    try:
        source_id = key_part(list(primary_key.values()))
        key = make_key(SOURCE, table.name, source_id)
    except ValueError:
        return None
    return Envelope[RowPayload](
        key=key,
        source=SOURCE,
        source_id=source_id,
        scope=Scope(
            tenant_id=workspace, container_id=table.catalog, path=[table.schema, table.table]
        ),
        created_at=None,
        updated_at=commit_timestamp,
        payload=RowPayload(table=table.name, primary_key=primary_key, columns=columns),
        raw={
            "_change_type": change_type,
            "_commit_version": commit_version,
            "_commit_timestamp": to_json_safe(commit_timestamp),
        },
    )
