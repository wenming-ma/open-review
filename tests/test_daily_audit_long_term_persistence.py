from __future__ import annotations

from types import SimpleNamespace

from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.config import settings
from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.runtime.store import InMemoryRuntimeStore
from agent.scenes.daily_audit.persistence.long_term import run_daily_audit_long_term_persistence
from agent.scenes.daily_audit.persistence.store import (
    DailyAuditPersistenceStore,
    reset_daily_audit_persistence_store,
)


def test_run_daily_audit_long_term_persistence_uses_single_write_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_daily_audit_persistence_store()
    reset_controlplane_services()
    calls = {}
    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    store.get_run_transcript = lambda *_args, **_kwargs: {"content": "transcript"}  # type: ignore[method-assign]
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

    class FakeAgent:
        async def ainvoke(self, _payload, config=None):
            calls["config"] = config
            tool = calls["tools"][0]
            tool(
                [
                    "这个工作流的主要风险集中在单一根因，适合一轮只追一个问题。",
                    "性能类结论需要脚本或基准支撑，不能只靠口头分析。",
                ]
            )
            return {"ok": True}

    def fake_create_deep_agent(**kwargs):
        calls["tools"] = kwargs["tools"]
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0].__name__ == "write_long_term_memory"
        assert any(isinstance(item, StructuredOutputRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ModelRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ToolErrorMiddleware) for item in kwargs["middleware"])
        return FakeAgent()

    monkeypatch.setattr("agent.scenes.daily_audit.persistence.long_term.create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.long_term.get_daily_audit_persistence_store",
        lambda: store,
    )

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "parent_runtime_run_id": "runtime-parent-run",
            "session_id": "daily_audit:team/project:daily-run-1:primary",
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_long_term_persistence(
            project_id="team/project",
            default_branch="main",
            event=event,
            runtime_run_id="runtime-long-term",
        )
    )

    memories = store.list_long_term_memory("team/project", limit=10)
    run = tracking.list_recent_runs(limit=1)[0]
    assert result.status == "persisted"
    assert len(memories) == 2
    assert calls["config"]["configurable"]["thread_id"].endswith(":long-term-persistence")
    assert run["agent_records"][0]["record_kind"] == "daily_audit.long_term_persistence"


def test_run_daily_audit_long_term_persistence_stops_when_parent_run_was_terminated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
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
        "agent.scenes.daily_audit.persistence.long_term.get_runtime_store",
        fake_runtime_store,
        raising=False,
    )

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "parent_runtime_run_id": "runtime-parent-run",
            "session_id": "daily_audit:team/project:daily-run-1:primary",
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_long_term_persistence(project_id="team/project", default_branch="main", event=event)
    )

    assert result.status == "terminated"
    assert result.reason == "parent_run_terminated"
