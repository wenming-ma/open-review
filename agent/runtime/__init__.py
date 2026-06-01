"""Durable runtime for MR-scoped agent execution."""

from agent.runtime.models import EventEnvelope, PublishReceipt, RunRecord
from agent.runtime.publish import GitLabPublishService
from agent.runtime.queue import enqueue_gitlab_event


def __getattr__(name: str):
    if name in {"drain_mr_actor", "run_sqlite_worker_forever"}:
        from agent.runtime import worker as worker_module

        return getattr(worker_module, name)
    raise AttributeError(name)

__all__ = [
    "EventEnvelope",
    "GitLabPublishService",
    "PublishReceipt",
    "RunRecord",
    "drain_mr_actor",
    "enqueue_gitlab_event",
    "run_sqlite_worker_forever",
]
