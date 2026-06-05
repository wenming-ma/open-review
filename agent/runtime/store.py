"""Store abstractions for the durable MR actor runtime."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Protocol

from agent.config import ensure_writable_directory
from agent.runtime.models import (
    ActorRuntimeStatus,
    EventEnvelope,
    PublishReceipt,
    RunCheckpoint,
    RunJournalEvent,
    RunRecord,
    RunTerminationRequest,
)
from agent.utils.timezone import now_in_open_review_tz, parse_iso_datetime


class RuntimeStore(Protocol):
    async def append_event(self, event: EventEnvelope) -> bool: ...
    async def list_actor_events(self, actor_key: str) -> list[EventEnvelope]: ...
    async def remove_pending_event(self, actor_key: str, event_id: str) -> bool: ...
    async def has_actor_events(self, actor_key: str) -> bool: ...
    async def restore_inflight(self, actor_key: str) -> int: ...
    async def pop_next_batch(self, actor_key: str) -> list[EventEnvelope]: ...
    async def ack_batch(self, actor_key: str, batch: list[EventEnvelope]) -> None: ...
    async def mark_inflight_failed(
        self,
        actor_key: str,
        batch: list[EventEnvelope],
        *,
        error: str,
        max_attempts: int,
    ) -> bool: ...
    async def write_run(self, record: RunRecord) -> None: ...
    async def list_runs(self, actor_key: str, limit: int = 20) -> list[RunRecord]: ...
    async def record_publish_receipt(self, receipt: PublishReceipt) -> None: ...
    async def claim_publish_receipt(
        self,
        receipt: PublishReceipt,
        *,
        stale_after_seconds: int | None = None,
    ) -> tuple[PublishReceipt, bool]: ...
    async def get_publish_receipt(self, actor_key: str, op_key: str) -> PublishReceipt | None: ...
    async def record_run_journal_event(self, event: RunJournalEvent) -> None: ...
    async def list_run_journal(
        self,
        execution_key: str,
        limit: int | None = None,
    ) -> list[RunJournalEvent]: ...
    async def write_run_checkpoint(self, checkpoint: RunCheckpoint) -> None: ...
    async def get_run_checkpoint(self, execution_key: str) -> RunCheckpoint | None: ...
    async def clear_run_checkpoint(self, execution_key: str) -> None: ...
    async def request_run_termination(
        self,
        run_id: str,
        *,
        actor_key: str,
        requested_by: str,
    ) -> RunTerminationRequest: ...
    async def get_run_termination(self, run_id: str) -> RunTerminationRequest | None: ...
    async def is_run_termination_requested(self, run_id: str) -> bool: ...
    async def list_actor_statuses(self) -> list[ActorRuntimeStatus]: ...
    async def mark_actor_scheduled(self, actor_key: str) -> bool: ...
    async def clear_actor_scheduled(self, actor_key: str) -> None: ...
    async def acquire_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool: ...
    async def heartbeat_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool: ...
    async def release_lease(self, actor_key: str, worker_id: str) -> None: ...


def _parse_received_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mention_batch_window_seconds(project_id: str) -> int:
    try:
        from agent.controlplane import get_config_service

        value = get_config_service().get_project_agent_config(project_id).get("MENTION_BATCH_WINDOW_SECONDS")
    except Exception:
        from agent.controlplane.service import project_agent_default_config

        value = project_agent_default_config().get("MENTION_BATCH_WINDOW_SECONDS")
    try:
        return int(value or 0)
    except Exception:
        return 15


def _should_batch_mentions(batch: list[EventEnvelope], next_event: EventEnvelope) -> bool:
    if not batch:
        return False
    first = batch[0]
    if first.event_type != "mention" or not first.discussion_id:
        return False
    if next_event.event_type != "mention" or next_event.discussion_id != first.discussion_id:
        return False
    window = _mention_batch_window_seconds(first.project_id)
    if window <= 0:
        return True
    previous = batch[-1]
    delta = (_parse_received_at(next_event.received_at) - _parse_received_at(previous.received_at)).total_seconds()
    return delta <= window


def _publish_receipt_can_be_reclaimed(
    receipt: PublishReceipt,
    *,
    stale_after_seconds: int | None,
) -> bool:
    if receipt.status == "failed":
        return True
    if receipt.status != "claimed":
        return False
    if stale_after_seconds is None or stale_after_seconds <= 0:
        return False
    try:
        created_at = parse_iso_datetime(receipt.created_at)
    except Exception:
        return True
    age_seconds = (now_in_open_review_tz() - created_at).total_seconds()
    return age_seconds >= stale_after_seconds


class InMemoryRuntimeStore:
    """Test-friendly in-memory runtime store."""

    def __init__(self) -> None:
        self._events: dict[str, deque[EventEnvelope]] = defaultdict(deque)
        self._inflight: dict[str, list[EventEnvelope]] = {}
        self._runs: dict[str, deque[RunRecord]] = defaultdict(deque)
        self._publish_receipts: dict[str, dict[str, PublishReceipt]] = defaultdict(dict)
        self._run_journal: dict[str, list[RunJournalEvent]] = defaultdict(list)
        self._run_checkpoints: dict[str, RunCheckpoint] = {}
        self._run_terminations: dict[str, RunTerminationRequest] = {}
        self._seen_event_ids: set[str] = set()
        self._scheduled: set[str] = set()
        self._leases: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def append_event(self, event: EventEnvelope) -> bool:
        async with self._lock:
            if event.event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event.event_id)
            self._events[event.actor_key].append(event)
            return True

    async def list_actor_events(self, actor_key: str) -> list[EventEnvelope]:
        async with self._lock:
            return list(self._events.get(actor_key, ()))

    async def remove_pending_event(self, actor_key: str, event_id: str) -> bool:
        async with self._lock:
            if any(item.event_id == event_id for item in self._inflight.get(actor_key, ())):
                return False
            queue = self._events.get(actor_key)
            if not queue:
                return False
            remaining = deque(item for item in queue if item.event_id != event_id)
            if len(remaining) == len(queue):
                return False
            if remaining:
                self._events[actor_key] = remaining
            else:
                self._events.pop(actor_key, None)
            return True

    async def has_actor_events(self, actor_key: str) -> bool:
        async with self._lock:
            return bool(self._events.get(actor_key) or self._inflight.get(actor_key))

    async def restore_inflight(self, actor_key: str) -> int:
        async with self._lock:
            batch = self._inflight.pop(actor_key, [])
            if not batch:
                return 0
            queue = self._events[actor_key]
            for event in reversed(batch):
                queue.appendleft(event)
            return len(batch)

    async def pop_next_batch(self, actor_key: str) -> list[EventEnvelope]:
        async with self._lock:
            if self._inflight.get(actor_key):
                return []
            queue = self._events.get(actor_key)
            if not queue:
                return []
            batch = [queue.popleft()]
            if batch[0].event_type == "auto_review":
                while queue and queue[0].event_type == "auto_review":
                    batch.append(queue.popleft())
            elif batch[0].event_type == "mention":
                while queue and _should_batch_mentions(batch, queue[0]):
                    batch.append(queue.popleft())
            if not queue:
                self._events.pop(actor_key, None)
            self._inflight[actor_key] = list(batch)
            return list(batch)

    async def ack_batch(self, actor_key: str, batch: list[EventEnvelope]) -> None:
        async with self._lock:
            inflight = self._inflight.get(actor_key)
            if inflight and [item.event_id for item in inflight] == [item.event_id for item in batch]:
                self._inflight.pop(actor_key, None)

    async def mark_inflight_failed(
        self,
        actor_key: str,
        batch: list[EventEnvelope],
        *,
        error: str,
        max_attempts: int,
    ) -> bool:
        async with self._lock:
            inflight = self._inflight.get(actor_key)
            if not inflight or [item.event_id for item in inflight] != [item.event_id for item in batch]:
                return False
            attempts = max(int(item.payload.get("_runtime_attempts") or 0) for item in inflight) + 1
            if attempts >= max(max_attempts, 1):
                self._inflight.pop(actor_key, None)
                return False
            self._inflight[actor_key] = [
                item.model_copy(
                    update={
                        "payload": {
                            **item.payload,
                            "_runtime_attempts": attempts,
                            "_runtime_last_error": error,
                        }
                    }
                )
                for item in inflight
            ]
            return True

    async def write_run(self, record: RunRecord) -> None:
        async with self._lock:
            runs = self._runs[record.actor_key]
            for index, existing in enumerate(runs):
                if existing.run_id == record.run_id:
                    runs[index] = record
                    break
            else:
                runs.appendleft(record)
            while len(runs) > 50:
                runs.pop()

    async def list_runs(self, actor_key: str, limit: int = 20) -> list[RunRecord]:
        async with self._lock:
            return list(self._runs.get(actor_key, ()))[:limit]

    async def record_publish_receipt(self, receipt: PublishReceipt) -> None:
        async with self._lock:
            self._publish_receipts[receipt.actor_key][receipt.op_key] = receipt

    async def claim_publish_receipt(
        self,
        receipt: PublishReceipt,
        *,
        stale_after_seconds: int | None = None,
    ) -> tuple[PublishReceipt, bool]:
        async with self._lock:
            existing = self._publish_receipts.get(receipt.actor_key, {}).get(receipt.op_key)
            if existing is not None and not _publish_receipt_can_be_reclaimed(
                existing,
                stale_after_seconds=stale_after_seconds,
            ):
                return existing, False
            self._publish_receipts[receipt.actor_key][receipt.op_key] = receipt
            return receipt, True

    async def get_publish_receipt(self, actor_key: str, op_key: str) -> PublishReceipt | None:
        async with self._lock:
            return self._publish_receipts.get(actor_key, {}).get(op_key)

    async def record_run_journal_event(self, event: RunJournalEvent) -> None:
        async with self._lock:
            self._run_journal[event.execution_key].append(event)

    async def list_run_journal(self, execution_key: str, limit: int | None = None) -> list[RunJournalEvent]:
        async with self._lock:
            journal = list(self._run_journal.get(execution_key, ()))
            if limit is not None and limit > 0:
                return journal[-limit:]
            return journal

    async def write_run_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            self._run_checkpoints[checkpoint.execution_key] = checkpoint

    async def get_run_checkpoint(self, execution_key: str) -> RunCheckpoint | None:
        async with self._lock:
            return self._run_checkpoints.get(execution_key)

    async def clear_run_checkpoint(self, execution_key: str) -> None:
        async with self._lock:
            self._run_checkpoints.pop(execution_key, None)

    async def request_run_termination(
        self,
        run_id: str,
        *,
        actor_key: str,
        requested_by: str,
    ) -> RunTerminationRequest:
        async with self._lock:
            existing = self._run_terminations.get(run_id)
            if existing is not None:
                return existing
            request = RunTerminationRequest(
                run_id=run_id,
                actor_key=actor_key,
                requested_by=requested_by,
            )
            self._run_terminations[run_id] = request
            return request

    async def get_run_termination(self, run_id: str) -> RunTerminationRequest | None:
        async with self._lock:
            return self._run_terminations.get(run_id)

    async def is_run_termination_requested(self, run_id: str) -> bool:
        async with self._lock:
            return run_id in self._run_terminations

    async def list_actor_statuses(self) -> list[ActorRuntimeStatus]:
        async with self._lock:
            actor_keys = (
                set(self._events)
                | set(self._inflight)
                | set(self._leases)
                | set(self._scheduled)
            )
            results = []
            now = time.monotonic()
            for actor_key in sorted(actor_keys):
                lease = self._leases.get(actor_key)
                ttl = None
                owner = None
                if lease:
                    owner = lease[0]
                    ttl = max(int(lease[1] - now), 0)
                results.append(
                    ActorRuntimeStatus(
                        actor_key=actor_key,
                        pending_count=len(self._events.get(actor_key, ())),
                        inflight_count=len(self._inflight.get(actor_key, ())),
                        lease_owner=owner,
                        lease_ttl_seconds=ttl,
                        scheduled=actor_key in self._scheduled,
                    )
                )
            return results

    async def mark_actor_scheduled(self, actor_key: str) -> bool:
        async with self._lock:
            if actor_key in self._scheduled:
                return False
            self._scheduled.add(actor_key)
            return True

    async def clear_actor_scheduled(self, actor_key: str) -> None:
        async with self._lock:
            self._scheduled.discard(actor_key)

    async def acquire_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            lease = self._leases.get(actor_key)
            if lease and lease[1] > now and lease[0] != worker_id:
                return False
            self._leases[actor_key] = (worker_id, now + ttl_seconds)
            return True

    async def heartbeat_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        async with self._lock:
            lease = self._leases.get(actor_key)
            if not lease or lease[0] != worker_id:
                return False
            self._leases[actor_key] = (worker_id, time.monotonic() + ttl_seconds)
            return True

    async def release_lease(self, actor_key: str, worker_id: str) -> None:
        async with self._lock:
            lease = self._leases.get(actor_key)
            if lease and lease[0] == worker_id:
                self._leases.pop(actor_key, None)


class SQLiteRuntimeStore:
    """SQLite-backed runtime store for single-host recoverable actors."""

    EVENT_ID_TTL_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        ensure_writable_directory(self.db_path.parent)
        self._initialize()

    @classmethod
    async def from_path(cls, db_path: str) -> SQLiteRuntimeStore:
        return cls(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_actor_state (
                    actor_key TEXT PRIMARY KEY,
                    events_json TEXT NOT NULL DEFAULT '[]',
                    inflight_json TEXT NOT NULL DEFAULT '[]',
                    scheduled INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS runtime_seen_event_ids (
                    event_id TEXT PRIMARY KEY,
                    actor_key TEXT NOT NULL,
                    seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_runs (
                    run_id TEXT PRIMARY KEY,
                    actor_key TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_runs_actor_updated
                    ON runtime_runs(actor_key, updated_at DESC);
                CREATE TABLE IF NOT EXISTS runtime_publish_receipts (
                    actor_key TEXT NOT NULL,
                    op_key TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (actor_key, op_key)
                );
                CREATE TABLE IF NOT EXISTS runtime_run_journal (
                    execution_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (execution_key, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_run_journal_created
                    ON runtime_run_journal(execution_key, created_at ASC);
                CREATE TABLE IF NOT EXISTS runtime_run_checkpoints (
                    execution_key TEXT PRIMARY KEY,
                    checkpoint_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_run_terminations (
                    run_id TEXT PRIMARY KEY,
                    actor_key TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _decode_events(raw: str | None) -> list[EventEnvelope]:
        payload = json.loads(raw or "[]")
        return [EventEnvelope.model_validate(item) for item in payload]

    @staticmethod
    def _encode_events(events: list[EventEnvelope]) -> str:
        return json.dumps([item.model_dump(mode="json") for item in events], ensure_ascii=True)

    def _load_actor_state(self, conn: sqlite3.Connection, actor_key: str) -> tuple[list[EventEnvelope], list[EventEnvelope], bool, str | None, float | None]:
        row = conn.execute(
            """
            SELECT events_json, inflight_json, scheduled, lease_owner, lease_expires_at
            FROM runtime_actor_state
            WHERE actor_key = ?
            """,
            (actor_key,),
        ).fetchone()
        if row is None:
            return [], [], False, None, None
        return (
            self._decode_events(row["events_json"]),
            self._decode_events(row["inflight_json"]),
            bool(row["scheduled"]),
            row["lease_owner"],
            row["lease_expires_at"],
        )

    def _persist_actor_state(
        self,
        conn: sqlite3.Connection,
        *,
        actor_key: str,
        events: list[EventEnvelope],
        inflight: list[EventEnvelope],
        scheduled: bool,
        lease_owner: str | None,
        lease_expires_at: float | None,
    ) -> None:
        if not events and not inflight and not scheduled and not lease_owner:
            conn.execute("DELETE FROM runtime_actor_state WHERE actor_key = ?", (actor_key,))
            return
        conn.execute(
            """
            INSERT INTO runtime_actor_state(
                actor_key, events_json, inflight_json, scheduled, lease_owner, lease_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_key) DO UPDATE SET
                events_json=excluded.events_json,
                inflight_json=excluded.inflight_json,
                scheduled=excluded.scheduled,
                lease_owner=excluded.lease_owner,
                lease_expires_at=excluded.lease_expires_at
            """,
            (
                actor_key,
                self._encode_events(events),
                self._encode_events(inflight),
                1 if scheduled else 0,
                lease_owner,
                lease_expires_at,
            ),
        )

    async def append_event(self, event: EventEnvelope) -> bool:
        return await asyncio.to_thread(self._append_event_sync, event)

    def _append_event_sync(self, event: EventEnvelope) -> bool:
        cutoff = time.time() - self.EVENT_ID_TTL_SECONDS
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM runtime_seen_event_ids WHERE seen_at < ?",
                (cutoff,),
            )
            existing = conn.execute(
                "SELECT 1 FROM runtime_seen_event_ids WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO runtime_seen_event_ids(event_id, actor_key, seen_at) VALUES (?, ?, ?)",
                (event.event_id, event.actor_key, time.time()),
            )
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                event.actor_key,
            )
            events.append(event)
            self._persist_actor_state(
                conn,
                actor_key=event.actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return True

    async def list_actor_events(self, actor_key: str) -> list[EventEnvelope]:
        return await asyncio.to_thread(self._list_actor_events_sync, actor_key)

    def _list_actor_events_sync(self, actor_key: str) -> list[EventEnvelope]:
        with self._connect() as conn:
            events, _inflight, _scheduled, _lease_owner, _lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            return events

    async def remove_pending_event(self, actor_key: str, event_id: str) -> bool:
        return await asyncio.to_thread(self._remove_pending_event_sync, actor_key, event_id)

    def _remove_pending_event_sync(self, actor_key: str, event_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if any(item.event_id == event_id for item in inflight):
                conn.commit()
                return False
            remaining = [item for item in events if item.event_id != event_id]
            if len(remaining) == len(events):
                conn.commit()
                return False
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=remaining,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return True

    async def has_actor_events(self, actor_key: str) -> bool:
        return await asyncio.to_thread(self._has_actor_events_sync, actor_key)

    def _has_actor_events_sync(self, actor_key: str) -> bool:
        with self._connect() as conn:
            events, inflight, _scheduled, _lease_owner, _lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            return bool(events or inflight)

    async def restore_inflight(self, actor_key: str) -> int:
        return await asyncio.to_thread(self._restore_inflight_sync, actor_key)

    def _restore_inflight_sync(self, actor_key: str) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if not inflight:
                conn.commit()
                return 0
            restored = list(inflight)
            events = restored + events
            inflight = []
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return len(restored)

    async def pop_next_batch(self, actor_key: str) -> list[EventEnvelope]:
        return await asyncio.to_thread(self._pop_next_batch_sync, actor_key)

    def _pop_next_batch_sync(self, actor_key: str) -> list[EventEnvelope]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if inflight or not events:
                conn.commit()
                return []

            batch = [events.pop(0)]
            if batch[0].event_type == "auto_review":
                while events and events[0].event_type == "auto_review":
                    batch.append(events.pop(0))
            elif batch[0].event_type == "mention":
                while events and _should_batch_mentions(batch, events[0]):
                    batch.append(events.pop(0))

            inflight = list(batch)
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return list(batch)

    async def ack_batch(self, actor_key: str, batch: list[EventEnvelope]) -> None:
        await asyncio.to_thread(self._ack_batch_sync, actor_key, batch)

    def _ack_batch_sync(self, actor_key: str, batch: list[EventEnvelope]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if [item.event_id for item in inflight] == [item.event_id for item in batch]:
                inflight = []
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()

    async def mark_inflight_failed(
        self,
        actor_key: str,
        batch: list[EventEnvelope],
        *,
        error: str,
        max_attempts: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._mark_inflight_failed_sync,
            actor_key,
            batch,
            error,
            max_attempts,
        )

    def _mark_inflight_failed_sync(
        self,
        actor_key: str,
        batch: list[EventEnvelope],
        error: str,
        max_attempts: int,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if [item.event_id for item in inflight] != [item.event_id for item in batch]:
                conn.commit()
                return False
            attempts = max(int(item.payload.get("_runtime_attempts") or 0) for item in inflight) + 1
            retryable = attempts < max(max_attempts, 1)
            if retryable:
                inflight = [
                    item.model_copy(
                        update={
                            "payload": {
                                **item.payload,
                                "_runtime_attempts": attempts,
                                "_runtime_last_error": error,
                            }
                        }
                    )
                    for item in inflight
                ]
            else:
                inflight = []
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return retryable

    async def write_run(self, record: RunRecord) -> None:
        await asyncio.to_thread(self._write_run_sync, record)

    def _write_run_sync(self, record: RunRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_runs(run_id, actor_key, record_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    actor_key=excluded.actor_key,
                    record_json=excluded.record_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record.run_id,
                    record.actor_key,
                    record.model_dump_json(),
                    time.time(),
                ),
            )

    async def list_runs(self, actor_key: str, limit: int = 20) -> list[RunRecord]:
        return await asyncio.to_thread(self._list_runs_sync, actor_key, limit)

    def _list_runs_sync(self, actor_key: str, limit: int) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json
                FROM runtime_runs
                WHERE actor_key = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (actor_key, max(limit, 0)),
            ).fetchall()
        return [RunRecord.model_validate_json(row["record_json"]) for row in rows]

    async def record_publish_receipt(self, receipt: PublishReceipt) -> None:
        await asyncio.to_thread(self._record_publish_receipt_sync, receipt)

    def _record_publish_receipt_sync(self, receipt: PublishReceipt) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_publish_receipts(actor_key, op_key, receipt_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_key, op_key) DO UPDATE SET
                    receipt_json=excluded.receipt_json
                """,
                (
                    receipt.actor_key,
                    receipt.op_key,
                    receipt.model_dump_json(),
                    time.time(),
                ),
            )

    async def claim_publish_receipt(
        self,
        receipt: PublishReceipt,
        *,
        stale_after_seconds: int | None = None,
    ) -> tuple[PublishReceipt, bool]:
        return await asyncio.to_thread(
            self._claim_publish_receipt_sync,
            receipt,
            stale_after_seconds,
        )

    def _claim_publish_receipt_sync(
        self,
        receipt: PublishReceipt,
        stale_after_seconds: int | None,
    ) -> tuple[PublishReceipt, bool]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT receipt_json
                FROM runtime_publish_receipts
                WHERE actor_key = ? AND op_key = ?
                """,
                (receipt.actor_key, receipt.op_key),
            ).fetchone()
            if row is not None:
                existing = PublishReceipt.model_validate_json(row["receipt_json"])
                if not _publish_receipt_can_be_reclaimed(
                    existing,
                    stale_after_seconds=stale_after_seconds,
                ):
                    conn.commit()
                    return existing, False
                conn.execute(
                    """
                    UPDATE runtime_publish_receipts
                    SET receipt_json = ?, created_at = ?
                    WHERE actor_key = ? AND op_key = ?
                    """,
                    (
                        receipt.model_dump_json(),
                        time.time(),
                        receipt.actor_key,
                        receipt.op_key,
                    ),
                )
                conn.commit()
                return receipt, True
            conn.execute(
                """
                INSERT INTO runtime_publish_receipts(actor_key, op_key, receipt_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    receipt.actor_key,
                    receipt.op_key,
                    receipt.model_dump_json(),
                    time.time(),
                ),
            )
            conn.commit()
            return receipt, True

    async def get_publish_receipt(self, actor_key: str, op_key: str) -> PublishReceipt | None:
        return await asyncio.to_thread(self._get_publish_receipt_sync, actor_key, op_key)

    def _get_publish_receipt_sync(self, actor_key: str, op_key: str) -> PublishReceipt | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT receipt_json
                FROM runtime_publish_receipts
                WHERE actor_key = ? AND op_key = ?
                """,
                (actor_key, op_key),
            ).fetchone()
        if row is None:
            return None
        return PublishReceipt.model_validate_json(row["receipt_json"])

    async def record_run_journal_event(self, event: RunJournalEvent) -> None:
        await asyncio.to_thread(self._record_run_journal_event_sync, event)

    def _record_run_journal_event_sync(self, event: RunJournalEvent) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS sequence
                FROM runtime_run_journal
                WHERE execution_key = ?
                """,
                (event.execution_key,),
            ).fetchone()
            sequence = int(row["sequence"] or 0) + 1
            conn.execute(
                """
                INSERT INTO runtime_run_journal(execution_key, sequence, event_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.execution_key,
                    sequence,
                    event.model_dump_json(),
                    time.time(),
                ),
            )

    async def list_run_journal(self, execution_key: str, limit: int | None = None) -> list[RunJournalEvent]:
        return await asyncio.to_thread(self._list_run_journal_sync, execution_key, limit)

    def _list_run_journal_sync(self, execution_key: str, limit: int | None = None) -> list[RunJournalEvent]:
        params: tuple[object, ...] = (execution_key,)
        query = """
            SELECT event_json
            FROM runtime_run_journal
            WHERE execution_key = ?
            ORDER BY sequence ASC
        """
        reverse_rows = False
        if limit is not None and limit > 0:
            query = """
                SELECT event_json
                FROM runtime_run_journal
                WHERE execution_key = ?
                ORDER BY sequence DESC
                LIMIT ?
            """
            params = (execution_key, limit)
            reverse_rows = True
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if reverse_rows:
            rows = list(reversed(rows))
        return [RunJournalEvent.model_validate_json(row["event_json"]) for row in rows]

    async def write_run_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        await asyncio.to_thread(self._write_run_checkpoint_sync, checkpoint)

    def _write_run_checkpoint_sync(self, checkpoint: RunCheckpoint) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_run_checkpoints(execution_key, checkpoint_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(execution_key) DO UPDATE SET
                    checkpoint_json=excluded.checkpoint_json,
                    updated_at=excluded.updated_at
                """,
                (
                    checkpoint.execution_key,
                    checkpoint.model_dump_json(),
                    time.time(),
                ),
            )

    async def get_run_checkpoint(self, execution_key: str) -> RunCheckpoint | None:
        return await asyncio.to_thread(self._get_run_checkpoint_sync, execution_key)

    def _get_run_checkpoint_sync(self, execution_key: str) -> RunCheckpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT checkpoint_json
                FROM runtime_run_checkpoints
                WHERE execution_key = ?
                """,
                (execution_key,),
            ).fetchone()
        if row is None:
            return None
        return RunCheckpoint.model_validate_json(row["checkpoint_json"])

    async def clear_run_checkpoint(self, execution_key: str) -> None:
        await asyncio.to_thread(self._clear_run_checkpoint_sync, execution_key)

    def _clear_run_checkpoint_sync(self, execution_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM runtime_run_checkpoints WHERE execution_key = ?",
                (execution_key,),
            )

    async def request_run_termination(
        self,
        run_id: str,
        *,
        actor_key: str,
        requested_by: str,
    ) -> RunTerminationRequest:
        return await asyncio.to_thread(
            self._request_run_termination_sync,
            run_id,
            actor_key,
            requested_by,
        )

    def _request_run_termination_sync(
        self,
        run_id: str,
        actor_key: str,
        requested_by: str,
    ) -> RunTerminationRequest:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT request_json
                FROM runtime_run_terminations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                return RunTerminationRequest.model_validate_json(row["request_json"])
            request = RunTerminationRequest(
                run_id=run_id,
                actor_key=actor_key,
                requested_by=requested_by,
            )
            conn.execute(
                """
                INSERT INTO runtime_run_terminations(run_id, actor_key, requested_by, request_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    actor_key,
                    requested_by,
                    request.model_dump_json(),
                    time.time(),
                ),
            )
            return request

    async def get_run_termination(self, run_id: str) -> RunTerminationRequest | None:
        return await asyncio.to_thread(self._get_run_termination_sync, run_id)

    def _get_run_termination_sync(self, run_id: str) -> RunTerminationRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT request_json
                FROM runtime_run_terminations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunTerminationRequest.model_validate_json(row["request_json"])

    async def is_run_termination_requested(self, run_id: str) -> bool:
        return await asyncio.to_thread(self._is_run_termination_requested_sync, run_id)

    def _is_run_termination_requested_sync(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM runtime_run_terminations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return row is not None

    async def list_actor_statuses(self) -> list[ActorRuntimeStatus]:
        return await asyncio.to_thread(self._list_actor_statuses_sync)

    def _list_actor_statuses_sync(self) -> list[ActorRuntimeStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT actor_key, events_json, inflight_json, scheduled, lease_owner, lease_expires_at
                FROM runtime_actor_state
                ORDER BY actor_key ASC
                """
            ).fetchall()
        now = time.time()
        results: list[ActorRuntimeStatus] = []
        for row in rows:
            events = self._decode_events(row["events_json"])
            inflight = self._decode_events(row["inflight_json"])
            lease_expires_at = row["lease_expires_at"]
            lease_ttl = None
            if lease_expires_at is not None:
                lease_ttl = max(int(lease_expires_at - now), 0)
            results.append(
                ActorRuntimeStatus(
                    actor_key=row["actor_key"],
                    pending_count=len(events),
                    inflight_count=len(inflight),
                    lease_owner=row["lease_owner"],
                    lease_ttl_seconds=lease_ttl,
                    scheduled=bool(row["scheduled"]),
                )
            )
        return results

    async def mark_actor_scheduled(self, actor_key: str) -> bool:
        return await asyncio.to_thread(self._mark_actor_scheduled_sync, actor_key)

    def _mark_actor_scheduled_sync(self, actor_key: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if scheduled:
                conn.commit()
                return False
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=True,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
            return True

    async def clear_actor_scheduled(self, actor_key: str) -> None:
        await asyncio.to_thread(self._clear_actor_scheduled_sync, actor_key)

    def _clear_actor_scheduled_sync(self, actor_key: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, _scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=False,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()

    async def acquire_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        return await asyncio.to_thread(self._acquire_lease_sync, actor_key, worker_id, ttl_seconds)

    def _acquire_lease_sync(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            now = time.time()
            if lease_owner and lease_expires_at and lease_expires_at > now and lease_owner != worker_id:
                conn.commit()
                return False
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=worker_id,
                lease_expires_at=now + ttl_seconds,
            )
            conn.commit()
            return True

    async def heartbeat_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        return await asyncio.to_thread(self._heartbeat_lease_sync, actor_key, worker_id, ttl_seconds)

    def _heartbeat_lease_sync(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, _lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if lease_owner != worker_id:
                conn.commit()
                return False
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=worker_id,
                lease_expires_at=time.time() + ttl_seconds,
            )
            conn.commit()
            return True

    async def release_lease(self, actor_key: str, worker_id: str) -> None:
        await asyncio.to_thread(self._release_lease_sync, actor_key, worker_id)

    def _release_lease_sync(self, actor_key: str, worker_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events, inflight, scheduled, lease_owner, lease_expires_at = self._load_actor_state(
                conn,
                actor_key,
            )
            if lease_owner == worker_id:
                lease_owner = None
                lease_expires_at = None
            self._persist_actor_state(
                conn,
                actor_key=actor_key,
                events=events,
                inflight=inflight,
                scheduled=scheduled,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
            conn.commit()
