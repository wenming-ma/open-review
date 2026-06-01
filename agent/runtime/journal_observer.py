"""Context-scoped runtime journal observation helpers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class RuntimeJournalObserver:
    """Async sink used to mirror fine-grained runtime observations into the run journal."""

    record: Callable[[str | None, str, str, str, dict[str, Any]], Awaitable[None]]
    default_stage_key: str = "scene_execute"
    default_event_type: str = "observation"
    default_status: str = "running"


_CURRENT_OBSERVER: ContextVar[RuntimeJournalObserver | None] = ContextVar(
    "open_review_runtime_journal_observer",
    default=None,
)
_CURRENT_SCOPE_DETAILS: ContextVar[dict[str, Any]] = ContextVar(
    "open_review_runtime_journal_scope_details",
    default={},
)


@contextmanager
def bind_runtime_journal_observer(observer: RuntimeJournalObserver):
    token = _CURRENT_OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _CURRENT_OBSERVER.reset(token)


@contextmanager
def runtime_observation_scope(**details: Any):
    current = dict(_CURRENT_SCOPE_DETAILS.get() or {})
    current.update({key: value for key, value in details.items() if value is not None})
    token = _CURRENT_SCOPE_DETAILS.set(current)
    try:
        yield current
    finally:
        _CURRENT_SCOPE_DETAILS.reset(token)


def _merged_details(details: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(_CURRENT_SCOPE_DETAILS.get() or {})
    merged.update({key: value for key, value in (details or {}).items() if value is not None})
    return merged


async def record_runtime_observation(
    summary: str,
    *,
    stage_key: str | None = None,
    details: dict[str, Any] | None = None,
    status: str | None = None,
    event_type: str | None = None,
) -> bool:
    observer = _CURRENT_OBSERVER.get()
    if observer is None:
        return False
    await observer.record(
        stage_key or observer.default_stage_key,
        event_type or observer.default_event_type,
        status or observer.default_status,
        summary,
        _merged_details(details),
    )
    return True


def schedule_runtime_observation(
    summary: str,
    *,
    stage_key: str | None = None,
    details: dict[str, Any] | None = None,
    status: str | None = None,
    event_type: str | None = None,
) -> bool:
    observer = _CURRENT_OBSERVER.get()
    if observer is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(
        record_runtime_observation(
            summary,
            stage_key=stage_key,
            details=details,
            status=status,
            event_type=event_type,
        )
    )
    return True
