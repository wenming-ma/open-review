from __future__ import annotations

import asyncio

import pytest

from agent.middleware.model_retry import ModelRetryMiddleware
from agent.runtime.journal_observer import (
    RuntimeJournalObserver,
    bind_runtime_journal_observer,
    runtime_observation_scope,
)


class _RateLimitError(Exception):
    status_code = 429


class _TimeoutError(Exception):
    pass


@pytest.mark.asyncio
async def test_model_retry_middleware_retries_rate_limit_errors(monkeypatch):
    sleeps: list[float] = []
    events: list[tuple[str, dict[str, object] | None]] = []
    attempts = {"count": 0}

    class _TraceContext:
        def add_event(self, name: str, attributes=None) -> None:
            events.append((name, attributes))

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    async def _handler(_request):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _RateLimitError("rate limit")
        return "ok"

    monkeypatch.setattr(
        "agent.middleware.model_retry.current_trace_identifiers",
        lambda: _TraceContext(),
    )

    middleware = ModelRetryMiddleware(
        delays_seconds=(5.0, 10.0),
        max_retry_attempts=2,
        sleep_async=_sleep,
    )

    result = await middleware.awrap_model_call(object(), _handler)

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleeps == [5.0, 10.0]
    assert events == [
        (
            "model_retry_scheduled",
            {
                "retry_attempt": 1,
                "retry_delay_seconds": 5.0,
                "retry_error_type": "_RateLimitError",
                "retry_error_message": "rate limit",
            },
        ),
        (
            "model_retry_scheduled",
            {
                "retry_attempt": 2,
                "retry_delay_seconds": 10.0,
                "retry_error_type": "_RateLimitError",
                "retry_error_message": "rate limit",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_model_retry_middleware_does_not_retry_non_retryable_errors():
    attempts = {"count": 0}

    async def _handler(_request):
        attempts["count"] += 1
        raise ValueError("schema mismatch")

    middleware = ModelRetryMiddleware(
        delays_seconds=(5.0,),
        max_retry_attempts=3,
    )

    with pytest.raises(ValueError, match="schema mismatch"):
        await middleware.awrap_model_call(object(), _handler)

    assert attempts["count"] == 1


def test_model_retry_middleware_caps_retry_delay_at_five_hours():
    middleware = ModelRetryMiddleware(delays_seconds=(300.0, 600.0), max_delay_seconds=18_000.0)

    assert middleware._retry_delay_seconds(1) == 300.0
    assert middleware._retry_delay_seconds(2) == 600.0
    assert middleware._retry_delay_seconds(3) == 18_000.0
    assert middleware._retry_delay_seconds(20) == 18_000.0


def test_model_retry_middleware_uses_updated_default_backoff_schedule():
    middleware = ModelRetryMiddleware()

    assert middleware._retry_delay_seconds(1) == 5.0
    assert middleware._retry_delay_seconds(2) == 10.0
    assert middleware._retry_delay_seconds(3) == 30.0
    assert middleware._retry_delay_seconds(4) == 60.0
    assert middleware._retry_delay_seconds(5) == 3600.0
    assert middleware._retry_delay_seconds(6) == 18_000.0


@pytest.mark.asyncio
async def test_model_retry_middleware_emits_runtime_observation_with_bound_scope(monkeypatch):
    sleeps: list[float] = []
    observations: list[tuple[str | None, str, str, str, dict[str, object]]] = []
    attempts = {"count": 0}

    class _TraceContext:
        def add_event(self, name: str, attributes=None) -> None:
            return None

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    async def _record(stage_key, event_type, status, summary, details):
        observations.append((stage_key, event_type, status, summary, details))

    async def _handler(_request):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise _RateLimitError("rate limit")
        return "ok"

    monkeypatch.setattr(
        "agent.middleware.model_retry.current_trace_identifiers",
        lambda: _TraceContext(),
    )

    middleware = ModelRetryMiddleware(
        delays_seconds=(5.0,),
        max_retry_attempts=1,
        sleep_async=_sleep,
    )

    with bind_runtime_journal_observer(RuntimeJournalObserver(record=_record)), runtime_observation_scope(
        mention_role="author",
        mention_round=1,
    ):
        result = await middleware.awrap_model_call(object(), _handler)
        await asyncio.sleep(0)

    assert result == "ok"
    assert sleeps == [5.0]
    assert observations == [
        (
            "scene_execute",
            "observation",
            "running",
            "model_retry_scheduled",
            {
                "mention_role": "author",
                "mention_round": 1,
                "retry_attempt": 1,
                "retry_delay_seconds": 5.0,
                "retry_error_type": "_RateLimitError",
                "retry_error_message": "rate limit",
            },
        )
    ]
