"""Operator commands that change stored data. Never run automatically.

``arango-connector-reconcile-expiry`` re-derives ``expires_at`` for stored documents
under the current retention window. In ``expiry`` mode deadlines are fixed at write
time (like MongoDB ``expireAt``, DynamoDB TTL or Cassandra per-write TTLs), so changing
``RETENTION_DAYS`` only affects new documents until this is run. Run it once after
upgrading from schema v3 too, or older documents never expire.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Any

from arango.database import StandardDatabase

from arango_connector.arango_common import connect
from arango_connector.config import ConfigError, CoreSettings, load_settings
from arango_connector.main import configure_logging
from arango_connector.retention import RetentionPolicy

logger = logging.getLogger("arango_connector.maintenance")

DEFAULT_BATCH_SIZE = 5000

# Walks the collection in primary-key order, one bounded batch per query, so each
# transaction and the memory it needs stay small however large the collection is. Only
# the fields needed to compute expiry are read, never the (possibly large) documents.
_BATCH_AQL = """
LET batch = (
    FOR d IN @@collection
        FILTER d._key > @after
        SORT d._key
        LIMIT @batch_size
        RETURN { _key: d._key, created_at: d.created_at, ingested_at: d.ingested_at,
                 expires_at: d.expires_at }
)
LET stale = (
    FOR d IN batch
        LET base = d.created_at != null ? d.created_at : d.ingested_at
        FILTER base != null
        LET expected = DATE_ADD(base, @days, "day")
        FILTER d.expires_at == null
            OR (!@only_missing AND DATE_TIMESTAMP(d.expires_at) != DATE_TIMESTAMP(expected))
        RETURN { _key: d._key, expires_at: DATE_ISO8601(expected) }
)
{update}
RETURN { last_key: LAST(batch)._key, scanned: LENGTH(batch), stale: LENGTH(stale) }
"""

_UPDATE_AQL = """
LET written = (
    FOR s IN stale
        UPDATE { _key: s._key } WITH { expires_at: s.expires_at } IN @@collection
        RETURN 1
)
"""


@dataclass
class ReconcileResult:
    scanned: int = 0
    stale: int = 0
    updated: int = 0
    batches: int = 0


def reconcile_expiry(
    db: StandardDatabase,
    collection: str,
    policy: RetentionPolicy,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    only_missing: bool = False,
) -> ReconcileResult:
    """Set ``expires_at`` = (created_at or ingested_at) + window where it differs.

    ``only_missing`` limits the pass to documents with no ``expires_at`` (the schema v3
    upgrade). ``dry_run`` counts what would change without writing.
    """
    if policy.mode != "expiry" or not policy.enabled:
        raise ValueError(
            "reconcile-expiry needs RETENTION_MODE=expiry and RETENTION_DAYS > 0 "
            f"(current: {policy.describe()})"
        )
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not db.has_collection(collection):
        raise LookupError(f"collection {collection!r} does not exist in database {db.name!r}")

    query = _BATCH_AQL.replace("{update}", "" if dry_run else _UPDATE_AQL)
    result = ReconcileResult()
    after = ""  # sorts before every valid _key
    while True:
        bind_vars: dict[str, Any] = {
            "@collection": collection,
            "after": after,
            "batch_size": batch_size,
            "days": policy.days,
            "only_missing": only_missing,
        }
        cursor = db.aql.execute(query, bind_vars=bind_vars)
        batch = next(iter(cursor))  # type: ignore[arg-type]
        if not batch["scanned"]:
            break
        result.batches += 1
        result.scanned += batch["scanned"]
        result.stale += batch["stale"]
        if not dry_run:
            result.updated += batch["stale"]
        logger.info(
            "reconcile_expiry_batch collection=%s batch=%s scanned=%s stale=%s dry_run=%s",
            collection,
            result.batches,
            result.scanned,
            result.stale,
            dry_run,
        )
        after = batch["last_key"]
        if batch["scanned"] < batch_size:
            break
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``arango-connector-reconcile-expiry``."""
    parser = argparse.ArgumentParser(
        prog="arango-connector-reconcile-expiry",
        description="Apply the current RETENTION_DAYS to expires_at on stored documents.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="count, do not write")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="only documents without expires_at (e.g. after upgrading from schema v3)",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(CoreSettings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_logging(settings.log_level)

    client = None
    try:
        client, db = connect(
            settings.arango_hosts,
            settings.arango_username,
            settings.arango_password.get_secret_value(),
            settings.arango_db,
            create=False,  # a maintenance command must never create databases
        )
        result = reconcile_expiry(
            db,
            settings.arango_collection,
            settings.retention_policy(),
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            only_missing=args.only_missing,
        )
    except (ValueError, LookupError) as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception:
        logger.exception("reconcile_expiry_failed")
        return 1
    finally:
        if client is not None:
            client.close()

    logger.info(
        "reconcile_expiry_done collection=%s scanned=%s stale=%s updated=%s dry_run=%s",
        settings.arango_collection,
        result.scanned,
        result.stale,
        result.updated,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
