"""Queue helpers for durable MR actor execution."""

from __future__ import annotations

import asyncio
import logging
import os

from agent.runtime.models import EventEnvelope
from agent.runtime.store import RuntimeStore, SQLiteRuntimeStore

logger = logging.getLogger(__name__)

_STORE: RuntimeStore | None = None
_QUEUE = None
MR_ACTOR_JOB_NAME = "drain_mr_actor_job"


class ImmediateJobQueue:
    """In-process queue used by the single SQLite-backed worker."""

    async def enqueue_job(self, job_name: str, *args):
        if job_name != MR_ACTOR_JOB_NAME:
            raise ValueError(f"Unsupported in-process job: {job_name}")
        from agent.runtime.worker import drain_mr_actor

        actor_key = args[0]
        asyncio.create_task(drain_mr_actor(actor_key))


class PassiveJobQueue:
    """No-op queue used by non-worker processes in SQLite mode."""

    async def enqueue_job(self, job_name: str, *args):
        del job_name, args
        return None

async def get_runtime_store() -> RuntimeStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    from agent.config import settings

    _STORE = await SQLiteRuntimeStore.from_path(settings.current_snapshot().OPEN_REVIEW_DB_PATH)
    return _STORE


async def get_job_queue():
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    _QUEUE = ImmediateJobQueue() if os.environ.get("OPEN_REVIEW_RUNTIME_ROLE") == "worker" else PassiveJobQueue()
    return _QUEUE


def reset_runtime_clients() -> None:
    global _STORE, _QUEUE
    _STORE = None
    _QUEUE = None


async def resume_runtime_processing(*, store: RuntimeStore | None = None, queue=None) -> int:
    """Resume actors with persisted pending or inflight work."""
    store = store or await get_runtime_store()
    queue = queue or await get_job_queue()
    statuses = await store.list_actor_statuses()
    scheduled = 0
    for status in statuses:
        if not (status.pending_count or status.inflight_count or status.scheduled):
            continue
        await store.clear_actor_scheduled(status.actor_key)
        if await store.mark_actor_scheduled(status.actor_key):
            await queue.enqueue_job(MR_ACTOR_JOB_NAME, status.actor_key)
            scheduled += 1
    return scheduled


async def enqueue_gitlab_event(
    event: EventEnvelope,
    *,
    store: RuntimeStore | None = None,
    queue=None,
) -> str:
    store = store or await get_runtime_store()
    queue = queue or await get_job_queue()
    appended = await store.append_event(event)
    if not appended:
        logger.info("Skipping duplicate event %s for actor %s", event.event_id, event.actor_key)
        return event.actor_key
    if await store.mark_actor_scheduled(event.actor_key):
        await queue.enqueue_job(MR_ACTOR_JOB_NAME, event.actor_key)
        logger.info("Scheduled durable actor drain for %s via %s", event.actor_key, event.event_id)
    else:
        logger.info("Queued event %s for busy actor %s", event.event_id, event.actor_key)
    return event.actor_key
