"""Tests for the daily audit orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.runtime.termination import RunTerminationRequested
from agent.scenes.daily_audit import orchestrator
from agent.scenes.daily_audit.models import (
    AuditCandidate,
    DailyAuditContext,
    DailyAuditSelectionResponse,
)
from agent.scenes.daily_audit.persistence.store import reset_daily_audit_persistence_store
from agent.scenes.daily_audit.runtime.deepagents import reset_daily_audit_deepagents_runtime


def _context(repo_dir: str) -> DailyAuditContext:
    return DailyAuditContext(
        project_id="team/project",
        actor_key="team/project!daily_audit",
        repo_dir=repo_dir,
        default_branch="main",
        run_id="daily-run-1",
        session_id="daily_audit:team/project:daily-run-1:primary",
        experiment_root="/tmp/sandbox/.open-review-daily-audit/daily-run-1",
        candidates=[
            AuditCandidate(
                unit_type="function",
                label="foo()",
                file_path="src/foo.c",
                rationale="hot function",
            )
        ],
    )


def test_is_safe_autofix_enforces_changed_file_and_line_limits(monkeypatch):
    monkeypatch.setattr(orchestrator.settings, "DAILY_AUDIT_ENABLE_AUTOFIX", True)
    monkeypatch.setattr(orchestrator.settings, "DAILY_AUDIT_MAX_CHANGED_FILES", 5)
    monkeypatch.setattr(orchestrator.settings, "DAILY_AUDIT_MAX_CHANGED_LINES", 200)

    safe, reason = orchestrator._is_safe_autofix(
        used_subagents=[],
        changed_files=["a.cpp", "b.cpp", "c.cpp", "d.cpp", "e.cpp", "f.cpp"],
        changed_line_count=100,
    )

    assert safe is False
    assert reason == "changed_file_limit_exceeded"

    safe, reason = orchestrator._is_safe_autofix(
        used_subagents=[],
        changed_files=["a.cpp"],
        changed_line_count=201,
    )

    assert safe is False
    assert reason == "changed_line_limit_exceeded"


@pytest.fixture(autouse=True)
def _reset_daily_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda _sandbox, *, repo_dir, head_sha, run_id: repo_dir)
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()
    yield
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()


def test_build_daily_audit_context_starts_without_program_built_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    reset_daily_audit_persistence_store()
    reset_daily_audit_deepagents_runtime()
    source = tmp_path / "src"
    source.mkdir()
    (source / "actions.cpp").write_text(
        "void open_viewer() {}\nfor (int i = 0; i < 4; ++i) {}\n",
        encoding="utf-8",
    )

    context = orchestrator.build_daily_audit_context(
        project_id="team/project",
        repo_dir=str(tmp_path),
        default_branch="main",
        event=SimpleNamespace(event_id="evt-context"),
        sandbox=None,
    )

    assert context.candidates == []
    assert context.session_id.startswith("daily_audit:team/project:")
    assert context.session_id.endswith(":primary")
    assert context.session_id != "daily_audit:team/project:primary"


@pytest.mark.asyncio
async def test_run_daily_audit_reports_to_rolling_issue(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    calls = {}

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            calls.setdefault("configs", []).append(_kwargs["config"])
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "CVPCB_ACTIONS::showFootprintViewer",
                    "file_path": "cvpcb/tools/cvpcb_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "CVPCB_ACTIONS::showFootprintViewer",
                    "workflow_summary": "Open the footprint viewer from the user-facing action and trace the viewer workflow.",
                    "entry_evidence": [
                        "toolbars_display_footprints.cpp appends CVPCB_ACTIONS::showFootprintViewer",
                    ],
                },
                "selection_reasoning": "This user-facing action has a bounded viewer workflow and concrete entry evidence.",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            calls.setdefault("configs", []).append(_kwargs["config"])
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "CVPCB_ACTIONS::showFootprintViewer",
                    "file_path": "cvpcb/tools/cvpcb_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "CVPCB_ACTIONS::showFootprintViewer",
                    "workflow_summary": "Open the footprint viewer from the user-facing action and trace the viewer workflow.",
                    "entry_evidence": [
                        "toolbars_display_footprints.cpp appends CVPCB_ACTIONS::showFootprintViewer",
                    ],
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [
                    {
                        "category": "bug",
                        "confidence": "high",
                        "summary": "possible null dereference",
                        "evidence": ["foo can be null"],
                    }
                ],
                "used_subagents": ["candidate_scout", "focus_selector", "analysis_specialist"],
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: calls.setdefault("agent_kwargs", []).append(kwargs) or (
            FakeSelector()
            if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
            else FakeAnalyzer()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda project_id, **kwargs: calls.setdefault("issue", (project_id, kwargs)) or 12,
    )

    result = await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-1"),
    )

    assert result.status == "reported"
    assert result.finding_count == 1
    assert result.unit_type == "action_workflow"
    assert result.unit_label == "CVPCB_ACTIONS::showFootprintViewer"
    assert calls["issue"][0] == "team/project"
    assert calls["issue"][1]["title"] == "Open Review 日常审计：CVPCB_ACTIONS::showFootprintViewer"
    assert "## 日常审计运行" in calls["issue"][1]["description"]
    assert "- 默认分支：" in calls["issue"][1]["description"]
    assert "- 选定工作流：" in calls["issue"][1]["description"]
    assert "report body" in calls["issue"][1]["description"]
    assert "open-review-daily-issue: current" not in calls["issue"][1]["description"]
    assert calls["configs"][0]["configurable"]["thread_id"] == "daily_audit:team/project:daily-run-1:direction"
    assert calls["configs"][1]["configurable"]["thread_id"] == ctx.session_id
    assert all("lifecycle" not in kwargs for kwargs in calls["agent_kwargs"])


@pytest.mark.asyncio
async def test_run_daily_audit_fails_with_explicit_stage_mismatch(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Draw Line",
                    "file_path": "pcbnew/tools/drawing_tool.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "PCB_ACTIONS::drawLine",
                    "workflow_summary": "Draw a line from the PCB drawing toolbar.",
                    "entry_evidence": ["toolbar appends PCB_ACTIONS::drawLine"],
                },
                "selection_reasoning": "Bounded workflow.",
                "used_subagents": ["candidate_scout"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "structured_response": DailyAuditSelectionResponse(
                    selected_unit={
                        "unit_type": "action_workflow",
                        "label": "Draw Line",
                        "file_path": "pcbnew/tools/drawing_tool.cpp",
                        "entrypoint_kind": "toolbar_action",
                        "entrypoint_symbol": "PCB_ACTIONS::drawLine",
                        "workflow_summary": "Draw a line from the PCB drawing toolbar.",
                        "entry_evidence": ["toolbar appends PCB_ACTIONS::drawLine"],
                    },
                    selection_reasoning="still a direction payload",
                )
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda *_args, **_kwargs: pytest.fail("stage mismatch must not publish issue"),
    )

    with pytest.raises(RuntimeError, match="direction-stage structured response"):
        await orchestrator.run_daily_audit(
            project_id="team/project",
            repo_dir=str(tmp_path),
            sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
            default_branch="main",
            publish_service=None,
            event=SimpleNamespace(event_id="evt-stage-mismatch"),
        )


@pytest.mark.asyncio
async def test_run_daily_audit_does_not_persist_legacy_evolution_samples(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    calls = {}

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "CVPCB_ACTIONS::showFootprintViewer",
                    "file_path": "cvpcb/tools/cvpcb_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "CVPCB_ACTIONS::showFootprintViewer",
                    "workflow_summary": "Open the footprint viewer from the user-facing action and trace the viewer workflow.",
                    "entry_evidence": [
                        "toolbars_display_footprints.cpp appends CVPCB_ACTIONS::showFootprintViewer",
                    ],
                },
                "selection_reasoning": "This user-facing action has a bounded viewer workflow and concrete entry evidence.",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "CVPCB_ACTIONS::showFootprintViewer",
                    "file_path": "cvpcb/tools/cvpcb_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "CVPCB_ACTIONS::showFootprintViewer",
                    "workflow_summary": "Open the footprint viewer from the user-facing action and trace the viewer workflow.",
                    "entry_evidence": [
                        "toolbars_display_footprints.cpp appends CVPCB_ACTIONS::showFootprintViewer",
                    ],
                },
                "summary_markdown": "summary for evolution",
                "report_markdown": "report body for evolution",
                "recommended_action": "report_only",
                "findings": [
                    {
                        "category": "bug",
                        "confidence": "high",
                        "summary": "possible null dereference",
                        "evidence": ["foo can be null"],
                    }
                ],
                "used_subagents": ["candidate_scout", "focus_selector", "analysis_specialist"],
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda project_id, **kwargs: calls.setdefault("issue", (project_id, kwargs)) or 12,
    )

    await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-evolution"),
    )

    assert "evolution" not in calls


@pytest.mark.asyncio
async def test_run_daily_audit_uses_per_run_worktree_for_analysis_and_autofix(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path / "repo"))
    calls = {
        "create_worktree": None,
        "cleanup": None,
        "agent_repo_dirs": [],
        "status_repo_dirs": [],
        "changed_repo_dirs": [],
        "commit": None,
        "push": None,
        "mr": None,
    }
    base_repo_dir = str(tmp_path / "repo")
    worktree_dir = str(tmp_path / "worktrees" / "daily-run-1")

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                    "rationale": "hot function",
                },
                "selection_reasoning": "chosen",
                "used_subagents": [],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                    "rationale": "hot function",
                },
                "summary_markdown": "summary",
                "report_markdown": "report",
                "recommended_action": "autofix",
                "findings": [],
                "used_subagents": [],
                "commit_message": "fix: foo",
                "branch_name": "open-review/daily-audit/foo",
            }

    def fake_create_temporary_worktree(sandbox, *, repo_dir, head_sha, run_id):
        calls["create_worktree"] = {
            "sandbox": sandbox,
            "repo_dir": repo_dir,
            "head_sha": head_sha,
            "run_id": run_id,
        }
        return worktree_dir

    def fake_build_daily_audit_agent(**kwargs):
        calls["agent_repo_dirs"].append(kwargs["repo_dir"])
        return FakeSelector() if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse else FakeAnalyzer()

    def fake_working_tree_has_changes(_sandbox, repo_dir):
        calls["status_repo_dirs"].append(repo_dir)
        return True

    def fake_collect_changed_paths(_sandbox, repo_dir):
        calls["changed_repo_dirs"].append(repo_dir)
        return ["src/foo.c"]

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", fake_create_temporary_worktree)
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda **kwargs: calls.__setitem__("cleanup", kwargs))
    monkeypatch.setattr(orchestrator, "build_daily_audit_agent", fake_build_daily_audit_agent)
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", fake_working_tree_has_changes)
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", fake_collect_changed_paths)
    monkeypatch.setattr(orchestrator, "_count_changed_lines", lambda _sandbox, repo_dir: 4 if repo_dir == worktree_dir else pytest.fail("line count used base repo"))
    monkeypatch.setattr(orchestrator, "_is_safe_autofix", lambda *_args, **_kwargs: (True, None))
    monkeypatch.setattr(orchestrator, "commit_all_and_get_sha", lambda **kwargs: calls.__setitem__("commit", kwargs) or "abc1234")
    monkeypatch.setattr(orchestrator, "push_branch_head", lambda **kwargs: calls.__setitem__("push", kwargs))
    monkeypatch.setattr(
        orchestrator,
        "create_project_merge_request",
        lambda project_id, **kwargs: calls.__setitem__("mr", (project_id, kwargs)) or SimpleNamespace(iid=9),
    )

    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")
    result = await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=base_repo_dir,
        sandbox=sandbox,
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-worktree"),
    )

    assert result.status == "merge_request_opened"
    assert calls["create_worktree"] == {
        "sandbox": sandbox,
        "repo_dir": base_repo_dir,
        "head_sha": "origin/main",
        "run_id": "daily-run-1",
    }
    assert calls["agent_repo_dirs"] == [worktree_dir, worktree_dir]
    assert calls["status_repo_dirs"] == [worktree_dir]
    assert calls["changed_repo_dirs"] == [worktree_dir]
    assert calls["commit"]["worktree_dir"] == worktree_dir
    assert calls["push"]["worktree_dir"] == worktree_dir
    assert calls["push"]["source_branch"] == "open-review/daily-audit/foo/daily-run-1"
    assert calls["mr"][1]["source_branch"] == "open-review/daily-audit/foo/daily-run-1"
    assert calls["cleanup"] == {"sandbox": sandbox, "repo_dir": base_repo_dir, "worktree_dir": worktree_dir}


@pytest.mark.asyncio
async def test_run_daily_audit_cleans_worktree_when_analysis_fails(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path / "repo"))
    calls = {"cleanup": None}
    base_repo_dir = str(tmp_path / "repo")
    worktree_dir = str(tmp_path / "worktrees" / "daily-run-1")

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                    "rationale": "hot function",
                },
                "selection_reasoning": "chosen",
                "used_subagents": [],
            }

    class FailingAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            raise RuntimeError("analysis failed")

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: worktree_dir)
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda **kwargs: calls.__setitem__("cleanup", kwargs))
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FailingAnalyzer(),
    )

    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")
    with pytest.raises(RuntimeError, match="analysis failed"):
        await orchestrator.run_daily_audit(
            project_id="team/project",
            repo_dir=base_repo_dir,
            sandbox=sandbox,
            default_branch="main",
            publish_service=None,
            event=SimpleNamespace(event_id="evt-worktree-fails"),
        )

    assert calls["cleanup"] == {"sandbox": sandbox, "repo_dir": base_repo_dir, "worktree_dir": worktree_dir}


@pytest.mark.asyncio
async def test_run_daily_audit_opens_merge_request_for_safe_autofix(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    calls = {}

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "selection_reasoning": "hot function candidate",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "autofix",
                "findings": [
                    {
                        "category": "optimization",
                        "confidence": "high",
                        "summary": "avoid repeated lookup",
                        "evidence": ["loop does redundant work"],
                    }
                ],
                "used_subagents": ["correctness_reviewer", "performance_reviewer", "verification_agent"],
                "merge_request_title": "optimize foo loop",
                "merge_request_description": "details",
                "commit_message": "fix: optimize foo loop",
                "branch_name": "open-review/daily-audit/foo-loop",
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["src/foo.c"])
    monkeypatch.setattr(orchestrator, "_count_changed_lines", lambda *_args, **_kwargs: 12)
    monkeypatch.setattr(orchestrator, "_is_safe_autofix", lambda *_args, **_kwargs: (True, None))
    monkeypatch.setattr(settings, "GITLAB_EXTERNAL_URL", "https://gitlab.example.com")
    def fake_commit(**kwargs):
        calls["commit"] = kwargs
        return "abc1234"

    monkeypatch.setattr(orchestrator, "commit_all_and_get_sha", fake_commit)
    monkeypatch.setattr(
        orchestrator,
        "push_branch_head",
        lambda **kwargs: calls.setdefault("push", kwargs),
    )
    def fake_create_mr(project_id, **kwargs):
        calls["mr"] = (project_id, kwargs)
        return SimpleNamespace(iid=9, web_url="http://gitlab/team/project/-/merge_requests/9")

    monkeypatch.setattr(orchestrator, "create_project_merge_request", fake_create_mr)

    result = await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-2"),
    )

    assert result.status == "merge_request_opened"
    assert result.commit_sha == "abc1234"
    assert result.merge_request_url == "https://gitlab.example.com/team/project/-/merge_requests/9"
    assert calls["mr"][0] == "team/project"
    assert calls["push"]["source_branch"] == "open-review/daily-audit/foo-loop/daily-run-1"
    assert calls["mr"][1]["source_branch"] == "open-review/daily-audit/foo-loop/daily-run-1"


@pytest.mark.asyncio
async def test_run_daily_audit_uses_chinese_fallback_titles(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    calls = {}

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "selection_reasoning": "hot function candidate",
                "used_subagents": [],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "summary_markdown": "中文摘要",
                "report_markdown": "中文报告正文",
                "recommended_action": "autofix",
                "findings": [],
                "used_subagents": ["verification_agent"],
                "merge_request_title": None,
                "merge_request_description": None,
                "commit_message": "fix: foo",
                "branch_name": "open-review/daily-audit/foo",
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["src/foo.c"])
    monkeypatch.setattr(orchestrator, "_count_changed_lines", lambda *_args, **_kwargs: 12)
    monkeypatch.setattr(orchestrator, "_is_safe_autofix", lambda *_args, **_kwargs: (True, None))
    monkeypatch.setattr(settings, "GITLAB_EXTERNAL_URL", "https://gitlab.example.com")
    monkeypatch.setattr(orchestrator, "commit_all_and_get_sha", lambda **_kwargs: "abc1234")
    monkeypatch.setattr(orchestrator, "push_branch_head", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "DAILY_AUDIT_ROLLING_ISSUE_TITLE", "Open Review 日常审计")

    def fake_create_mr(project_id, **kwargs):
        calls["mr"] = (project_id, kwargs)
        return SimpleNamespace(iid=9, web_url="http://gitlab/team/project/-/merge_requests/9")

    monkeypatch.setattr(orchestrator, "create_project_merge_request", fake_create_mr)

    await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-cn"),
    )

    assert calls["mr"][1]["title"] == "日常审计：foo()"
    assert orchestrator._issue_title("foo()") == "Open Review 日常审计：foo()"


@pytest.mark.asyncio
async def test_run_daily_audit_does_not_write_runtime_memory_projection_in_hot_path(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "selection_reasoning": "hot function candidate",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda project_id, **kwargs: 12,
    )

    await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-3"),
    )

    assert not hasattr(orchestrator, "sync_daily_audit_memory_file")


@pytest.mark.asyncio
async def test_run_daily_audit_threads_stage_context_without_lifecycle_hot_path(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    contexts: list[DailyAuditContext] = []

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "selection_reasoning": "hot function candidate",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: contexts.append(kwargs["context"]) or (
            FakeSelector()
            if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
            else FakeAnalyzer()
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda *_args, **_kwargs: 17,
    )

    await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-lifecycle"),
    )

    assert contexts[0].experiment_root == "/tmp/sandbox/.open-review-daily-audit/daily-run-1"
    assert contexts[0].session_id == "daily_audit:team/project:daily-run-1:primary"
    assert contexts[1].experiment_root == "/tmp/sandbox/.open-review-daily-audit/daily-run-1"
    assert contexts[1].session_id == "daily_audit:team/project:daily-run-1:primary"
    assert contexts[1].selected_unit is not None
    assert contexts[1].selected_unit.label == "foo()"


@pytest.mark.asyncio
async def test_run_daily_audit_emits_direction_and_analysis_spans(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    spans = []
    events = []

    @contextmanager
    def _fake_span(name, **kwargs):
        spans.append((name, kwargs))
        yield SimpleNamespace(
            set_input=lambda value, mime_type=None: events.append(("input", name, value, mime_type)),
            set_output=lambda value, mime_type=None: events.append(("output", name, value, mime_type)),
            add_event=lambda event_name, attributes=None: events.append(("event", name, event_name, attributes)),
            record_exception=lambda exc: events.append(("exception", name, type(exc).__name__)),
            set_error_status=lambda description: events.append(("error", name, description)),
        )

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "selection_reasoning": "hot function candidate",
                "used_subagents": ["candidate_scout", "focus_selector"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "function",
                    "label": "foo()",
                    "file_path": "src/foo.c",
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }

    monkeypatch.setattr(orchestrator, "start_open_review_span", _fake_span)
    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(orchestrator, "create_project_issue", lambda *_args, **_kwargs: 17)

    await orchestrator.run_daily_audit(
        project_id="team/project",
        repo_dir=str(tmp_path),
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        default_branch="main",
        publish_service=None,
        event=SimpleNamespace(event_id="evt-span"),
    )

    span_names = [item[0] for item in spans]
    assert "open_review.daily_audit.direction" in span_names
    assert "open_review.daily_audit.analysis" in span_names
    assert any(item[0] == "input" and item[1] == "open_review.daily_audit.direction" for item in events)
    assert any(item[0] == "output" and item[1] == "open_review.daily_audit.analysis" for item in events)
    assert any(item[0] == "event" and item[1] == "open_review.daily_audit.analysis" and item[2] == "invoke_completed" for item in events)


@pytest.mark.asyncio
async def test_run_daily_audit_raises_termination_before_issue_publish(monkeypatch, tmp_path):
    ctx = _context(str(tmp_path))
    calls = {"termination_checks": 0}

    class FakeSelector:
        async def ainvoke(self, *_args, **_kwargs):
            return {
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
                "used_subagents": ["candidate_scout"],
            }

    class FakeAnalyzer:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Zone Fill All",
                    "file_path": "pcbnew/tools/zone_actions.cpp",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "PCB_ACTIONS::zoneFillAll",
                    "workflow_summary": "Fill all zones from the toolbar action and trace refill scheduling.",
                    "entry_evidence": ["toolbar appends PCB_ACTIONS::zoneFillAll"],
                },
                "summary_markdown": "summary",
                "report_markdown": "report body",
                "recommended_action": "report_only",
                "findings": [],
                "used_subagents": ["analysis_specialist"],
            }

    async def fake_raise_if_run_termination_requested(**_kwargs):
        calls["termination_checks"] += 1
        if calls["termination_checks"] == 4:
            raise RunTerminationRequested(
                run_id="runtime-daily-run",
                actor_key="team/project!daily_audit",
                reason="user_terminated",
            )

    monkeypatch.setattr(orchestrator, "build_daily_audit_context", lambda **_kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "build_daily_audit_agent",
        lambda **kwargs: FakeSelector()
        if kwargs.get("response_format") is orchestrator.DailyAuditSelectionResponse
        else FakeAnalyzer(),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_project_issue",
        lambda *_args, **_kwargs: pytest.fail("terminated daily audit runs must not publish issues"),
    )
    monkeypatch.setattr(orchestrator, "raise_if_run_termination_requested", fake_raise_if_run_termination_requested)

    with pytest.raises(RunTerminationRequested, match="user_terminated"):
        await orchestrator.run_daily_audit(
            project_id="team/project",
            repo_dir=str(tmp_path),
            sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
            default_branch="main",
            publish_service=None,
            event=SimpleNamespace(event_id="evt-terminate"),
            runtime_run_id="runtime-daily-run",
        )
