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
from agent.scenes.daily_audit.persistence.direction import run_daily_audit_direction_persistence
from agent.scenes.daily_audit.persistence.store import (
    DailyAuditPersistenceStore,
    reset_daily_audit_persistence_store,
)


def test_run_daily_audit_direction_persistence_uses_single_write_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_daily_audit_persistence_store()
    reset_controlplane_services()
    calls = {}
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
                archive_brief="Zone Fill All toolbar workflow with refill scheduling sensitivity.",
                archive_keywords=["zone fill", "toolbar action", "refill scheduling"],
            )
            return {"ok": True}

    def fake_create_deep_agent(**kwargs):
        calls["tools"] = kwargs["tools"]
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0].__name__ == "write_direction_archive"
        assert kwargs["tools"][0].__doc__ == "Persist the final direction archive for this run."
        assert any(isinstance(item, StructuredOutputRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ModelRetryMiddleware) for item in kwargs["middleware"])
        assert any(isinstance(item, ToolErrorMiddleware) for item in kwargs["middleware"])
        return FakeAgent()

    monkeypatch.setattr("agent.scenes.daily_audit.persistence.direction.create_deep_agent", fake_create_deep_agent)

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "parent_runtime_run_id": "runtime-parent-run",
            "session_id": "daily_audit:team/project:daily-run-1:primary",
            "selection": {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Zone Fill All",
                    "file_path": "pcbnew/tools/zone_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "PCB_ACTIONS::zoneFillAll",
                    "workflow_summary": "Fill all zones from the toolbar action and trace refill scheduling.",
                    "entry_evidence": ["toolbar appends PCB_ACTIONS::zoneFillAll"],
                },
                "selection_reasoning": "Bounded workflow.",
            },
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_direction_persistence(project_id="team/project", default_branch="main", event=event)
    )

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    rows = store.search_direction_archives("team/project", "refill scheduling", limit=5)
    run = tracking.list_recent_runs(limit=1)[0]

    assert result.status == "persisted"
    assert rows[0]["run_id"] == "daily-run-1"
    assert rows[0]["unit_label"] == "Zone Fill All"
    assert calls["config"]["configurable"]["thread_id"].endswith(":direction-persistence")
    assert run["agent_records"][0]["record_kind"] == "daily_audit.direction_persistence"


def test_run_daily_audit_direction_persistence_stops_when_parent_run_was_terminated(tmp_path, monkeypatch):
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
        "agent.scenes.daily_audit.persistence.direction.get_runtime_store",
        fake_runtime_store,
        raising=False,
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.direction.create_deep_agent",
        lambda **_kwargs: SimpleNamespace(
            ainvoke=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("terminated parent runs should not invoke the persistence agent")
            )
        ),
    )

    event = SimpleNamespace(
        payload={
            "run_id": "daily-run-1",
            "parent_runtime_run_id": "runtime-parent-run",
            "session_id": "daily_audit:team/project:daily-run-1:primary",
            "selection": {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Zone Fill All",
                    "file_path": "pcbnew/tools/zone_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "PCB_ACTIONS::zoneFillAll",
                    "workflow_summary": "Fill all zones from the toolbar action and trace refill scheduling.",
                    "entry_evidence": ["toolbar appends PCB_ACTIONS::zoneFillAll"],
                },
                "selection_reasoning": "Bounded workflow.",
            },
        }
    )

    result = __import__("asyncio").run(
        run_daily_audit_direction_persistence(project_id="team/project", default_branch="main", event=event)
    )

    assert result.status == "terminated"
    assert result.reason == "parent_run_terminated"
