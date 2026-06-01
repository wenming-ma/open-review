"""Optional fail-open Phoenix tracing integration."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import quote

from agent.config import settings

logger = logging.getLogger(__name__)

_BOOTSTRAPPED = False
_TRACING_ENABLED = False
_CURRENT_TRACE: ContextVar["OpenReviewTraceContext | None"] = ContextVar("open_review_current_trace", default=None)


@dataclass
class OpenReviewTraceContext:
    trace_id: str | None = None
    trace_url: str | None = None
    session_id: str | None = None
    session_url: str | None = None
    span: Any | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        if self.span is None:
            return
        _set_attribute(self.span, key, value)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        if self.span is None or not hasattr(self.span, "add_event"):
            return
        payload = {
            key: _normalize_attribute_value(value)
            for key, value in (attributes or {}).items()
            if value is not None
        }
        self.span.add_event(name, attributes=payload or None)

    def set_input(self, value: Any, *, mime_type: str | None = None) -> None:
        if self.span is None:
            return
        setter = getattr(self.span, "set_input", None)
        if callable(setter):
            setter(value, mime_type=mime_type)

    def set_output(self, value: Any, *, mime_type: str | None = None) -> None:
        if self.span is None:
            return
        setter = getattr(self.span, "set_output", None)
        if callable(setter):
            setter(value, mime_type=mime_type)

    def record_exception(self, exc: Exception) -> None:
        if self.span is None or not hasattr(self.span, "record_exception"):
            return
        self.span.record_exception(exc)

    def set_error_status(self, description: str) -> None:
        if self.span is None or not hasattr(self.span, "set_status"):
            return
        try:
            from opentelemetry.trace import Status, StatusCode
        except Exception:
            return
        self.span.set_status(Status(StatusCode.ERROR, description))


def _base_url() -> str:
    return (settings.PHOENIX_UI_BASE_URL or settings.PHOENIX_COLLECTOR_ENDPOINT or "").rstrip("/")


def build_phoenix_trace_url(trace_id: str | None) -> str | None:
    if not trace_id or not _base_url():
        return None
    return f"{_base_url()}/redirects/traces/{quote(trace_id, safe='')}"


def build_phoenix_session_url(session_id: str | None) -> str | None:
    if not session_id or not _base_url():
        return None
    return f"{_base_url()}/redirects/sessions/{quote(session_id, safe='')}"


def _short_trace_token(value: str | None) -> str | None:
    token = (value or "").strip()
    if not token:
        return None
    for separator in (":", "-"):
        if separator in token:
            token = token.rsplit(separator, 1)[-1].strip()
    return token[:8] or None


def build_open_review_trace_name(
    scene: str,
    actor_key: str,
    *,
    head_sha: str | None = None,
    run_key: str | None = None,
    note_id: int | None = None,
) -> str:
    parts = [scene, actor_key]
    if note_id is not None:
        parts.append(f"note#{note_id}")
    if head_sha:
        parts.append(f"@{head_sha[:8]}")
    short_run = _short_trace_token(run_key)
    if short_run:
        parts.append(f"[{short_run}]")
    return " ".join(parts)


def phoenix_tracing_enabled() -> bool:
    return _TRACING_ENABLED


def configure_phoenix_tracing() -> bool:
    """Best-effort Phoenix setup.

    If Phoenix is disabled, misconfigured, or the optional tracing packages are
    unavailable, the business workflow must keep running.
    """

    global _BOOTSTRAPPED, _TRACING_ENABLED
    if _BOOTSTRAPPED:
        return _TRACING_ENABLED

    _BOOTSTRAPPED = True
    _TRACING_ENABLED = False

    if not settings.PHOENIX_TRACING_ENABLED:
        return False
    if not settings.PHOENIX_COLLECTOR_ENDPOINT or not settings.PHOENIX_API_KEY:
        logger.warning("Phoenix tracing enabled but endpoint/API key are incomplete; skipping bootstrap")
        return False

    try:
        from phoenix.otel import register
    except Exception:
        logger.warning("Phoenix tracing requested but optional Phoenix packages are unavailable")
        return False

    try:
        register(
            project_name=settings.PHOENIX_PROJECT_NAME,
            auto_instrument=True,
            batch=False,
            endpoint=settings.PHOENIX_COLLECTOR_ENDPOINT,
            api_key=settings.PHOENIX_API_KEY,
        )
    except Exception:
        logger.warning("Phoenix tracing bootstrap failed; continuing without tracing", exc_info=True)
        return False

    logger.info(
        "Phoenix tracing configured for project=%s endpoint=%s",
        settings.PHOENIX_PROJECT_NAME,
        settings.PHOENIX_COLLECTOR_ENDPOINT,
    )
    _TRACING_ENABLED = True
    return True


def _normalize_attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=isinstance(value, dict))


def _set_attribute(span, key: str, value: Any) -> None:
    span.set_attribute(key, _normalize_attribute_value(value))


def _set_span_attributes(span, attributes: dict[str, Any] | None, metadata: dict[str, Any] | None, tags: list[str] | None) -> None:
    if not attributes and not metadata and not tags:
        return

    for key, value in (attributes or {}).items():
        if value is None:
            continue
        _set_attribute(span, key, value)

    if metadata:
        _set_attribute(span, "open_review.metadata", metadata)
    if tags:
        _set_attribute(span, "open_review.tags", tags)


@contextmanager
def start_open_review_span(
    name: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    span_kind: str | None = None,
) -> Iterator[OpenReviewTraceContext]:
    """Start a best-effort tracing span without making business logic depend on Phoenix."""

    parent = _CURRENT_TRACE.get()
    effective_session_id = session_id or (parent.session_id if parent else None)
    fallback = OpenReviewTraceContext(
        session_id=effective_session_id,
        session_url=build_phoenix_session_url(effective_session_id),
    )

    if not phoenix_tracing_enabled():
        token = _CURRENT_TRACE.set(fallback)
        try:
            yield fallback
        finally:
            _CURRENT_TRACE.reset(token)
        return

    try:
        from openinference.instrumentation import OITracer, TraceConfig, using_session, using_user
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
    except Exception:
        logger.warning("Phoenix tracing is enabled but tracing helpers are unavailable; using no-op span")
        token = _CURRENT_TRACE.set(fallback)
        try:
            yield fallback
        finally:
            _CURRENT_TRACE.reset(token)
        return

    base_tracer = trace.get_tracer("open-review")
    tracer = base_tracer if isinstance(base_tracer, OITracer) else OITracer(base_tracer, TraceConfig())
    with ExitStack() as stack:
        if effective_session_id:
            stack.enter_context(using_session(session_id=effective_session_id))
        if user_id:
            stack.enter_context(using_user(user_id=user_id))
        with tracer.start_as_current_span(name, openinference_span_kind=span_kind) as span:
            _set_span_attributes(span, attributes, metadata, tags)
            trace_id = format(span.get_span_context().trace_id, "032x")
            ctx = OpenReviewTraceContext(
                trace_id=trace_id,
                trace_url=build_phoenix_trace_url(trace_id),
                session_id=effective_session_id,
                session_url=build_phoenix_session_url(effective_session_id),
                span=span,
            )
            token = _CURRENT_TRACE.set(ctx)
            try:
                yield ctx
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                _CURRENT_TRACE.reset(token)


def current_trace_identifiers() -> OpenReviewTraceContext:
    current = _CURRENT_TRACE.get()
    if current is not None:
        return current

    fallback = OpenReviewTraceContext()
    if not phoenix_tracing_enabled():
        return fallback

    try:
        from opentelemetry import trace
    except Exception:
        return fallback

    span = trace.get_current_span()
    span_context = span.get_span_context()
    if not getattr(span_context, "is_valid", False):
        return fallback

    trace_id = format(span_context.trace_id, "032x")
    return OpenReviewTraceContext(
        trace_id=trace_id,
        trace_url=build_phoenix_trace_url(trace_id),
        session_id=current.session_id if current else None,
        session_url=build_phoenix_session_url(current.session_id) if current and current.session_id else None,
    )
