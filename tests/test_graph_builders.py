from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import ExecuteResponse
from deepagents.middleware.filesystem import _supports_execution
from langchain.agents.structured_output import ToolStrategy

from agent.config import settings
from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.scenes.auto_review import graph as auto_graph
from agent.scenes.auto_review import prompts as auto_prompts
from agent.scenes.auto_review.models import ChangedFileContext, ReviewContext
from agent.scenes.daily_audit import graph as daily_graph
from agent.scenes.daily_audit.models import AuditCandidate, DailyAuditContext
from agent.scenes.daily_audit.runtime.deepagents import reset_daily_audit_deepagents_runtime
from agent.scenes.daily_audit.selfevolution.prompts import (
    build_daily_audit_agent_prompt,
    build_daily_audit_auxiliary_prompt,
)
from agent.scenes.daily_audit.selfevolution.tools import load_tool_descriptions
from agent.scenes.mention import graph as mention_graph
from agent.scenes.mention import prompts as mention_prompts
from agent.scenes.mention.models import MentionThreadMessage
from agent.utils.structured_output import SimpleSubagentResult


class _FakeSandbox:
    cwd = "/tmp/open-review-sandboxes/thread-graph"

    def __init__(self):
        self.calls = []

    def ls(self, path):
        self.calls.append(("ls", path))
        return {"path": path}

    def read(self, file_path, offset=0, limit=2000):
        self.calls.append(("read", file_path, offset, limit))
        return {"path": file_path, "offset": offset, "limit": limit}

    def grep(self, pattern, path=None, glob=None):
        self.calls.append(("grep", pattern, path, glob))
        return {"pattern": pattern, "path": path, "glob": glob}

    def glob(self, pattern, path="/"):
        self.calls.append(("glob", pattern, path))
        return {"pattern": pattern, "path": path}

    def write(self, file_path, content):
        self.calls.append(("write", file_path, content))
        return {"path": file_path, "content": content}

    def edit(self, file_path, old_string, new_string, replace_all=False):
        self.calls.append(("edit", file_path, old_string, new_string, replace_all))
        return {
            "path": file_path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }

    def upload_files(self, files):
        self.calls.append(("upload_files", files))
        return [{"path": path} for path, _ in files]

    def download_files(self, paths):
        self.calls.append(("download_files", paths))
        return [{"path": path} for path in paths]

    def execute(self, command, timeout=None):
        self.calls.append(("execute", command, timeout))
        return ExecuteResponse(output=f"ran: {command}", exit_code=0, truncated=False)


def _context() -> mention_graph.MentionContext:
    return mention_graph.MentionContext.model_validate(
        {
            "project_id": "group/project",
            "mr_iid": 1,
            "note_id": 1,
            "discussion_id": None,
            "note_body": "@bot help with this MR",
            "note_author": "dev",
            "trigger_note": {"note_id": 1, "author": "dev", "body": "@bot help with this MR"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "mr_comment",
            "run_id": "run-1",
            "mr_snapshot": {
                "project_id": "group/project",
                "mr_iid": 1,
                "title": "MR",
                "description": "",
                "author": "dev",
                "url": "http://gitlab.local/group/project/-/merge_requests/1",
                "source_branch": "feature",
                "target_branch": "main",
                "base_sha": "a",
                "start_sha": "b",
                "head_sha": "c",
                "repo_dir": "/tmp/repo",
                "diff_range": "a..c",
                "commit_range": "a..c",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )


def _daily_context() -> DailyAuditContext:
    return DailyAuditContext(
        project_id="group/project",
        actor_key="group/project!daily_audit",
        repo_dir="/tmp/repo",
        default_branch="main",
        run_id="daily-run-1",
        experiment_root="/workspace/.open-review-daily-audit/daily-run-1",
        candidates=[
            AuditCandidate(
                unit_type="function",
                label="optimize_hot_loop()",
                file_path="src/hot.cpp",
                rationale="indexed function candidate",
                start_line=10,
                end_line=40,
            )
        ],
    )


def test_auto_review_specialist_agent_registers_investigation_subagents(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())

    auto_graph.build_auto_review_specialist_agent(_FakeSandbox(), "/tmp/repo", "security")

    specialist_call = calls[-1]
    middleware = specialist_call["middleware"]
    assert len(middleware) == 3
    assert isinstance(middleware[0], StructuredOutputRetryMiddleware)
    assert isinstance(middleware[1], ModelRetryMiddleware)
    assert isinstance(middleware[2], ToolErrorMiddleware)
    assert isinstance(specialist_call["response_format"], ToolStrategy)
    assert specialist_call["response_format"].schema is SimpleSubagentResult
    assert [tool.__name__ for tool in specialist_call["tools"]] == [
        "review_scope",
        "repo_capabilities",
        "semantic_diff",
        "evidence_search",
        "symbol_impact",
        "target_context",
        "format_probe",
    ]
    assert {item["name"] for item in specialist_call["subagents"]} == {
        "git-inspector",
        "trace-impact",
        "counterexample",
    }


def test_auto_review_director_agent_registers_structured_output_and_specialists(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())

    auto_graph.build_auto_review_director_agent(_FakeSandbox(), "/tmp/repo")

    director_call = calls[-1]
    middleware_types = [type(item).__name__ for item in director_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in middleware_types
    assert isinstance(director_call["response_format"], ToolStrategy)
    assert [tool.__name__ for tool in director_call["tools"]] == [
        "review_scope",
        "repo_capabilities",
        "semantic_diff",
        "evidence_search",
        "symbol_impact",
        "target_context",
        "format_probe",
    ]
    assert {item["name"] for item in director_call["subagents"]} == {
        "correctness",
        "reliability",
        "contracts",
        "performance-build",
        "security",
        "git-inspector",
        "repo-analyst",
    }


def test_auto_review_trace_impact_subagent_registers_static_workbench_tools(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())

    auto_graph._build_auto_review_investigation_subagent(
        _FakeSandbox(),
        "/tmp/repo",
        "trace-impact",
    )

    trace_call = calls[-1]
    assert [tool.__name__ for tool in trace_call["tools"]] == [
        "review_scope",
        "repo_capabilities",
        "semantic_diff",
        "evidence_search",
        "symbol_impact",
        "target_context",
        "format_probe",
    ]


def test_auto_review_repo_analyst_receives_static_workbench_extra_tools(monkeypatch):
    captured = {}

    def _fake_create_deep_agent(**kwargs):
        return object()

    def _fake_repo_analyst_subagent(**kwargs):
        captured["extra_tools"] = kwargs["extra_tools"]
        return {"runnable": object()}

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(auto_graph, "build_repo_analyst_subagent", _fake_repo_analyst_subagent)

    auto_graph.build_auto_review_director_agent(_FakeSandbox(), "/tmp/repo")

    assert list(captured["extra_tools"]) == [
        "review_scope",
        "repo_capabilities",
        "semantic_diff",
        "evidence_search",
        "symbol_impact",
        "target_context",
        "format_probe",
    ]


def test_auto_review_director_agent_loads_director_prompt_at_build_time(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    original_loader = auto_prompts.load_prompt_asset_text

    def _patched_loader(target_name: str) -> str:
        if target_name == "director-prompt":
            return "DYNAMIC DIRECTOR PROMPT {eda_standards}"
        return original_loader(target_name)

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(auto_prompts, "load_prompt_asset_text", _patched_loader)

    director_agent = auto_graph.build_auto_review_director_agent(_FakeSandbox(), "/tmp/repo")

    director_call = calls[-1]
    assert director_call["system_prompt"].startswith("DYNAMIC DIRECTOR PROMPT")
    assert director_agent.open_review_system_prompt.startswith("DYNAMIC DIRECTOR PROMPT")


def test_auto_review_director_agent_registers_run_termination_middleware_when_runtime_run_id_is_provided(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())

    auto_graph.build_auto_review_director_agent(
        _FakeSandbox(),
        "/tmp/repo",
        runtime_run_id="run-auto-runtime",
    )

    director_call = calls[-1]
    middleware_types = [type(item).__name__ for item in director_call["middleware"]]
    assert "RunTerminationMiddleware" in middleware_types


def test_git_inspector_subagent_uses_deep_agent_with_skills(monkeypatch, tmp_path):
    deepagent_calls = []

    def _fake_create_deep_agent(**kwargs):
        deepagent_calls.append(kwargs)
        return object()

    auto_skill_root = tmp_path / "auto-review-skills"
    auto_skill_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(auto_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(auto_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(auto_graph, "_skill_sources", lambda *_args: [str(auto_skill_root)])

    auto_graph.build_auto_review_director_agent(_FakeSandbox(), "/tmp/repo")

    git_inspector_call = next(
        call
        for call in deepagent_calls
        if "Subagent role: git-inspector" in call["system_prompt"]
    )

    middleware_types = [type(item).__name__ for item in git_inspector_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in middleware_types
    assert "ModelRetryMiddleware" in middleware_types
    assert "ToolErrorMiddleware" in middleware_types
    assert git_inspector_call["backend"] is not None
    assert git_inspector_call["skills"] == [str(auto_skill_root)]
    assert git_inspector_call["name"] == "git-inspector"
    assert isinstance(git_inspector_call["response_format"], ToolStrategy)
    assert git_inspector_call["response_format"].schema is SimpleSubagentResult
    assert [tool.__name__ for tool in git_inspector_call["tools"]] == ["review_scope"]


def test_scene_skill_sources_resolve_local_service_repo_paths(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers"
    auto_skill_root = self_repo_root / "agent" / "scenes" / "auto_review" / "selfevolution" / "skills"
    mention_skill_root = self_repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "skills"
    shared_skill_dir = shared_skill_root / "using-superpowers"
    shared_skill_dir.mkdir(parents=True, exist_ok=True)
    (shared_skill_dir / "SKILL.md").write_text(
        "---\nname: using-superpowers\ndescription: shared\n---\n\nbody\n",
        encoding="utf-8",
    )
    auto_skill_root.mkdir(parents=True, exist_ok=True)
    mention_skill_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)

    auto_sources = auto_graph._skill_sources(_FakeSandbox(), "/tmp/repo")
    mention_sources = mention_graph._skill_sources(_FakeSandbox(), "/tmp/repo")

    assert auto_sources == [str(shared_skill_root), str(auto_skill_root)]
    assert mention_sources == [str(shared_skill_root), str(mention_skill_root)]


def test_scene_skill_sources_prefer_self_repo_when_configured(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers"
    auto_skill_root = self_repo_root / "agent" / "scenes" / "auto_review" / "selfevolution" / "skills"
    mention_skill_root = self_repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "skills"
    shared_skill_dir = shared_skill_root / "using-superpowers"
    shared_skill_dir.mkdir(parents=True, exist_ok=True)
    (shared_skill_dir / "SKILL.md").write_text(
        "---\nname: using-superpowers\ndescription: shared\n---\n\nbody\n",
        encoding="utf-8",
    )
    auto_skill_root.mkdir(parents=True, exist_ok=True)
    mention_skill_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)

    auto_sources = auto_graph._skill_sources(_FakeSandbox(), "/tmp/repo")
    mention_sources = mention_graph._skill_sources(_FakeSandbox(), "/tmp/repo")

    assert auto_sources == [str(shared_skill_root), str(auto_skill_root)]
    assert mention_sources == [str(shared_skill_root), str(mention_skill_root)]


def test_scene_skill_sources_mirror_bundled_skills_for_docker_backends(monkeypatch, tmp_path):
    host_root = tmp_path / "sandbox" / "thread-graph"

    class _DockerLikeSandbox(_FakeSandbox):
        root_dir = "/workspace"
        cwd = "/workspace/repo"

        def __init__(self):
            super().__init__()
            self.host_root_dir = str(host_root)

    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers"
    auto_skill_root = self_repo_root / "agent" / "scenes" / "auto_review" / "selfevolution" / "skills"
    mention_skill_root = self_repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "skills"
    shared_skill_dir = shared_skill_root / "using-superpowers"
    auto_skill_dir = auto_skill_root / "review-swarm"
    mention_skill_dir = mention_skill_root / "mention-flow"
    for skill_dir, description in (
        (shared_skill_dir, "shared"),
        (auto_skill_dir, "auto review"),
        (mention_skill_dir, "mention"),
    ):
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_dir.name}\ndescription: {description}\n---\n\nbody\n",
            encoding="utf-8",
        )

    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)

    auto_sources = auto_graph._skill_sources(_DockerLikeSandbox(), "/workspace/repo")
    mention_sources = mention_graph._skill_sources(_DockerLikeSandbox(), "/workspace/repo")

    assert auto_sources == [
        "/workspace/runtime/auto_review/bundled-skills/superpowers",
        "/workspace/runtime/auto_review/bundled-skills/service_repo",
    ]
    assert mention_sources == [
        "/workspace/runtime/mention/bundled-skills/superpowers",
        "/workspace/runtime/mention/bundled-skills/service_repo",
    ]
    assert (
        host_root / "runtime" / "auto_review" / "bundled-skills" / "service_repo" / "review-swarm" / "SKILL.md"
    ).read_text(encoding="utf-8").startswith("---")
    assert (
        host_root / "runtime" / "mention" / "bundled-skills" / "service_repo" / "mention-flow" / "SKILL.md"
    ).read_text(encoding="utf-8").startswith("---")


def test_auto_review_director_prompt_prefers_self_repo_source_when_configured(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    target = self_repo_root / "agent" / "scenes" / "auto_review" / "selfevolution" / "prompts" / "director-prompt.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SELF REPO DIRECTOR {eda_standards}", encoding="utf-8")

    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)

    prompt = auto_prompts.get_auto_review_director_prompt()

    assert prompt.startswith("SELF REPO DIRECTOR")


def test_mention_author_prompt_prefers_self_repo_source_when_configured(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    target = self_repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "prompts" / "author-prompt.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SELF REPO MENTION {thread_text}", encoding="utf-8")

    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)

    prompt = mention_prompts.build_mention_author_prompt("/tmp/repo", "/tmp/repo", _context())

    assert prompt.startswith("SELF REPO MENTION")


def test_shared_scene_skill_sources_module_is_removed():
    assert importlib.util.find_spec("agent.scenes.skill_sources") is None


def test_auto_review_observed_runnable_emits_open_review_span(monkeypatch):
    spans = []
    interactions = []

    class _FakeTraceContext:
        def set_input(self, value, mime_type=None):
            interactions.append(("input", value, mime_type))

        def set_output(self, value, mime_type=None):
            interactions.append(("output", value, mime_type))

        def add_event(self, name, attributes=None):
            interactions.append(("event", name, attributes))

    @contextmanager
    def _fake_span(name, **kwargs):
        spans.append((name, kwargs))
        yield _FakeTraceContext()

    class _FakeRunnable:
        async def ainvoke(self, payload, config=None, **kwargs):
            return {"payload": payload, "config": config, "kwargs": kwargs}

    monkeypatch.setattr(auto_graph, "start_open_review_span", _fake_span)

    wrapped = auto_graph._ObservedSubagentRunnable(
        _FakeRunnable(),
        span_name="open_review.auto_review.specialist.security",
        tags=["auto_review", "specialist"],
        static_attributes={
            "open_review.parent_role": "director",
            "open_review.specialist_lane": "security",
        },
    )

    result = asyncio.run(
        wrapped.ainvoke(
            {"messages": [{"role": "user", "content": "check"}]},
            config={
                "configurable": {
                    "project_id": "team/project",
                    "mr_iid": 42,
                    "review_run_id": "run-123",
                }
            },
        )
    )

    assert result["config"]["configurable"]["review_run_id"] == "run-123"
    assert spans == [
        (
            "open_review.auto_review.specialist.security",
            {
                "attributes": {
                    "open_review.parent_role": "director",
                    "open_review.project_id": "team/project",
                    "open_review.mr_iid": 42,
                    "open_review.review_run_id": "run-123",
                    "open_review.specialist_lane": "security",
                },
                "metadata": None,
                "tags": ["auto_review", "specialist"],
                "span_kind": "agent",
            },
        )
    ]
    assert interactions == [
        (
            "input",
            {"messages": [{"role": "user", "content": "check"}]},
            None,
        ),
        (
            "event",
            "invoke_completed",
            {"payload_keys": ["config", "kwargs", "payload"], "structured_response_present": False},
        ),
        (
            "output",
            {
                "payload": {"messages": [{"role": "user", "content": "check"}]},
                "config": {
                    "configurable": {
                        "project_id": "team/project",
                        "mr_iid": 42,
                        "review_run_id": "run-123",
                    }
                },
                "kwargs": {},
            },
            None,
        ),
    ]


def test_auto_review_execution_backend_allows_workspace_edits_but_blocks_git_mutation():
    class _DockerLikeSandbox(_FakeSandbox):
        root_dir = "/workspace"
        host_root_dir = "/tmp/open-review-sandboxes/thread-graph"
        cwd = "/workspace"

        def __init__(self):
            super().__init__()
            self.read_result = None
            self.execute_output = ""

        def read(self, file_path, offset=0, limit=2000):
            self.calls.append(("read", file_path, offset, limit))
            return self.read_result or {"path": file_path, "offset": offset, "limit": limit}

        def execute(self, command, timeout=None):
            self.calls.append(("execute", command, timeout))
            return ExecuteResponse(output=self.execute_output, exit_code=0, truncated=False)

    sandbox = _DockerLikeSandbox()
    backend = auto_graph.AutoReviewExecutionBackend(
        sandbox,
        repo_dir="/workspace/repo",
    )

    write_result = backend.write("/worktrees/review-mr:root_kicad:5/src/router.cpp", "int x = 1;\n")
    assert write_result["path"] == "/workspace/worktrees/review-mr:root_kicad:5/src/router.cpp"

    edit_result = backend.edit("/worktrees/review-mr:root_kicad:5/src/router.cpp", "x = 1", "x = 2")
    assert edit_result["path"] == "/workspace/worktrees/review-mr:root_kicad:5/src/router.cpp"

    diff_result = backend.execute("git -C /workspace/repo diff --stat")
    assert diff_result.exit_code == 0
    assert ("execute", "git -C /workspace/repo diff --stat", None) in sandbox.calls

    with pytest.raises(PermissionError, match="git_state_mutation_blocked"):
        backend.execute("git -C /workspace/repo commit -m 'nope'")

    with pytest.raises(PermissionError, match="git_state_mutation_blocked"):
        backend.execute("git -C /workspace/repo push origin HEAD")

    assert backend.tool_error_count == 0

    async def _async_checks():
        await backend.awrite("/worktrees/review-mr:root_kicad:5/src/router.cpp", "int y = 3;\n")
        await backend.aedit("/worktrees/review-mr:root_kicad:5/src/router.cpp", "y = 3", "y = 4")
        await backend.aexecute("git -C /workspace/repo status --short")

    asyncio.run(_async_checks())


def test_auto_review_execution_backend_is_detected_as_executable_backend():
    backend = auto_graph.AutoReviewExecutionBackend(
        _FakeSandbox(),
        repo_dir="/tmp/repo",
    )

    assert _supports_execution(backend) is True


def test_auto_review_execution_backend_normalizes_visible_paths_and_resolves_gitfile_metadata(tmp_path):
    class _DockerLikeSandbox(_FakeSandbox):
        root_dir = "/workspace"
        cwd = "/workspace"

        def __init__(self, host_root_dir: str):
            super().__init__()
            self.host_root_dir = host_root_dir

    host_root = tmp_path / "sandbox"
    repo_root = host_root / "repo"
    worktree_name = "review-mr:root_kicad:8:open:abc123"
    worktree_root = host_root / "worktrees" / worktree_name
    gitdir = repo_root / ".git" / "worktrees" / "review-mr-root_kicad-8-open-abc123"

    worktree_root.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (worktree_root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "HEAD").write_text("abc123\n", encoding="utf-8")
    (repo_root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (worktree_root / "src").mkdir()
    (worktree_root / "src" / "router.cpp").write_text("int router();\n", encoding="utf-8")

    sandbox = _DockerLikeSandbox(str(host_root))
    backend = auto_graph.AutoReviewExecutionBackend(
        sandbox,
        repo_dir=f"/workspace/worktrees/{worktree_name}",
    )

    backend.read("/worktrees/review-mr:root_kicad:8:open:abc123/src/router.cpp")
    backend.read("/workspace/worktrees/review-mr:root_kicad:8:open:abc123/.git/HEAD")
    backend.read("/workspace/worktrees/review-mr:root_kicad:8:open:abc123/.git/config")

    assert ("read", "/workspace/worktrees/review-mr:root_kicad:8:open:abc123/src/router.cpp", 0, 2000) in sandbox.calls
    assert ("read", "/workspace/repo/.git/worktrees/review-mr-root_kicad-8-open-abc123/HEAD", 0, 2000) in sandbox.calls
    assert ("read", "/workspace/repo/.git/config", 0, 2000) in sandbox.calls


def test_auto_review_execution_backend_raises_semantic_tool_failures():
    class _DockerLikeSandbox(_FakeSandbox):
        root_dir = "/workspace"
        host_root_dir = "/tmp/open-review-sandboxes/thread-graph"
        cwd = "/workspace"

        def __init__(self):
            super().__init__()
            self.read_result = None
            self.execute_output = ""
            self.execute_exit_code = 0

        def read(self, file_path, offset=0, limit=2000):
            self.calls.append(("read", file_path, offset, limit))
            return self.read_result or {"path": file_path, "offset": offset, "limit": limit}

        def execute(self, command, timeout=None):
            self.calls.append(("execute", command, timeout))
            return ExecuteResponse(output=self.execute_output, exit_code=self.execute_exit_code, truncated=False)

    sandbox = _DockerLikeSandbox()
    backend = auto_graph.AutoReviewExecutionBackend(
        sandbox,
        repo_dir="/workspace/repo",
    )

    sandbox.read_result = {"error": "file_not_found"}
    with pytest.raises(auto_graph.SemanticToolFailure, match="read:file_not_found"):
        backend.read("src/router.cpp")
    assert backend.semantic_failure_count == 0

    sandbox.execute_output = "[stderr] path_not_found\n\nExit code: 3"
    sandbox.execute_exit_code = 3
    with pytest.raises(auto_graph.SemanticToolFailure, match="grep:path_not_found"):
        backend.grep("needle", "/worktrees/review-mr:root_kicad:5")
    assert backend.semantic_failure_count == 0

    sandbox.read_result = {"error": "permission_denied"}
    with pytest.raises(auto_graph.SemanticToolFailure, match="read:permission_denied"):
        backend.read("src/router.cpp")

    assert backend.semantic_failure_count == 1
    assert backend.failure_reasons == ["read:permission_denied"]


def test_review_scope_tool_returns_frozen_scope_snapshot():
    context = ReviewContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "title": "Fix router regression",
            "description": "",
            "author": "dev",
            "url": "http://gitlab/team/project/-/merge_requests/42",
            "source_branch": "feature/router-fix",
            "target_branch": "main",
            "base_sha": "base123",
            "start_sha": "start123",
            "head_sha": "head123",
            "repo_dir": "/tmp/repo",
            "review_run_id": "run-123",
            "review_mode": "full",
            "diff_range": "origin/main...HEAD",
            "commit_range": "origin/main..HEAD",
            "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
            "diff_fingerprint": "fp123",
            "changed_files": [
                {
                    "file_path": "src/router.cpp",
                    "old_path": "src/router.cpp",
                    "diff": "@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                    "added_lines": [1],
                },
                {
                    "file_path": "include/new_header.h",
                    "old_path": "include/new_header.h",
                    "diff": "@@ -0,0 +1 @@\n+int x;\n",
                    "new_file": True,
                    "deleted_file": False,
                    "renamed_file": False,
                    "added_lines": [1],
                },
            ],
            "commit_messages": ["fix: router regression"],
            "previous_review_head_sha": None,
            "previous_review_diff_fingerprint": None,
            "previous_bot_comments": [],
            "previous_bot_dedupe_keys": [],
            "recent_human_comments": [],
            "static_analysis_findings": [],
            "skip_reason": None,
        }
    )

    tool = auto_graph._build_review_scope_tool(context)

    snapshot = tool()
    file_snapshot = tool(file_path="src/router.cpp")

    assert snapshot["scope_source"] == "orchestrator_frozen_snapshot"
    assert snapshot["head_sha"] == "head123"
    assert snapshot["changed_files"] == [
        {"path": "src/router.cpp", "status": "modified"},
        {"path": "include/new_header.h", "status": "new"},
    ]
    assert file_snapshot["status"] == "modified"
    assert file_snapshot["diff"] == "@@ -1 +1 @@\n-int x;\n+int x = 1;\n"


def test_main_mention_agent_registers_subagents_and_structured_output(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(mention_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(mention_graph, "make_model", lambda *_args, **_kwargs: object())

    mention_graph.build_mention_agent(
        _FakeSandbox(),
        "/tmp/repo",
        "feature",
        _context(),
    )

    main_call = calls[-1]
    middleware = main_call["middleware"]
    assert len(middleware) == 4
    assert type(middleware[0]).__name__ == "MentionRawRecordMiddleware"
    assert isinstance(middleware[1], StructuredOutputRetryMiddleware)
    assert isinstance(middleware[2], ModelRetryMiddleware)
    assert isinstance(middleware[3], ToolErrorMiddleware)
    assert isinstance(main_call["response_format"], ToolStrategy)
    assert [tool.__name__ for tool in main_call["tools"]] == ["review_scope"]
    assert len(main_call["subagents"]) == 2
    assert {item["name"] for item in main_call["subagents"]} == {
        "dialogs",
        "repo-analyst",
    }


def test_main_mention_agent_registers_run_termination_middleware_when_runtime_run_id_is_provided(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(mention_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(mention_graph, "make_model", lambda *_args, **_kwargs: object())

    mention_graph.build_mention_agent(
        _FakeSandbox(),
        "/tmp/repo",
        "feature",
        _context(),
        runtime_run_id="run-mention-runtime",
    )

    main_call = calls[-1]
    middleware_types = [type(item).__name__ for item in main_call["middleware"]]
    assert "RunTerminationMiddleware" in middleware_types
    assert "StructuredOutputRetryMiddleware" in middleware_types


def test_mention_author_prompt_and_tool_metadata_track_runtime_helpers():
    prompt = mention_prompts.build_mention_author_prompt("/tmp/repo", "/tmp/repo", _context())
    tool_descriptions = json.loads(
        (Path(mention_graph.__file__).resolve().with_name("selfevolution") / "tools" / "tool_descriptions.json").read_text(
            encoding="utf-8"
        )
    )

    assert "Available auxiliary subagents: `dialogs`, `repo-analyst`." in prompt
    assert "cross-file reasoning, whole-repo location, or impact-chain tracing" in prompt
    assert "explore" not in tool_descriptions
    assert "repo-analyst" in tool_descriptions


def test_mention_author_prompt_strips_hidden_open_review_markers_from_thread_text():
    context = _context().model_copy(
        update={
            "discussion_messages": [
                MentionThreadMessage(
                    note_id=2,
                    author="open-review-bot",
                    body="结论正文\n<!-- open-review-mention-run: old-run -->\n<!-- open-review-head-sha: old-head -->",
                )
            ]
        }
    )

    prompt = mention_prompts.build_mention_author_prompt("/tmp/repo", "/tmp/repo", context)

    assert "结论正文" in prompt
    assert "open-review-mention-run" not in prompt
    assert "open-review-head-sha" not in prompt


def test_main_mention_agent_allows_shell_but_blocks_branch_mutation(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(mention_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(mention_graph, "make_model", lambda *_args, **_kwargs: object())
    sandbox = _FakeSandbox()

    mention_graph.build_mention_agent(
        sandbox,
        "/tmp/repo",
        "feature",
        _context(),
    )

    backend = calls[-1]["backend"]
    middleware_types = [type(item).__name__ for item in calls[-1]["middleware"]]
    assert "StructuredOutputRetryMiddleware" in middleware_types
    assert backend is not sandbox
    assert hasattr(backend, "execute")
    assert _supports_execution(backend) is True

    backend.write("foo.cc", "int main() {}")
    backend.edit("foo.cc", "main", "entry", replace_all=False)
    backend.upload_files([("foo.cc", b"int main() {}")])
    backend.download_files(["foo.cc"])
    safe = backend.execute("cmake --build build", timeout=30)
    blocked_push = backend.execute("git push origin HEAD:feature")
    blocked_commit = backend.execute("git commit -m 'fix'")

    assert ("write", "foo.cc", "int main() {}") in sandbox.calls
    assert ("edit", "foo.cc", "main", "entry", False) in sandbox.calls
    assert ("upload_files", [("foo.cc", b"int main() {}")]) in sandbox.calls
    assert ("download_files", ["foo.cc"]) in sandbox.calls
    assert ("execute", "cmake --build build", 30) in sandbox.calls
    assert safe.exit_code == 0
    assert blocked_push.exit_code == 126
    assert "git push" in blocked_push.output
    assert blocked_commit.exit_code == 126
    assert "git commit" in blocked_commit.output


def test_auxiliary_mention_subagent_uses_read_only_backend(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(mention_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(mention_graph, "make_model", lambda *_args, **_kwargs: object())
    sandbox = _FakeSandbox()

    mention_graph.build_mention_auxiliary_subagent(
        sandbox,
        "/tmp/repo",
        "feature",
        "dialogs",
        _context(),
    )

    backend = calls[-1]["backend"]
    assert backend is not sandbox
    assert not hasattr(backend, "execute")
    assert isinstance(calls[-1]["response_format"], ToolStrategy)
    assert calls[-1]["response_format"].schema is SimpleSubagentResult
    assert [tool.__name__ for tool in calls[-1]["tools"]] == ["review_scope"]


def test_daily_audit_auxiliary_subagent_uses_simple_result_schema(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(daily_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(daily_graph, "make_model", lambda *_args, **_kwargs: object())

    daily_graph.build_daily_audit_auxiliary_subagent(
        _FakeSandbox(),
        "/tmp/repo",
        _daily_context(),
        "verification_agent",
    )

    auxiliary_call = calls[-1]
    assert isinstance(auxiliary_call["response_format"], ToolStrategy)
    assert auxiliary_call["response_format"].schema is SimpleSubagentResult


def test_scene_owned_subagent_prompts_require_single_result_field():
    mention_prompt = mention_prompts.build_mention_auxiliary_prompt(
        repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
        subagent_type="dialogs",
        context=_context(),
    )
    auto_specialist_prompt = auto_prompts.build_auto_review_specialist_prompt(
        "/tmp/repo",
        "/tmp/repo",
        "security",
    )
    auto_investigation_prompt = auto_prompts.build_auto_review_investigation_subagent_prompt(
        "/tmp/repo",
        "/tmp/repo",
        "trace-impact",
    )
    daily_prompt = build_daily_audit_auxiliary_prompt(
        repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
        context=_daily_context(),
        subagent_type="verification_agent",
    )

    for prompt in (
        mention_prompt,
        auto_specialist_prompt,
        auto_investigation_prompt,
        daily_prompt,
    ):
        assert "exactly one field: `result`" in prompt


def test_reviewer_mention_agent_is_read_only_but_supports_shell(monkeypatch):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(mention_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(mention_graph, "make_model", lambda *_args, **_kwargs: object())
    sandbox = _FakeSandbox()

    mention_graph.build_mention_reviewer_agent(
        sandbox,
        "/tmp/repo",
        _context(),
    )

    reviewer_call = calls[-1]
    middleware_types = [type(item).__name__ for item in reviewer_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in middleware_types
    backend = reviewer_call["backend"]
    assert backend is not sandbox
    assert _supports_execution(backend) is True
    assert [tool.__name__ for tool in reviewer_call["tools"]] == ["review_scope"]
    assert "subagents" not in reviewer_call or reviewer_call["subagents"] in (None, [])

    write_result = backend.write("foo.cc", "int main() {}")
    edit_result = backend.edit("foo.cc", "main", "entry", replace_all=False)
    safe = backend.execute("git diff --stat", timeout=30)
    blocked_push = backend.execute("git push origin HEAD:feature")

    assert write_result.error == "read_only_backend"
    assert edit_result.error == "read_only_backend"
    assert ("execute", "git diff --stat", 30) in sandbox.calls
    assert safe.exit_code == 0
    assert blocked_push.exit_code == 126
    assert "git push" in blocked_push.output


def test_daily_audit_agent_registers_native_skill_sources_and_file_tools(monkeypatch, tmp_path):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(daily_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(daily_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    self_repo_root = tmp_path / "service-repo" / "open-review"
    shared_skill_root = self_repo_root / "agent" / "scenes" / "skills" / "superpowers"
    (self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills").mkdir(parents=True, exist_ok=True)
    shared_skill_dir = shared_skill_root / "using-superpowers"
    shared_skill_dir.mkdir(parents=True, exist_ok=True)
    (shared_skill_dir / "SKILL.md").write_text(
        "---\nname: using-superpowers\ndescription: shared\n---\n\nbody\n",
        encoding="utf-8",
    )
    prompt_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "prompts"
    prompt_root.mkdir(parents=True, exist_ok=True)
    tools_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    for name in ("direction-finder-prompt.md", "workflow-auditor-prompt.md", "auxiliary-agent-prompt.md"):
        (prompt_root / name).write_text(
            (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "prompts" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tools_root / "tool_descriptions.json").write_text(
        (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "tools" / "tool_descriptions.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: self_repo_root)
    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)
    reset_daily_audit_deepagents_runtime()
    sandbox = _FakeSandbox()

    daily_graph.build_daily_audit_agent(
        sandbox,
        "/tmp/repo",
        _daily_context(),
    )

    main_call = calls[-1]
    assert main_call["checkpointer"] is not None
    assert main_call["store"] is not None
    assert "memory" not in main_call
    assert str(shared_skill_root) in main_call["skills"]
    tool_names = {getattr(tool, "__name__", "") for tool in main_call["tools"]}
    assert {"session_search", "skills_list", "skill_view", "skill_manage", "direction_history", "exploration_memory"}.issubset(
        tool_names
    )
    assert "memory" not in tool_names
    middleware_types = [type(item).__name__ for item in main_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in middleware_types
    assert "ModelRetryMiddleware" in middleware_types
    assert middleware_types.count("ModelRetryMiddleware") == 1
    assert "DailyAuditSessionMiddleware" in middleware_types
    assert "SQLSkillsMiddleware" not in middleware_types
    assert {item["name"] for item in main_call["subagents"]} == {"repo-analyst"}


def test_daily_audit_agent_registers_run_termination_middleware_when_runtime_run_id_is_provided(
    monkeypatch, tmp_path
):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(daily_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(daily_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    self_repo_root = tmp_path / "service-repo" / "open-review"
    (self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills").mkdir(parents=True, exist_ok=True)
    prompt_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "prompts"
    prompt_root.mkdir(parents=True, exist_ok=True)
    tools_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    for name in ("direction-finder-prompt.md", "workflow-auditor-prompt.md", "auxiliary-agent-prompt.md"):
        (prompt_root / name).write_text(
            (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "prompts" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tools_root / "tool_descriptions.json").write_text(
        (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "tools" / "tool_descriptions.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: self_repo_root)
    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)
    reset_daily_audit_deepagents_runtime()

    daily_graph.build_daily_audit_agent(
        _FakeSandbox(),
        "/tmp/repo",
        _daily_context(),
        runtime_run_id="run-daily-runtime",
    )

    main_call = calls[-1]
    middleware_types = [type(item).__name__ for item in main_call["middleware"]]
    assert "RunTerminationMiddleware" in middleware_types
    assert "StructuredOutputRetryMiddleware" in middleware_types


def test_daily_audit_analysis_agent_registers_only_analysis_subagents(monkeypatch, tmp_path):
    calls = []

    def _fake_create_deep_agent(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(daily_graph, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(daily_graph, "make_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    self_repo_root = tmp_path / "service-repo" / "open-review"
    (self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills").mkdir(parents=True, exist_ok=True)
    prompt_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "prompts"
    prompt_root.mkdir(parents=True, exist_ok=True)
    tools_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    for name in ("direction-finder-prompt.md", "workflow-auditor-prompt.md", "auxiliary-agent-prompt.md"):
        (prompt_root / name).write_text(
            (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "prompts" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tools_root / "tool_descriptions.json").write_text(
        (Path(daily_graph.__file__).resolve().with_name("selfevolution") / "tools" / "tool_descriptions.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: self_repo_root)
    monkeypatch.setattr("agent.selfevolution.assets.ensure_self_repo_checkout", lambda default_branch=None: self_repo_root)
    reset_daily_audit_deepagents_runtime()
    sandbox = _FakeSandbox()
    context = _daily_context()
    context.selected_unit = context.candidates[0]

    daily_graph.build_daily_audit_agent(
        sandbox,
        "/tmp/repo",
        context,
        stage="analysis",
    )

    main_call = calls[-1]
    main_middleware_types = [type(item).__name__ for item in main_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in main_middleware_types
    assert "ModelRetryMiddleware" in main_middleware_types
    assert main_middleware_types.count("ModelRetryMiddleware") == 1
    tool_names = {getattr(tool, "__name__", "") for tool in main_call["tools"]}
    assert "direction_history" not in tool_names
    assert "exploration_memory" not in tool_names
    assert {item["name"] for item in main_call["subagents"]} == {
        "correctness_reviewer",
        "performance_reviewer",
        "optimization_reviewer",
        "verification_agent",
        "evolution_curator",
        "repo-analyst",
    }
    auxiliary_call = calls[0]
    auxiliary_middleware_types = [type(item).__name__ for item in auxiliary_call["middleware"]]
    assert "StructuredOutputRetryMiddleware" in auxiliary_middleware_types
    assert "ModelRetryMiddleware" in auxiliary_middleware_types
    assert "ToolErrorMiddleware" in auxiliary_middleware_types


def test_daily_audit_direction_prompt_instructs_agent_to_check_direction_history():
    prompt = build_daily_audit_agent_prompt(
        repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
        context=_daily_context(),
        stage="direction",
    )

    assert "direction_history" in prompt
    assert "exploration_memory" in prompt
    assert "judge overlap yourself" in prompt.lower()
    assert "prefer `repo-analyst`" in prompt


def test_daily_audit_analysis_prompt_treats_selected_unit_as_authoritative_direction():
    prompt = build_daily_audit_agent_prompt(
        repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
        context=_daily_context().model_copy(update={"selected_unit": _daily_context().candidates[0]}),
        stage="analysis",
    )

    assert "Do not re-open direction selection" in prompt
    assert "Do not compare alternative workflows" in prompt
    assert "prefer `repo-analyst`" in prompt


def test_daily_audit_analysis_prompt_enforces_single_primary_issue_and_measured_performance_validation():
    prompt = build_daily_audit_agent_prompt(
        repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
        context=_daily_context().model_copy(update={"selected_unit": _daily_context().candidates[0]}),
        stage="analysis",
    )

    assert "single most important bounded issue" in prompt
    assert "Do not emit multiple unrelated findings" in prompt
    assert "one issue, one evidence chain, one recommended action" in prompt
    assert "actual script, harness, or benchmark" in prompt
    assert "must not elevate the claim into a formal finding" in prompt
    assert "Simplified Chinese" in prompt


def test_daily_audit_workflow_auditor_assets_emphasize_depth_over_breadth():
    skill_text = (
        Path(daily_graph.__file__).resolve().with_name("selfevolution") / "skills" / "workflow-auditor" / "SKILL.md"
    ).read_text(encoding="utf-8")
    descriptions = load_tool_descriptions()

    assert "one primary issue" in skill_text
    assert "Do not enumerate multiple unrelated findings" in skill_text
    assert "script, harness, or benchmark" in skill_text
    assert "analysis_specialist" not in descriptions
    assert "trace cross-file impact" in descriptions["repo-analyst"]
    assert "script-backed" in descriptions["performance_reviewer"]
    assert "script-backed" in descriptions["optimization_reviewer"]


def test_auto_review_tool_metadata_tracks_explicit_investigation_roles():
    descriptions = json.loads(
        (
            Path(auto_graph.__file__).resolve().with_name("selfevolution") / "tools" / "tool_descriptions.json"
        ).read_text(encoding="utf-8")
    )

    assert "explore" not in descriptions
    assert "general-purpose" not in descriptions
    assert "repo-analyst" in descriptions
    assert "semantic_diff" in descriptions
    assert "format_probe" in descriptions
    assert "configuring or building" in descriptions["target_context"]


def test_daily_audit_skill_sources_keep_service_repo_path_for_local_backend(monkeypatch):
    self_repo_root = Path("/tmp/service-repo/open-review")
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.tools.skills.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: self_repo_root)
    sandbox = _FakeSandbox()
    # simulate bootstrapped service repo assets
    shared_root = self_repo_root / "agent" / "shared" / "skills"
    scene_root = self_repo_root / "agent" / "scenes" / "daily_audit" / "selfevolution" / "skills"
    shared_root.mkdir(parents=True, exist_ok=True)
    scene_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        daily_graph,
        "_skill_source_roots",
        lambda repo_dir, default_branch=None: [("superpowers", shared_root), ("self_repo", scene_root)],
    )

    sources = daily_graph._skill_sources(sandbox, "/tmp/repo", "main")

    assert sources == [
        str(shared_root),
        str(scene_root),
    ]


def test_daily_audit_skill_sources_mirror_bundled_skills_into_shared_state_for_docker_backend(
    monkeypatch,
    tmp_path,
):
    class _DockerLikeSandbox(_FakeSandbox):
        root_dir = "/workspace"
        host_root_dir = "/tmp/open-review-sandboxes/thread-graph"
        cwd = "/workspace/repo"

    shared_root = tmp_path / "shared-skills"
    shared_skill_dir = shared_root / "test-driven-development"
    shared_skill_dir.mkdir(parents=True)
    (shared_skill_dir / "SKILL.md").write_text(
        "---\nname: test-driven-development\ndescription: shared\n---\n\nbody\n",
        encoding="utf-8",
    )
    scene_root = tmp_path / "daily-skills"
    scene_skill_dir = scene_root / "direction-finder"
    scene_skill_dir.mkdir(parents=True)
    (scene_skill_dir / "SKILL.md").write_text(
        "---\nname: direction-finder\ndescription: bundled\n---\n\nbody\n",
        encoding="utf-8",
    )
    shared_state_root = tmp_path / "shared-state"

    monkeypatch.setattr(
        daily_graph,
        "_skill_source_roots",
        lambda repo_dir, default_branch=None: [
            ("superpowers", shared_root),
            ("self_repo", scene_root),
        ],
    )
    monkeypatch.setattr(daily_graph, "daily_audit_state_root", lambda: shared_state_root)

    sources = daily_graph._skill_sources(_DockerLikeSandbox(), "/workspace/repo", "master")

    assert sources == [
        str(shared_state_root / "runtime" / "daily_audit" / "bundled-skills" / "superpowers"),
        str(shared_state_root / "runtime" / "daily_audit" / "bundled-skills" / "self_repo"),
    ]
    assert (
        shared_state_root
        / "runtime"
        / "daily_audit"
        / "bundled-skills"
        / "superpowers"
        / "test-driven-development"
        / "SKILL.md"
    ).read_text(encoding="utf-8").startswith("---")
    assert (
        shared_state_root
        / "runtime"
        / "daily_audit"
        / "bundled-skills"
        / "self_repo"
        / "direction-finder"
        / "SKILL.md"
    ).read_text(encoding="utf-8").startswith("---")


def test_daily_audit_observed_subagent_runnable_emits_open_review_span(monkeypatch):
    spans = []
    interactions = []

    @contextmanager
    def _fake_span(name, **kwargs):
        spans.append((name, kwargs))
        yield SimpleNamespace(
            set_input=lambda value, mime_type=None: interactions.append(("input", value, mime_type)),
            set_output=lambda value, mime_type=None: interactions.append(("output", value, mime_type)),
            add_event=lambda name, attributes=None: interactions.append(("event", name, attributes)),
            record_exception=lambda exc: interactions.append(("exception", type(exc).__name__)),
            set_error_status=lambda description: interactions.append(("error", description)),
        )

    class _FakeRunnable:
        async def ainvoke(self, payload, config=None, **kwargs):
            return {"payload": payload, "config": config, "kwargs": kwargs}

    monkeypatch.setattr(daily_graph, "start_open_review_span", _fake_span)

    wrapped = daily_graph._ObservedDailyAuditRunnable(
        _FakeRunnable(),
        span_name="open_review.daily_audit.subagent.analysis_specialist",
        tags=["daily_audit", "subagent"],
        static_attributes={
            "open_review.parent_role": "daily_audit",
            "open_review.daily_subagent": "analysis_specialist",
            "open_review.run_id": "run-123",
        },
    )

    result = asyncio.run(
        wrapped.ainvoke(
            {"messages": [{"role": "user", "content": "investigate"}]},
            config={
                "configurable": {
                    "project_id": "team/project",
                    "thread_id": "daily_audit:team/project:run-123:primary",
                }
            },
        )
    )

    assert result["config"]["configurable"]["project_id"] == "team/project"
    assert spans == [
        (
            "open_review.daily_audit.subagent.analysis_specialist",
            {
                "attributes": {
                    "open_review.parent_role": "daily_audit",
                    "open_review.daily_subagent": "analysis_specialist",
                    "open_review.run_id": "run-123",
                    "open_review.project_id": "team/project",
                    "open_review.session_id": "daily_audit:team/project:run-123:primary",
                },
                "metadata": None,
                "tags": ["daily_audit", "subagent"],
                "span_kind": "agent",
            },
        )
    ]
    assert any(item[0] == "input" for item in interactions)
    assert any(item[0] == "output" for item in interactions)
    assert any(item[0] == "event" and item[1] == "invoke_completed" for item in interactions)


def test_mention_review_scope_tool_returns_frozen_snapshot():
    context = _context()
    context.mr_snapshot.changed_files = [
        ChangedFileContext(
            file_path="src/router.cpp",
            old_path="src/router.cpp",
            diff="@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
            new_file=False,
            deleted_file=False,
            renamed_file=False,
            added_lines=[1],
        ),
        ChangedFileContext(
            file_path="include/router.h",
            old_path="include/router.h",
            diff="@@ -0,0 +1 @@\n+int router();\n",
            new_file=True,
            deleted_file=False,
            renamed_file=False,
            added_lines=[1],
        ),
    ]

    tool = mention_graph._build_review_scope_tool(context)

    snapshot = tool()
    file_snapshot = tool(file_path="src/router.cpp")

    assert snapshot["scope_source"] == "orchestrator_frozen_snapshot"
    assert snapshot["changed_files"] == [
        {"path": "src/router.cpp", "status": "modified"},
        {"path": "include/router.h", "status": "new"},
    ]
    assert file_snapshot["path"] == "src/router.cpp"
    assert file_snapshot["status"] == "modified"
    assert file_snapshot["diff"] == "@@ -1 +1 @@\n-int x;\n+int x = 1;\n"
