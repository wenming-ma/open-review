from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.config import settings
from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.runtime.store import InMemoryRuntimeStore
from agent.scenes.daily_audit.persistence import skill as skill_persistence
from agent.scenes.daily_audit.persistence.skill import (
    _review_prompt,
    run_daily_audit_skill_persistence,
    run_daily_audit_skill_review,
)
from agent.scenes.daily_audit.persistence.store import reset_daily_audit_persistence_store
from agent.scenes.daily_audit.runtime.deepagents import reset_daily_audit_deepagents_runtime


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()
    yield
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()


def test_run_daily_audit_skill_persistence_reuses_existing_reviewer_helper(tmp_path, monkeypatch):
    calls = {}
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-parent-run",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )

    async def fake_run_review(**kwargs):
        calls["review"] = kwargs
        return None

    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.skill.run_daily_audit_skill_review",
        fake_run_review,
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.skill.get_daily_audit_persistence_store",
        lambda: SimpleNamespace(get_run_transcript=lambda *_args, **_kwargs: {"content": "transcript"}),
    )

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "default_branch": "main",
            "repo_dir": str(tmp_path / "repo"),
            "flush": True,
            "parent_runtime_run_id": "runtime-parent-run",
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_skill_persistence(
            project_id="team/project",
            default_branch="main",
            event=event,
            runtime_run_id="runtime-skill",
        )
    )
    run = tracking.list_recent_runs(limit=1)[0]

    assert result.status == "reviewed"
    assert calls["review"]["project_id"] == "team/project"
    assert calls["review"]["run_id"] == "daily-run-1"
    assert calls["review"]["flush"] is True
    assert run["agent_records"][0]["record_kind"] == "daily_audit.skill_persistence"


def test_run_daily_audit_skill_persistence_stops_when_parent_run_was_terminated(tmp_path, monkeypatch):
    runtime_store = InMemoryRuntimeStore()
    __import__("asyncio").run(
        runtime_store.request_run_termination(
            "runtime-parent-run",
            actor_key="team/project!daily_audit",
            requested_by="admin",
        )
    )

    async def fake_runtime_store():
        return runtime_store

    monkeypatch.setattr(
        skill_persistence,
        "get_runtime_store",
        fake_runtime_store,
        raising=False,
    )

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "parent_runtime_run_id": "runtime-parent-run",
            "default_branch": "main",
            "repo_dir": "/tmp/repo",
            "flush": True,
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_skill_persistence(project_id="team/project", default_branch="main", event=event)
    )

    assert result.status == "terminated"
    assert result.reason == "parent_run_terminated"


def test_run_daily_audit_skill_review_registers_model_retry_middleware(tmp_path, monkeypatch):
    calls = {}

    class FakeAgent:
        async def ainvoke(self, _payload, config=None):
            calls["config"] = config
            return {"ok": True}

    def fake_create_deep_agent(**kwargs):
        calls["middleware"] = kwargs["middleware"]
        tool_names = [tool.__name__ for tool in kwargs["tools"]]
        assert tool_names == ["skills_list", "skill_view", "skill_manage"]
        assert any(isinstance(item, StructuredOutputRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ModelRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ToolErrorMiddleware) for item in kwargs["middleware"])
        return FakeAgent()

    monkeypatch.setattr("agent.scenes.daily_audit.persistence.skill.create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr("agent.scenes.daily_audit.persistence.skill.make_model", lambda *_args, **_kwargs: object())

    __import__("asyncio").run(
        run_daily_audit_skill_review(
            project_id="team/project",
            run_id="daily-run-1",
            repo_dir="/tmp/repo",
            transcript_text="important transcript",
            flush=False,
        )
    )

    assert calls["config"]["configurable"]["thread_id"].endswith(":skill-persistence")


def test_review_prompt_only_allows_generic_high_value_skills():
    prompt = _review_prompt(flush=True)

    assert "genuinely worth saving" in prompt
    assert "cross-project" in prompt
    assert "high-value" in prompt
    assert "Do not save one-off project facts" in prompt
    assert "When unsure, skip saving" in prompt


def test_run_daily_audit_skill_review_uses_strict_skill_screening_message(monkeypatch):
    calls = {}

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            calls["payload"] = payload
            calls["config"] = config
            return {"ok": True}

    def fake_create_deep_agent(**kwargs):
        calls["system_prompt"] = kwargs["system_prompt"]
        return FakeAgent()

    monkeypatch.setattr("agent.scenes.daily_audit.persistence.skill.create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr("agent.scenes.daily_audit.persistence.skill.make_model", lambda *_args, **_kwargs: object())

    __import__("asyncio").run(
        run_daily_audit_skill_review(
            project_id="team/project",
            run_id="daily-run-1",
            repo_dir="/tmp/repo",
            transcript_text="important transcript",
            flush=False,
        )
    )

    message = calls["payload"]["messages"][0]["content"]
    assert "Only save a skill when it captures a generic, reusable workflow" in message
    assert "Do not save one-off project facts" in message
    assert "When unsure, skip saving" in message


@pytest.mark.asyncio
async def test_skill_review_emits_phoenix_span(monkeypatch, tmp_path):
    spans = []
    events = []

    @contextmanager
    def _fake_span(name, **kwargs):
        spans.append((name, kwargs))
        yield SimpleNamespace(
            set_input=lambda value, mime_type=None: events.append(("input", value, mime_type)),
            set_output=lambda value, mime_type=None: events.append(("output", value, mime_type)),
            add_event=lambda name, attributes=None: events.append(("event", name, attributes)),
            record_exception=lambda exc: events.append(("exception", type(exc).__name__)),
            set_error_status=lambda description: events.append(("error", description)),
        )

    async def _fake_ainvoke(*args, **kwargs):
        return {"structured_response": None, "messages": []}

    monkeypatch.setattr(skill_persistence, "start_open_review_span", _fake_span)
    monkeypatch.setattr(
        skill_persistence,
        "create_deep_agent",
        lambda **kwargs: SimpleNamespace(ainvoke=_fake_ainvoke),
    )

    await skill_persistence.run_daily_audit_skill_review(
        project_id="team/project",
        run_id="run-1",
        repo_dir=str(tmp_path),
        transcript_text="useful transcript",
        flush=False,
    )

    assert spans == [
        (
            "open_review.daily_audit.skill_persistence",
            {
                "session_id": "daily_audit:team/project:run-1:skill-persistence",
                "attributes": {
                    "open_review.project_id": "team/project",
                    "open_review.session_id": "daily_audit:team/project:run-1:skill-persistence",
                    "open_review.flush": False,
                },
                "metadata": {"transcript_chars": len("useful transcript")},
                "tags": ["daily_audit", "skill-persistence"],
                "span_kind": "agent",
            },
        )
    ]
    assert any(item[0] == "input" for item in events)
    assert any(item[0] == "output" for item in events)
    assert any(item[0] == "event" and item[1] == "invoke_completed" for item in events)
