"""Gmail polling source: incremental sync per mailbox via the Gmail history API."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from arango_connector.sources.base import EmitFn
from arango_connector.sources.gmail.client import GmailClient, HistoryExpiredError
from arango_connector.sources.gmail.config import InitialSync, LabelMatch
from arango_connector.sources.gmail.normalize import (
    AttachmentOptions,
    AttachmentPart,
    NormalizeOptions,
    attachment_parts,
    decode_part_data,
    mailbox_key,
    normalize,
    normalize_attachment,
)
from arango_connector.sources.polling import PollingSourceAdapter
from arango_connector.state.base import StateStore

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "gmail"

# Catch-up after a history gap re-reads from slightly before the last sync, so mail
# whose internalDate predates its history entry is not missed. Duplicates are harmless:
# keys are deterministic.
GAP_OVERLAP = timedelta(hours=1)

ClientFactory = Callable[[str], GmailClient]


class _Stopping(Exception):
    """Raised mid-sync when the adapter is stopping; the cursor is left untouched."""


@dataclass(frozen=True)
class LabelFilter:
    """Ingest a message carrying any ``include`` label and no ``exclude`` label."""

    include: frozenset[str]
    exclude: frozenset[str]

    def included(self, label_ids: Iterable[str]) -> bool:
        return bool(self.include.intersection(label_ids))

    def excluded(self, label_ids: Iterable[str]) -> bool:
        return bool(self.exclude.intersection(label_ids))

    def matches(self, label_ids: Iterable[str]) -> bool:
        labels = list(label_ids)
        return self.included(labels) and not self.excluded(labels)


@dataclass
class _Changes:
    """What history reported since the cursor, in order of first appearance."""

    ids: list[str] = field(default_factory=list)
    # Messages that are new to the mailbox (or come from a by-date listing).
    added: set[str] = field(default_factory=set)
    # Messages whose labels changed (re-read with updated_at set).
    relabelled: set[str] = field(default_factory=set)
    # Labels added per message, to spot a watched label being newly applied.
    labels_added: dict[str, set[str]] = field(default_factory=dict)


def resolve_labels(
    configured: Iterable[str], available: list[dict[str, Any]]
) -> tuple[frozenset[str], list[str]]:
    """Map configured label IDs or names (case-insensitive) to this mailbox's label IDs.

    Returns the resolved IDs and the entries that matched nothing. User label IDs
    (``Label_123``) differ per mailbox, which is why names are accepted too.
    """
    by_id = {str(label["id"]): str(label["id"]) for label in available}
    by_name = {str(label.get("name", "")).lower(): str(label["id"]) for label in available}
    resolved: set[str] = set()
    unknown: list[str] = []
    for entry in configured:
        label_id = by_id.get(entry) or by_name.get(entry.lower())
        if label_id is None:
            unknown.append(entry)
        else:
            resolved.add(label_id)
    return frozenset(resolved), unknown


@dataclass
class _Mailbox:
    address: str
    key: str  # hashed or plain, per GMAIL_KEY_MODE; also what logs show
    client: GmailClient | None = None
    labels: LabelFilter | None = None


class GmailPollingAdapter(PollingSourceAdapter):
    """Polls each configured mailbox and emits its new and re-labelled messages.

    Per mailbox, the cursor (Gmail ``historyId`` plus the time of the last sync) lives in
    the StateStore, so a restart resumes where it stopped. The cursor only advances
    after every message up to it has been emitted, so delivery is at-least-once;
    deterministic keys make repeats harmless.

    TODO(future): fetch messages with batch HTTP requests instead of one call each.
    TODO(future): Pub/Sub push notifications instead of polling.
    TODO(future): handle messagesDeleted once Envelope/Sink support deletes.
    """

    name = "gmail"

    def __init__(
        self,
        mailboxes: list[str],
        client_factory: ClientFactory,
        state: StateStore,
        *,
        include_labels: list[str],
        exclude_labels: list[str],
        options: NormalizeOptions,
        attachments: AttachmentOptions | None = None,
        initial_sync: InitialSync = "now",
        initial_lookback: timedelta = timedelta(hours=24),
        label_match: LabelMatch = "thread",
        interval_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(interval_seconds)
        self._mailboxes = [_Mailbox(m, mailbox_key(m, options.key_mode)) for m in mailboxes]
        self._client_factory = client_factory
        self._state = state
        self._include = list(include_labels)
        self._exclude = list(exclude_labels)
        self._options = options
        self._attachments = attachments or AttachmentOptions()
        self._initial_sync = initial_sync
        self._initial_lookback = initial_lookback
        self._label_match = label_match
        self._clock = clock

    def poll_once(self, emit: EmitFn) -> None:
        """Sync every mailbox. One failing mailbox does not stop the others.

        Raises only if every mailbox failed, which points at something systemic (network,
        credentials) and lets the polling base back off.
        """
        failures = 0
        for mailbox in self._mailboxes:
            if self.stopping:
                return
            try:
                self._sync(mailbox, emit)
            except _Stopping:
                logger.info("gmail_sync_interrupted mailbox=%s cursor unchanged", mailbox.key)
                return
            except Exception:
                failures += 1
                logger.exception("gmail_mailbox_sync_failed mailbox=%s", mailbox.key)
        if failures and failures == len(self._mailboxes):
            raise RuntimeError(f"all {failures} Gmail mailbox(es) failed to sync")

    # -- per mailbox ----------------------------------------------------------

    def _sync(self, mailbox: _Mailbox, emit: EmitFn) -> None:
        client = self._client(mailbox)
        labels = self._labels(mailbox, client)
        started_at = self._clock()
        cursor = self._state.get(STATE_NAMESPACE, mailbox.key)

        if cursor is None:
            self._first_sync(mailbox, client, labels, emit, started_at)
            return

        try:
            changes, history_id = self._collect_history(client, str(cursor["history_id"]))
        except HistoryExpiredError:
            logger.warning(
                "gmail_history_gap mailbox=%s history_id=%s catching up by date",
                mailbox.key,
                cursor["history_id"],
            )
            # Take the new cursor first, so mail arriving during catch-up is caught by
            # the next history poll rather than falling between the two.
            history_id = str(client.get_profile()["historyId"])
            since = datetime.fromisoformat(str(cursor["last_synced_at"])) - GAP_OVERLAP
            added = list(client.list_message_ids(_after_query(since)))
            self._ingest(mailbox, client, labels, _Changes(ids=added, added=set(added)), emit)
        else:
            self._ingest(mailbox, client, labels, changes, emit)

        self._save_cursor(mailbox, history_id, started_at)

    def _first_sync(
        self,
        mailbox: _Mailbox,
        client: GmailClient,
        labels: LabelFilter,
        emit: EmitFn,
        started_at: datetime,
    ) -> None:
        history_id = str(client.get_profile()["historyId"])
        if self._initial_sync == "lookback":
            since = started_at - self._initial_lookback
            ids = list(client.list_message_ids(_after_query(since)))
            logger.info(
                "gmail_initial_lookback mailbox=%s since=%s messages=%s",
                mailbox.key,
                since.isoformat(),
                len(ids),
            )
            self._ingest(mailbox, client, labels, _Changes(ids=ids, added=set(ids)), emit)
        self._save_cursor(mailbox, history_id, started_at)
        logger.info(
            "gmail_mailbox_initialised mailbox=%s initial_sync=%s history_id=%s",
            mailbox.key,
            self._initial_sync,
            history_id,
        )

    def _collect_history(self, client: GmailClient, start_history_id: str) -> tuple[_Changes, str]:
        """Messages changed since ``start_history_id``, and the history id to resume from."""
        changes = _Changes()
        seen: set[str] = set()
        latest = start_history_id
        for page in client.list_history(start_history_id):
            latest = str(page.get("historyId", latest))
            for record in page.get("history", []):
                for kind in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    for entry in record.get(kind, []):
                        message_id = str(entry["message"]["id"])
                        if message_id not in seen:
                            seen.add(message_id)
                            changes.ids.append(message_id)
                        if kind == "messagesAdded":
                            changes.added.add(message_id)
                        else:
                            changes.relabelled.add(message_id)
                        if kind == "labelsAdded":
                            changes.labels_added.setdefault(message_id, set()).update(
                                entry.get("labelIds", [])
                            )
        return changes, latest

    def _ingest(
        self,
        mailbox: _Mailbox,
        client: GmailClient,
        labels: LabelFilter,
        changes: _Changes,
        emit: EmitFn,
    ) -> None:
        """Fetch each changed message and emit those that pass the label filter.

        In ``thread`` mode a message also qualifies through its thread, so replies
        to a labelled conversation are ingested; and a watched label newly added to a
        message pulls in the rest of its thread. Excluded labels always apply to the
        message itself.
        """
        queue = list(changes.ids)
        queued = set(queue)
        relabelled = set(changes.relabelled)
        # Messages being stored for the first time, as far as history tells: new mail,
        # mail that just gained a watched label, and thread siblings pulled in with it.
        # Only these get their attachments fetched; a read/unread toggle does not
        # re-download files that cannot have changed.
        first_seen = set(changes.added)
        threads: dict[str, list[dict[str, Any]]] = {}  # per-poll cache of thread label sets
        emitted = skipped = gone = attachments = 0
        position = 0
        while position < len(queue):
            message_id = queue[position]
            position += 1
            if self.stopping:
                # Leave the cursor where it was; the next start re-reads from there.
                raise _Stopping
            message = client.get_message(message_id)
            if message is None:
                gone += 1  # deleted since the history entry; deletes are out of scope
                continue
            own = message.get("labelIds") or []
            if labels.excluded(own) or not self._qualifies(client, labels, message, threads):
                skipped += 1
                continue

            watched_label_added = labels.included(changes.labels_added.get(message_id, ()))
            if watched_label_added:
                first_seen.add(message_id)
            if self._label_match == "thread" and watched_label_added:
                # A watched label was just applied: bring in the rest of the conversation.
                for sibling in self._thread(client, threads, message):
                    sibling_id = str(sibling["id"])
                    if sibling_id not in queued:
                        queued.add(sibling_id)
                        queue.append(sibling_id)
                        relabelled.add(sibling_id)
                        first_seen.add(sibling_id)

            updated_at = self._clock() if message_id in relabelled else None
            envelope = normalize(message, mailbox.address, self._options, updated_at=updated_at)
            if envelope is None:
                skipped += 1
                continue
            emit(envelope)
            emitted += 1
            if self._attachments.enabled and message_id in first_seen:
                attachments += self._emit_attachments(mailbox, client, message, updated_at, emit)
        if queue:
            logger.info(
                "gmail_mailbox_synced mailbox=%s changed=%s emitted=%s filtered=%s gone=%s "
                "attachments=%s",
                mailbox.key,
                len(queue),
                emitted,
                skipped,
                gone,
                attachments,
            )

    def _emit_attachments(
        self,
        mailbox: _Mailbox,
        client: GmailClient,
        message: dict[str, Any],
        updated_at: datetime | None,
        emit: EmitFn,
    ) -> int:
        """Emit one envelope per attachment of ``message``; returns how many."""
        count = 0
        for part in attachment_parts(message):
            data, skipped_reason = self._attachment_bytes(client, message, part)
            emit(
                normalize_attachment(
                    message,
                    part,
                    mailbox.address,
                    self._options,
                    self._attachments,
                    data=data,
                    skipped_reason=skipped_reason,
                    updated_at=updated_at,
                )
            )
            count += 1
        return count

    def _attachment_bytes(
        self, client: GmailClient, message: dict[str, Any], part: AttachmentPart
    ) -> tuple[bytes | None, str | None]:
        """The attachment's bytes when the mode needs them, else why there are none.

        A transient API error propagates, failing this mailbox's sync so the next poll
        retries from the same cursor.
        """
        if not self._attachments.needs_bytes:
            return None, None
        if part.size > self._attachments.max_bytes:
            return None, "too_large"
        if part.data:
            return decode_part_data(part.data), None
        if part.attachment_id:
            data = client.get_attachment(str(message["id"]), part.attachment_id)
            if data is not None:
                return data, None
        return None, "download_failed"

    def _qualifies(
        self,
        client: GmailClient,
        labels: LabelFilter,
        message: dict[str, Any],
        threads: dict[str, list[dict[str, Any]]],
    ) -> bool:
        """Whether a (non-excluded) message carries, or its thread carries, a watched label."""
        if labels.included(message.get("labelIds") or []):
            return True
        if self._label_match != "thread":
            return False
        # Only messages that are not themselves excluded can qualify their thread, so a
        # labelled message sitting in spam does not pull in its conversation.
        return any(
            labels.matches(sibling.get("labelIds") or [])
            for sibling in self._thread(client, threads, message)
        )

    def _thread(
        self,
        client: GmailClient,
        threads: dict[str, list[dict[str, Any]]],
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        thread_id = str(message.get("threadId") or message["id"])
        if thread_id not in threads:
            threads[thread_id] = client.get_thread_messages(thread_id)
        return threads[thread_id]

    def _save_cursor(self, mailbox: _Mailbox, history_id: str, synced_at: datetime) -> None:
        self._state.put(
            STATE_NAMESPACE,
            mailbox.key,
            {"history_id": history_id, "last_synced_at": synced_at.isoformat()},
        )

    # -- lazily built per-mailbox resources ------------------------------------

    def _client(self, mailbox: _Mailbox) -> GmailClient:
        if mailbox.client is None:
            mailbox.client = self._client_factory(mailbox.address)
        return mailbox.client

    def _labels(self, mailbox: _Mailbox, client: GmailClient) -> LabelFilter:
        if mailbox.labels is None:
            available = client.list_labels()
            include, unknown_include = resolve_labels(self._include, available)
            exclude, unknown_exclude = resolve_labels(self._exclude, available)
            for entry in (*unknown_include, *unknown_exclude):
                logger.warning("gmail_label_unknown mailbox=%s label=%s", mailbox.key, entry)
            if not include:
                logger.warning(
                    "gmail_no_watched_labels mailbox=%s nothing will be ingested", mailbox.key
                )
            mailbox.labels = LabelFilter(include, exclude)
        return mailbox.labels


def _after_query(since: datetime) -> str:
    """A Gmail search for mail received after ``since`` (epoch seconds, UTC)."""
    return f"after:{int(since.timestamp())}"
