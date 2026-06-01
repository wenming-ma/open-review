"""Model-call retry middleware for transient upstream failures."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from agent.observability import current_trace_identifiers
from agent.runtime.journal_observer import schedule_runtime_observation

logger = logging.getLogger(__name__)

_DEFAULT_RETRY_DELAYS_SECONDS = (
    5.0,
    10.0,
    30.0,
    60.0,
    60.0 * 60.0,
)
_DEFAULT_MAX_DELAY_SECONDS = 5 * 60.0 * 60.0
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "RateLimitError",
    "TimeoutException",
    "WriteTimeout",
}


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _is_retryable_model_error(exc: Exception) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return False

    status = _status_code(exc)
    if status in _RETRYABLE_STATUS_CODES or (status is not None and status >= 500):
        return True

    if exc.__class__.__name__ in _RETRYABLE_ERROR_NAMES:
        return True

    message = str(exc).lower()
    return "rate limit" in message or "too many requests" in message


class ModelRetryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Retry transient model failures with long capped backoff."""

    def __init__(
        self,
        *,
        delays_seconds: Sequence[float] = _DEFAULT_RETRY_DELAYS_SECONDS,
        max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS,
        max_retry_attempts: int | None = None,
        sleep_sync: Callable[[float], None] = time.sleep,
        sleep_async: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._delays_seconds = tuple(delays_seconds)
        self._max_delay_seconds = max_delay_seconds
        self._max_retry_attempts = max_retry_attempts
        self._sleep_sync = sleep_sync
        self._sleep_async = sleep_async

    def _retry_delay_seconds(self, retry_attempt: int) -> float:
        if retry_attempt <= 0:
            raise ValueError("retry_attempt must be positive")
        if retry_attempt <= len(self._delays_seconds):
            return self._delays_seconds[retry_attempt - 1]
        return self._max_delay_seconds

    @staticmethod
    def _record_retry_event(retry_attempt: int, delay_seconds: float, exc: Exception) -> None:
        details = {
            "retry_attempt": retry_attempt,
            "retry_delay_seconds": delay_seconds,
            "retry_error_type": exc.__class__.__name__,
            "retry_error_message": str(exc),
        }
        current_trace_identifiers().add_event(
            "model_retry_scheduled",
            details,
        )
        schedule_runtime_observation(
            "model_retry_scheduled",
            details=details,
        )

    def wrap_model_call(self, request, handler):
        retry_attempt = 0
        while True:
            try:
                return handler(request)
            except Exception as exc:
                if not _is_retryable_model_error(exc):
                    raise
                retry_attempt += 1
                if self._max_retry_attempts is not None and retry_attempt > self._max_retry_attempts:
                    raise
                delay_seconds = self._retry_delay_seconds(retry_attempt)
                logger.warning(
                    "Retrying model call after %s attempt=%d delay=%ss error=%s",
                    exc.__class__.__name__,
                    retry_attempt,
                    delay_seconds,
                    exc,
                )
                self._record_retry_event(retry_attempt, delay_seconds, exc)
                self._sleep_sync(delay_seconds)

    async def awrap_model_call(self, request, handler):
        retry_attempt = 0
        while True:
            try:
                return await handler(request)
            except Exception as exc:
                if not _is_retryable_model_error(exc):
                    raise
                retry_attempt += 1
                if self._max_retry_attempts is not None and retry_attempt > self._max_retry_attempts:
                    raise
                delay_seconds = self._retry_delay_seconds(retry_attempt)
                logger.warning(
                    "Retrying async model call after %s attempt=%d delay=%ss error=%s",
                    exc.__class__.__name__,
                    retry_attempt,
                    delay_seconds,
                    exc,
                )
                self._record_retry_event(retry_attempt, delay_seconds, exc)
                await self._sleep_async(delay_seconds)
