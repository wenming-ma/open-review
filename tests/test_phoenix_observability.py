"""Tests for optional Phoenix tracing integration."""

from __future__ import annotations

from contextlib import nullcontext
import sys
from types import SimpleNamespace
from types import ModuleType

from agent.config import settings
from agent.observability import phoenix


def test_configure_phoenix_tracing_registers_runtime(monkeypatch):
    calls: dict[str, object] = {}

    fake_phoenix = ModuleType("phoenix")
    fake_phoenix_otel = ModuleType("phoenix.otel")

    def fake_register(**kwargs):
        calls.update(kwargs)
        return object()

    fake_phoenix_otel.register = fake_register
    fake_phoenix.otel = fake_phoenix_otel
    monkeypatch.setitem(sys.modules, "phoenix", fake_phoenix)
    monkeypatch.setitem(sys.modules, "phoenix.otel", fake_phoenix_otel)

    monkeypatch.setattr(settings, "PHOENIX_TRACING_ENABLED", True)
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix.local:6006")
    monkeypatch.setattr(settings, "PHOENIX_API_KEY", "phoenix-api-key")
    monkeypatch.setattr(settings, "PHOENIX_PROJECT_NAME", "open-review")
    monkeypatch.setattr(phoenix, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(phoenix, "_TRACING_ENABLED", False, raising=False)

    assert phoenix.configure_phoenix_tracing() is True
    assert phoenix.phoenix_tracing_enabled() is True
    assert calls["project_name"] == "open-review"
    assert calls["auto_instrument"] is True
    assert calls["batch"] is False
    assert calls["endpoint"] == "http://phoenix.local:6006"


def test_build_phoenix_redirect_urls(monkeypatch):
    monkeypatch.setattr(settings, "PHOENIX_UI_BASE_URL", "http://phoenix.local:6006")

    assert (
        phoenix.build_phoenix_trace_url("trace-123")
        == "http://phoenix.local:6006/redirects/traces/trace-123"
    )
    assert (
        phoenix.build_phoenix_session_url("session-123")
        == "http://phoenix.local:6006/redirects/sessions/session-123"
    )


def test_build_open_review_trace_name_formats_auto_review_trace():
    assert (
        phoenix.build_open_review_trace_name(
            "auto_review",
            "root/kicad!20",
            head_sha="99939c3ab55817f76d1c322a2ea2428c0a0d3a7d",
            run_key="20260413175240-dc43964f",
        )
        == "auto_review root/kicad!20 @99939c3a [dc43964f]"
    )


def test_build_open_review_trace_name_formats_mention_trace():
    assert (
        phoenix.build_open_review_trace_name(
            "mention",
            "root/kicad!20",
            note_id=1234,
            head_sha="99939c3ab55817f76d1c322a2ea2428c0a0d3a7d",
            run_key="mention-run-abcdef12",
        )
        == "mention root/kicad!20 note#1234 @99939c3a [abcdef12]"
    )


def test_start_open_review_span_uses_openinference_span_helpers(monkeypatch):
    import openinference.instrumentation as oi
    import opentelemetry.trace as otel_trace

    calls: dict[str, object] = {}

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}
            self.events: list[tuple[str, dict[str, object] | None]] = []
            self.inputs: list[tuple[object, str | None]] = []
            self.outputs: list[tuple[object, str | None]] = []

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

        def add_event(self, name: str, attributes: dict[str, object] | None = None) -> None:
            self.events.append((name, attributes))

        def set_input(self, value: object, *, mime_type: str | None = None) -> None:
            self.inputs.append((value, mime_type))

        def set_output(self, value: object, *, mime_type: str | None = None) -> None:
            self.outputs.append((value, mime_type))

        def get_span_context(self):
            return SimpleNamespace(trace_id=0x1234)

        def record_exception(self, _exc: Exception) -> None:
            return None

        def set_status(self, _status) -> None:
            return None

    fake_span = _FakeSpan()

    class _FakeOITracer:
        def __init__(self, tracer, config) -> None:
            calls["wrapped_tracer"] = tracer
            calls["config"] = config

        def start_as_current_span(self, name: str, **kwargs):
            calls["span_name"] = name
            calls["span_kwargs"] = kwargs

            class _Manager:
                def __enter__(self_inner):
                    return fake_span

                def __exit__(self_inner, exc_type, exc, tb):
                    return False

            return _Manager()

    monkeypatch.setattr(phoenix, "_TRACING_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PHOENIX_UI_BASE_URL", "http://phoenix.local:6006")
    monkeypatch.setattr(oi, "OITracer", _FakeOITracer)
    monkeypatch.setattr(oi, "TraceConfig", lambda: "trace-config")
    monkeypatch.setattr(oi, "using_session", lambda session_id: nullcontext())
    monkeypatch.setattr(oi, "using_user", lambda user_id: nullcontext())
    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: "otel-tracer")

    with phoenix.start_open_review_span(
        "open_review.auto_review.director",
        session_id="team/project!42",
        user_id="dev",
        attributes={"open_review.project_id": "team/project", "open_review.mr_iid": 42},
        metadata={"changed_files": ["src/main.cc"]},
        tags=["auto_review", "director"],
        span_kind="agent",
    ) as ctx:
        ctx.set_input({"messages": [{"role": "user", "content": "review this MR"}]})
        ctx.set_output({"summary": "ok"})
        ctx.add_event("invoke_completed", {"structured_response_present": True})

    assert calls["wrapped_tracer"] == "otel-tracer"
    assert calls["config"] == "trace-config"
    assert calls["span_name"] == "open_review.auto_review.director"
    assert calls["span_kwargs"] == {"openinference_span_kind": "agent"}
    assert fake_span.inputs == [
        ({"messages": [{"role": "user", "content": "review this MR"}]}, None)
    ]
    assert fake_span.outputs == [({"summary": "ok"}, None)]
    assert fake_span.events == [
        ("invoke_completed", {"structured_response_present": True})
    ]
    assert fake_span.attributes["open_review.project_id"] == "team/project"
    assert fake_span.attributes["open_review.mr_iid"] == 42
    assert fake_span.attributes["open_review.metadata"] == '{"changed_files": ["src/main.cc"]}'
    assert fake_span.attributes["open_review.tags"] == '["auto_review", "director"]'
