"""Tests for daily audit file-backed skill management."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.scenes.daily_audit.middleware import DailyAuditSessionMiddleware
from agent.scenes.daily_audit.models import AuditUnit, DailyAuditContext
from agent.scenes.daily_audit.persistence.direction import build_write_direction_archive_tool
from agent.scenes.daily_audit.persistence.store import reset_daily_audit_persistence_store
from agent.scenes.daily_audit.runtime.deepagents import reset_daily_audit_deepagents_runtime
from agent.scenes.daily_audit.selfevolution.tools import (
    build_direction_history_tool,
    build_exploration_memory_tool,
    build_skill_tools,
    list_skill_descriptors,
)


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()
    yield
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()


def _context(tmp_path) -> DailyAuditContext:
    return DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
        selected_unit=AuditUnit(
            unit_type="action_workflow",
            label="viewer workflow",
            file_path="src/viewer.cpp",
        ),
    )


def test_skill_tools_create_patch_and_delete_files_in_self_repo(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    skill_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills"
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout",
        lambda default_branch=None: self_repo_root,
    )

    skills_list, skill_view, skill_manage = build_skill_tools(
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
    )

    created = skill_manage(
        "create",
        "workflow-selection",
        content="---\nname: workflow-selection\ndescription: Pick one bounded workflow.\n---\n\nPrefer toolbar entrypoints first.\n",
    )
    skill_path = skill_root / "workflow-selection" / "SKILL.md"

    assert created["success"] is True
    assert skill_path.exists()
    assert "Prefer toolbar entrypoints first." in skill_path.read_text(encoding="utf-8")
    assert skill_view("workflow-selection")["source"] == "self_repo"
    assert any(item["name"] == "workflow-selection" for item in skills_list()["skills"])

    patched = skill_manage(
        "patch",
        "workflow-selection",
        old_string="toolbar entrypoints",
        new_string="menu and toolbar entrypoints",
    )
    assert patched["success"] is True
    assert "menu and toolbar entrypoints" in skill_path.read_text(encoding="utf-8")

    deleted = skill_manage("delete", "workflow-selection")
    assert deleted["success"] is True
    assert not skill_path.exists()


def test_skill_descriptors_ignore_project_repo_overrides(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    self_skill_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills" / "workflow-auditor"
    self_skill_root.mkdir(parents=True, exist_ok=True)
    (self_skill_root / "SKILL.md").write_text(
        "---\nname: workflow-auditor\ndescription: Service version.\n---\n\nBase guidance.\n",
        encoding="utf-8",
    )
    project_skill_root = tmp_path / "project-repo" / ".agents" / "skills" / "workflow-auditor"
    project_skill_root.mkdir(parents=True, exist_ok=True)
    (project_skill_root / "SKILL.md").write_text(
        "---\nname: workflow-auditor\ndescription: Project override.\n---\n\nProject-specific guidance.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout",
        lambda default_branch=None: self_repo_root,
    )

    descriptors = list_skill_descriptors(repo_dir=str(tmp_path / "project-repo"), default_branch="main")

    workflow = next(item for item in descriptors if item["name"] == "workflow-auditor")
    assert workflow["description"] == "Service version."
    assert workflow["source"] == "self_repo"


def test_skill_manage_ignores_project_repo_and_writes_self_repo(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers" / "workflow-auditor"
    shared_skill_root.mkdir(parents=True, exist_ok=True)
    shared_content = "---\nname: workflow-auditor\ndescription: Shared version.\n---\n\nShared baseline.\n"
    (shared_skill_root / "SKILL.md").write_text(shared_content, encoding="utf-8")
    project_skill_root = tmp_path / "project-repo" / ".agents" / "skills" / "workflow-auditor"
    project_skill_root.mkdir(parents=True, exist_ok=True)
    (project_skill_root / "SKILL.md").write_text(
        "---\nname: workflow-auditor\ndescription: Project override.\n---\n\nProject-specific guidance.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout",
        lambda default_branch=None: self_repo_root,
    )

    skills_list, skill_view, skill_manage = build_skill_tools(
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
    )
    result = skill_manage(
        "create",
        "workflow-auditor",
        content="---\nname: workflow-auditor\ndescription: New version.\n---\n\nReplacement body.\n",
    )

    assert result["success"] is True
    assert skill_view("workflow-auditor")["source"] == "self_repo"
    assert "Replacement body." in skill_view("workflow-auditor")["content"]
    assert (shared_skill_root / "SKILL.md").read_text(encoding="utf-8") == shared_content
    workflow = next(item for item in skills_list()["skills"] if item["name"] == "workflow-auditor")
    assert workflow["source"] == "self_repo"
    assert workflow["writable"] is True


def test_shared_skills_are_not_selfevolution_tool_descriptors(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers" / "branch-finish"
    shared_skill_root.mkdir(parents=True, exist_ok=True)
    (shared_skill_root / "SKILL.md").write_text(
        "---\nname: branch-finish\ndescription: Shared branch completion workflow.\n---\n\nShared baseline.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout",
        lambda default_branch=None: self_repo_root,
    )

    skills_list, skill_view, _skill_manage = build_skill_tools(
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
    )

    descriptors = list_skill_descriptors(repo_dir=str(tmp_path / "project-repo"), default_branch="main")
    assert all(item["name"] != "branch-finish" for item in descriptors)
    assert all(item["name"] != "branch-finish" for item in skills_list()["skills"])
    assert skill_view("branch-finish") == {"success": False, "error": "Skill 'branch-finish' not found"}


@pytest.mark.asyncio
async def test_session_middleware_normalizes_glob_path_none(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    context = _context(tmp_path)
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage="analysis",
        runtime_run_id=None,
        store=store,
        repo_dir=str(tmp_path / "project-repo"),
        sandbox=SimpleNamespace(root_dir="/workspace"),
    )

    request = SimpleNamespace(tool_call={"name": "glob", "args": {"pattern": "**/*.cpp", "path": None}})

    async def _handler(tool_request):
        assert tool_request.tool_call["args"]["path"] == "/"
        return SimpleNamespace(content="[]")

    await session_middleware.awrap_tool_call(request, _handler)


def test_direction_agent_no_longer_uses_memory_context_injection(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    context = DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
    )
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage="direction",
        runtime_run_id=None,
        store=store,
        repo_dir=str(tmp_path / "project-repo"),
        sandbox=SimpleNamespace(root_dir="/workspace"),
    )

    request = SimpleNamespace(messages=[SimpleNamespace(type="human", content="pick a direction")])

    def _handler(model_request):
        assert "memory-context" not in model_request.messages[0].content
        return SimpleNamespace()

    session_middleware.wrap_model_call(request, _handler)


@pytest.mark.asyncio
async def test_direction_session_after_agent_records_direction_archive(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    context = DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
    )
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage="direction",
        runtime_run_id=None,
        store=store,
        repo_dir=str(tmp_path / "project-repo"),
        sandbox=SimpleNamespace(root_dir="/workspace"),
    )

    enqueued = {}

    async def fake_enqueue(event):
        enqueued["event"] = event

    session_middleware._enqueue_direction_persistence = fake_enqueue

    await session_middleware.aafter_agent(
        {
            "structured_response": {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Zone Fill All",
                    "file_path": "pcbnew/tools/zone_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "PCB_ACTIONS::zoneFillAll",
                    "workflow_summary": "Fill all zones from the toolbar action and trace refill scheduling.",
                    "entry_evidence": [
                        "toolbar appends PCB_ACTIONS::zoneFillAll",
                    ],
                },
                "selection_reasoning": "This is bounded and likely to expose useful performance or correctness signal.",
            }
        },
        runtime=None,
    )

    assert enqueued["event"].event_type == "daily_audit_direction_persistence"
    assert enqueued["event"].payload["kind"] == "direction_archive"
    assert enqueued["event"].payload["run_id"] == "daily-run-1"
    assert enqueued["event"].payload["selection"]["selected_unit"]["label"] == "Zone Fill All"


@pytest.mark.asyncio
async def test_analysis_session_after_agent_enqueues_async_persistence_events_after_transcript_archive(tmp_path, monkeypatch):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    context = DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
        selected_unit=AuditUnit(
            unit_type="action_workflow",
            label="Zone Fill All",
            file_path="pcbnew/tools/zone_actions.cpp",
            entrypoint_kind="toolbar_action",
            entrypoint_symbol="PCB_ACTIONS::zoneFillAll",
            workflow_summary="Fill all zones from the toolbar action and trace refill scheduling.",
        ),
    )
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage="analysis",
        runtime_run_id=None,
        store=store,
        repo_dir=str(tmp_path / "project-repo"),
        sandbox=SimpleNamespace(root_dir="/workspace"),
    )
    enqueued = []

    async def fake_enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        "agent.scenes.daily_audit.middleware.session_lifecycle.archive_daily_audit_run_transcript",
        lambda **kwargs: True,
    )
    session_middleware._enqueue_short_term_persistence = fake_enqueue
    session_middleware._enqueue_long_term_persistence = fake_enqueue
    session_middleware._enqueue_skill_persistence = fake_enqueue

    await session_middleware.aafter_agent(
        {
            "structured_response": {
                "selected_unit": context.selected_unit.model_dump(mode="json"),
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }
        },
        runtime=None,
    )

    event_types = {event.event_type for event in enqueued}
    assert store.get_short_term_summary("team/project", "primary") == ""
    assert store.list_long_term_memory("team/project", limit=5) == []
    assert event_types == {
        "daily_audit_short_term_persistence",
        "daily_audit_long_term_persistence",
        "daily_audit_skill_persistence",
    }


@pytest.mark.asyncio
async def test_analysis_session_after_agent_skips_async_persistence_when_transcript_archive_fails(tmp_path, monkeypatch):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    context = DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=str(tmp_path / "project-repo"),
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
        selected_unit=AuditUnit(
            unit_type="action_workflow",
            label="Zone Fill All",
            file_path="pcbnew/tools/zone_actions.cpp",
            entrypoint_kind="toolbar_action",
            entrypoint_symbol="PCB_ACTIONS::zoneFillAll",
            workflow_summary="Fill all zones from the toolbar action and trace refill scheduling.",
        ),
    )
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage="analysis",
        runtime_run_id=None,
        store=store,
        repo_dir=str(tmp_path / "project-repo"),
        sandbox=SimpleNamespace(root_dir="/workspace"),
    )
    enqueued = []

    async def fake_enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr(
        "agent.scenes.daily_audit.middleware.session_lifecycle.archive_daily_audit_run_transcript",
        lambda **kwargs: False,
    )
    session_middleware._enqueue_short_term_persistence = fake_enqueue
    session_middleware._enqueue_long_term_persistence = fake_enqueue
    session_middleware._enqueue_skill_persistence = fake_enqueue

    await session_middleware.aafter_agent(
        {
            "structured_response": {
                "selected_unit": context.selected_unit.model_dump(mode="json"),
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }
        },
        runtime=None,
    )

    assert enqueued == []


def test_write_direction_archive_tool_persists_agent_generated_brief_and_keywords(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    selection_payload = {
        "selected_unit": {
            "unit_type": "action_workflow",
            "label": "Zone Fill All",
            "file_path": "pcbnew/tools/zone_actions.cpp",
            "entrypoint_kind": "toolbar_action",
            "entrypoint_symbol": "PCB_ACTIONS::zoneFillAll",
            "workflow_summary": "Fill all zones from the toolbar action and trace refill scheduling.",
            "entry_evidence": [
                "toolbar appends PCB_ACTIONS::zoneFillAll",
            ],
        },
        "selection_reasoning": "This is bounded and likely to expose useful performance or correctness signal.",
    }
    tool = build_write_direction_archive_tool(
        project_id="team/project",
        run_id="daily-run-1",
        store=store,
        selection_payload=selection_payload,
    )

    result = tool(
        archive_brief="Zone Fill All toolbar workflow with refill scheduling and zone recomputation sensitivity.",
        archive_keywords=["zone fill", "toolbar action", "refill scheduling"],
    )

    rows = store.search_direction_archives("team/project", "refill scheduling", limit=5)

    assert result["success"] is True
    assert rows[0]["run_id"] == "daily-run-1"
    assert rows[0]["direction_brief"].startswith("Zone Fill All toolbar workflow")


def test_direction_history_tool_returns_recent_and_matching_archives(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    store.record_direction_archive(
        "team/project",
        run_id="run-1",
        unit_type="action_workflow",
        unit_label="Zone Fill All",
        file_path="pcbnew/tools/zone_actions.cpp",
        entrypoint_kind="toolbar_action",
        entrypoint_symbol="PCB_ACTIONS::zoneFillAll",
        workflow_summary="Fill all zones from the toolbar action and trace refill scheduling.",
        selection_reasoning="Bounded workflow.",
        direction_brief="Zone Fill All toolbar workflow touching refill scheduling and zone recomputation.",
        keywords=["zone", "fill", "toolbar", "refill"],
        metadata={},
    )
    store.record_direction_archive(
        "team/project",
        run_id="run-2",
        unit_type="action_workflow",
        unit_label="3D Viewer",
        file_path="pcbnew/tools/viewer_actions.cpp",
        entrypoint_kind="menu_action",
        entrypoint_symbol="PCB_ACTIONS::show3DViewer",
        workflow_summary="Open the 3D viewer from the menu.",
        selection_reasoning="Bounded workflow.",
        direction_brief="3D Viewer menu workflow touching scene bootstrap.",
        keywords=["viewer", "3d", "menu"],
        metadata={},
    )

    tool = build_direction_history_tool(
        project_id="team/project",
        run_id="run-2",
        store=store,
    )

    recent = tool(limit=5)
    matched = tool(query="refill toolbar", limit=5)

    assert recent["count"] == 1
    assert recent["results"][0]["run_id"] == "run-1"
    assert matched["results"][0]["unit_label"] == "Zone Fill All"
    assert matched["results"][0]["keywords"] == ["zone", "fill", "toolbar", "refill"]


def test_exploration_memory_tool_returns_short_term_and_matching_long_term(tmp_path):
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore

    store = DailyAuditPersistenceStore(str(tmp_path / "controlplane.db"))
    store.upsert_short_term_summary("team/project", "primary", "Resume the parser hotspot audit.")
    store.add_long_term_memory(
        "team/project",
        memory_type="project_fact",
        content="Parser startup is sensitive to broad rewrites.",
        source_run_id="run-1",
    )

    tool = build_exploration_memory_tool(
        project_id="team/project",
        store=store,
    )

    result = tool(query="parser startup", limit=5)

    assert result["success"] is True
    assert result["short_term_summary"] == "Resume the parser hotspot audit."
    assert result["count"] == 1
    assert "broad rewrites" in result["results"][0]["content"]
