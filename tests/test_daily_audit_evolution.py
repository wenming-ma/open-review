"""Tests for daily audit self-evolution plumbing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.scenes.daily_audit.persistence.store import reset_daily_audit_persistence_store
from agent.scenes.daily_audit.selfevolution.engine import (
    _render_daily_audit_prompt_candidate,
    apply_evolved_code_direct_merge,
    apply_evolved_skill_direct_merge,
    build_direction_eval_examples,
    build_skill_eval_examples,
    find_daily_audit_skill,
    list_daily_audit_code_targets,
    list_daily_audit_prompt_targets,
    list_daily_audit_skills,
    list_daily_audit_tool_description_targets,
    maybe_run_daily_audit_self_evolution,
    run_daily_audit_code_evolution,
    run_daily_audit_prompt_evolution,
    run_daily_audit_skill_evolution,
    run_daily_audit_tool_description_evolution,
    validate_code_layer_change_set,
    validate_text_layer_change_set,
)
from agent.scenes.daily_audit.selfevolution.evaluation import DailyAuditEvalExample
from agent.scenes.daily_audit.selfevolution.evaluation import HeldoutEvaluationResult
from agent.selfevolution.gepa import PromptTaskExample


@pytest.fixture(autouse=True)
def _reset_evolution_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    reset_daily_audit_persistence_store()
    yield
    reset_controlplane_services()
    reset_daily_audit_persistence_store()

def _seed_raw_analysis_run(
    *,
    project_id: str,
    run_id: str,
    payload: dict[str, object],
    feedback_events: list[dict[str, object]] | None = None,
) -> None:
    tracking = get_tracking_service()
    runtime_run_id = f"runtime-{run_id}"
    tracking.record_run(
        {
            "run_id": runtime_run_id,
            "actor_key": f"{project_id}!daily_audit",
            "project_id": project_id,
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": f"2026-04-20T10:{len(tracking.list_runs(project_id=project_id, event_type='daily_audit', limit=200)):02d}:00+08:00",
        }
    )
    tracking.append_agent_record(
        runtime_run_id,
        {
            "record_kind": "daily_audit.analysis",
            "thread_id": f"daily_audit:{project_id}:{run_id}:primary",
            "system_prompt": "analysis prompt",
            "input_messages_json": [{"role": "user", "content": str(payload.get("summary") or payload.get("summary_markdown") or "")}],
            "messages_json": [
                {"role": "user", "content": str(payload.get("summary") or payload.get("summary_markdown") or "")},
                {"role": "assistant", "content": str(payload.get("report_markdown") or "")},
            ],
            "result_json": {
                "summary_markdown": str(payload.get("summary_markdown") or payload.get("summary") or ""),
                "report_markdown": str(payload.get("report_markdown") or ""),
                "recommended_action": str(payload.get("status") or "report_only"),
                "used_subagents": list(payload.get("used_subagents") or []),
            },
            "started_at": f"2026-04-20T10:{len(tracking.list_runs(project_id=project_id, event_type='daily_audit', limit=200)):02d}:00+08:00",
            "completed_at": f"2026-04-20T10:{len(tracking.list_runs(project_id=project_id, event_type='daily_audit', limit=200)):02d}:30+08:00",
            "metadata_json": {
                "logical_run_id": run_id,
                "unit_label": str(payload.get("unit_label") or ""),
                "file_path": str(payload.get("file_path") or ""),
            },
        },
    )
    for event in feedback_events or []:
        tracking.append_feedback_event(runtime_run_id, event)


def test_build_skill_eval_examples_returns_empty_without_raw_runs():
    assert build_skill_eval_examples("team/project") == []


def test_validate_text_layer_change_set_allows_only_daily_text_assets():
    allowed, reason = validate_text_layer_change_set(
        [
            "agent/scenes/daily_audit/selfevolution/prompts/__init__.py",
            "agent/scenes/daily_audit/selfevolution/skills/workflow-auditor/SKILL.md",
            "agent/scenes/daily_audit/selfevolution/prompts/workflow-auditor-prompt.md",
            "agent/scenes/daily_audit/selfevolution/tools/tool_descriptions.json",
        ]
    )
    blocked, blocked_reason = validate_text_layer_change_set(
        [
            "agent/scenes/daily_audit/orchestrator.py",
            "agent/runtime/worker.py",
        ]
    )

    assert allowed is True
    assert reason is None
    assert blocked is False
    assert blocked_reason == "non_text_layer_change_detected"


def test_validate_code_layer_change_set_allows_only_daily_code_targets():
    allowed, reason = validate_code_layer_change_set(
        [
            "agent/scenes/daily_audit/orchestrator.py",
            "agent/scenes/daily_audit/persistence/raw_records.py",
        ]
    )
    blocked, blocked_reason = validate_code_layer_change_set(
        [
            "agent/runtime/models.py",
            "agent/scenes/daily_audit/orchestrator.py",
        ]
    )

    assert allowed is True
    assert reason is None
    assert blocked is False
    assert blocked_reason == "non_code_target_change_detected"


def test_find_daily_audit_skill_locates_primary_skill():
    path = find_daily_audit_skill("direction-finder")

    assert path.name == "SKILL.md"
    assert path.parent.name == "direction-finder"


def test_find_daily_audit_skill_prefers_self_repo_when_configured(monkeypatch, tmp_path):
    self_repo_root = tmp_path / "service-repo" / "open-review"
    target = (
        self_repo_root
        / "agent"
        / "scenes"
        / "daily_audit"
        / "selfevolution"
        / "skills"
        / "direction-finder"
        / "SKILL.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nname: direction-finder\ndescription: self repo\n---\n\nself repo body\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.ensure_daily_audit_self_repo_checkout",
        lambda default_branch=None: self_repo_root,
    )

    path = find_daily_audit_skill("direction-finder")

    assert path == target


def test_list_daily_audit_skills_includes_service_roles():
    skills = list_daily_audit_skills()

    assert "direction-finder" in skills
    assert "workflow-auditor" in skills
    assert "candidate-scout" in skills
    assert "focus-selector" in skills
    assert "verification-agent" in skills


def test_list_daily_audit_prompt_targets_and_tool_description_targets():
    prompt_targets = list_daily_audit_prompt_targets()
    tool_targets = list_daily_audit_tool_description_targets()
    code_targets = list_daily_audit_code_targets()

    assert "direction-finder-prompt" in prompt_targets
    assert "workflow-auditor-prompt" in prompt_targets
    assert "auxiliary-agent-prompt" in prompt_targets
    assert "candidate_scout" in tool_targets
    assert "verification_agent" in tool_targets
    assert "agent/scenes/daily_audit/orchestrator.py" in code_targets


def test_build_skill_eval_examples_uses_raw_tracked_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={
            "unit_type": "function",
            "unit_label": "foo()",
            "finding_count": 2,
            "status": "reported",
            "summary": "Inspect foo() for bounded optimization opportunities.",
            "report_markdown": "Identified repeated lookup inside the loop.",
        },
    )

    examples = build_skill_eval_examples("team/project")

    assert examples == [
        DailyAuditEvalExample(
            task_input="Inspect foo() for bounded optimization opportunities.",
            expected_behavior="Identified repeated lookup inside the loop.",
            source_run_id="run-1",
            unit_label="foo()",
            recommended_action="reported",
        )
    ]


def test_render_daily_audit_prompt_candidate_leaves_json_schema_braces_literal():
    candidate_text = """Repository root: {repo_dir}

Required Output Schema:
{
  "workflow_name": "<short descriptive name>",
  "entry_points": [
    {
      "file": "<path from repo root>",
      "line": <number>
    }
  ]
}
"""
    example = PromptTaskExample(
        agent_type="daily_audit",
        prompt_target="direction-finder-prompt",
        source_run_id="run-1",
        runtime_run_id="runtime-1",
        project_id="team/service",
        task_input="Audit a user-triggered workflow.",
        historical_system_prompt="baseline",
        agent_record={},
        trigger_events=[],
        feedback_events=[],
        published_objects=[],
        metadata={"repo_dir": "/workspace/repo", "default_branch": "master"},
    )

    rendered = _render_daily_audit_prompt_candidate("direction-finder-prompt", candidate_text, example)

    assert "Repository root: /workspace/repo" in rendered
    assert '"workflow_name": "<short descriptive name>"' in rendered
    assert '"file": "<path from repo root>"' in rendered


def test_build_skill_eval_examples_prefers_raw_tracked_run_records(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    tracking.append_agent_record(
        "runtime-run-1",
        {
            "record_kind": "daily_audit.analysis",
            "thread_id": "daily_audit:team/project:run-1:primary",
            "system_prompt": "analysis prompt",
            "input_messages_json": [{"role": "user", "content": "analyze foo"}],
            "messages_json": [
                {"role": "user", "content": "Inspect foo() for bounded optimization opportunities."},
                {"role": "assistant", "content": "Identified repeated lookup inside the loop."},
            ],
            "result_json": {
                "summary_markdown": "Inspect foo() for bounded optimization opportunities.",
                "report_markdown": "Identified repeated lookup inside the loop.",
                "recommended_action": "report_only",
                "used_subagents": ["verification_agent"],
            },
            "started_at": "2026-04-20T10:00:00+08:00",
            "completed_at": "2026-04-20T10:05:00+08:00",
            "metadata_json": {
                "logical_run_id": "run-1",
                "unit_label": "foo()",
                "file_path": "src/foo.cpp",
            },
        },
    )
    tracking.append_feedback_event(
        "runtime-run-1",
        {
            "feedback_kind": "issue_note",
            "association_method": "published_issue_iid",
            "author": "reviewer",
            "payload_json": {"note": "Please include a reproducer."},
        },
    )

    examples = build_skill_eval_examples("team/project")

    assert examples == [
        DailyAuditEvalExample(
            task_input="Inspect foo() for bounded optimization opportunities.",
            expected_behavior="Identified repeated lookup inside the loop.\n\nExternal feedback:\n- issue_note by reviewer",
            source_run_id="run-1",
            unit_label="foo()",
            file_path="src/foo.cpp",
            recommended_action="report_only",
            used_subagents=("verification_agent",),
        )
    ]


def test_build_skill_eval_examples_captures_replay_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={
            "summary": "Inspect foo() for bounded optimization opportunities.",
            "report_markdown": "Identified repeated lookup inside the loop.",
            "unit_label": "foo()",
            "file_path": "src/foo.cpp",
            "status": "report_only",
            "used_subagents": ["candidate_scout", "verification_agent"],
        },
    )

    example = build_skill_eval_examples("team/project")[0]

    assert example.unit_label == "foo()"
    assert example.file_path == "src/foo.cpp"
    assert example.recommended_action == "report_only"
    assert example.used_subagents == ("candidate_scout", "verification_agent")


def test_build_direction_eval_examples_reads_raw_direction_record(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    tracking.append_agent_record(
        "runtime-run-1",
        {
            "record_kind": "daily_audit.direction",
            "thread_id": "daily_audit:team/project:run-1:direction",
            "system_prompt": "direction prompt",
            "input_messages_json": [{"role": "user", "content": "pick one workflow"}],
            "messages_json": [{"role": "assistant", "content": "Pick Refresh All Orders."}],
            "result_json": {
                "selected_unit": {
                    "unit_type": "action_workflow",
                    "label": "Refresh All Orders",
                    "file_path": "services/orders/refresh_jobs.py",
                    "entrypoint_kind": "toolbar_action",
                    "entrypoint_symbol": "OrderActions.refreshAll",
                    "workflow_summary": "Refresh all orders from the toolbar action and trace refresh scheduling.",
                    "entry_evidence": ["toolbar appends OrderActions.refreshAll"],
                },
                "selection_reasoning": "Bounded and user-facing.",
                "used_subagents": ["direction_history"],
            },
            "started_at": "2026-04-20T10:00:00+08:00",
            "completed_at": "2026-04-20T10:02:00+08:00",
            "metadata_json": {"logical_run_id": "run-1"},
        },
    )

    examples = build_direction_eval_examples("team/project")

    assert examples == [
        DailyAuditEvalExample(
            task_input=(
                "Discover one user-triggered action workflow for today's audit. "
                "Explore the repository yourself, pick one bounded workflow, and justify it with concrete entry evidence."
            ),
            expected_behavior=(
                "Workflow: Refresh All Orders\n"
                "Entrypoint: OrderActions.refreshAll\n"
                "Summary: Refresh all orders from the toolbar action and trace refresh scheduling.\n"
                "Why: Bounded and user-facing.\n"
                "Evidence: toolbar appends OrderActions.refreshAll"
            ),
            source_run_id="run-1",
            unit_label="Refresh All Orders",
            file_path="services/orders/refresh_jobs.py",
            recommended_action="report_only",
            used_subagents=("direction_history",),
        )
    ]


def test_run_daily_audit_skill_evolution_uses_shared_gepa_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    calls = {}

    def _fake_shared_run_skill_evolution(**kwargs):
        calls["kwargs"] = kwargs
        return Path(tmp_path / "candidate.md"), HeldoutEvaluationResult(0.4, 0.7, 1, None)

    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.shared_run_skill_evolution",
        _fake_shared_run_skill_evolution,
    )

    output = run_daily_audit_skill_evolution(
        project_id="team/project",
        skill_name="primary-daily-optimizer",
        iterations=3,
    )

    assert output.name == "candidate.md"
    assert calls["kwargs"]["skill_name"] == "primary-daily-optimizer"
    assert calls["kwargs"]["iterations"] == 3


def test_run_daily_audit_skill_evolution_can_return_heldout_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.shared_run_skill_evolution",
        lambda **_kwargs: (Path(tmp_path / "candidate" / "SKILL.md"), HeldoutEvaluationResult(0.4, 0.7, 1, None)),
    )

    candidate_path, eval_result = run_daily_audit_skill_evolution(
        project_id="team/project",
        skill_name="primary-daily-optimizer",
        iterations=3,
        return_metadata=True,
    )

    assert candidate_path.name == "SKILL.md"
    assert eval_result.heldout_examples == 1
    assert eval_result.gate_reason is None
    assert eval_result.candidate_score is not None


def test_run_daily_audit_skill_evolution_rejects_worse_candidate_on_heldout_examples(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.shared_run_skill_evolution",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("held-out regression for primary-daily-optimizer: baseline=0.7 candidate=0.2")),
    )

    with pytest.raises(RuntimeError, match="held-out"):
        run_daily_audit_skill_evolution(
            project_id="team/project",
            skill_name="primary-daily-optimizer",
            iterations=3,
        )


def test_run_daily_audit_prompt_evolution_writes_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.shared_run_prompt_evolution",
        lambda **_kwargs: (Path(tmp_path / "workflow-auditor-prompt.md"), HeldoutEvaluationResult(0.3, 0.6, 1, None)),
    )

    output = run_daily_audit_prompt_evolution(
        project_id="team/project",
        target_name="primary-agent-prompt",
        iterations=2,
    )

    assert output.name == "workflow-auditor-prompt.md"


def test_run_daily_audit_tool_description_evolution_writes_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.shared_run_tool_description_evolution",
        lambda **_kwargs: (Path(tmp_path / "candidate_scout" / "tool_description.txt"), HeldoutEvaluationResult(0.3, 0.6, 1, None)),
    )

    output = run_daily_audit_tool_description_evolution(
        project_id="team/project",
        target_name="candidate_scout",
        iterations=2,
    )

    assert output.name == "tool_description.txt"


def test_run_daily_audit_code_evolution_invokes_external_evolver(monkeypatch, tmp_path):
    with pytest.raises(Exception, match="code_self_evolution_placeholder"):
        run_daily_audit_code_evolution(
            project_id="team/project",
            target_path="agent/scenes/daily_audit/orchestrator.py",
            iterations=4,
        )


def test_run_daily_audit_skill_evolution_uses_sandbox_cli_when_sandbox_provided(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))

    class FakeSandbox:
        def __init__(self):
            self.commands = []

        def execute(self, command: str, timeout: int | None = None):
                del timeout
                self.commands.append(command)
                candidate_path = tmp_path / "daily_audit" / "evolution" / "team__project" / "candidates" / "primary-daily-optimizer" / "SKILL.md"
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                candidate_path.write_text("---\nname: primary-daily-optimizer\n---\n\nupdated\n", encoding="utf-8")
                return SimpleNamespace(
                    exit_code=0,
                    output=(
                        f'{{"candidate_path": "{candidate_path}", "baseline_score": 0.4, '
                        '"candidate_score": 0.6, "heldout_examples": 1}\n'
                    ),
                )

    sandbox = FakeSandbox()
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: Path("/var/lib/open-review/service-repo/open-review"))

    candidate_path, metadata = run_daily_audit_skill_evolution(
        project_id="team/project",
        skill_name="primary-daily-optimizer",
        iterations=3,
        return_metadata=True,
        sandbox=sandbox,
        default_branch="main",
    )

    assert candidate_path.name == "SKILL.md"
    assert metadata.candidate_score == 0.6
    assert any("selfevolution.cli" in command for command in sandbox.commands)
    assert any("--skill-name primary-daily-optimizer" in command for command in sandbox.commands)


def test_apply_evolved_code_direct_merge_uses_sandbox_cli_when_sandbox_provided(monkeypatch, tmp_path):
    candidate_path = tmp_path / "orchestrator.py"
    candidate_path.write_text("def evolved():\n    return 1\n", encoding="utf-8")

    class FakeSandbox:
        def __init__(self):
            self.commands = []

        def execute(self, command: str, timeout: int | None = None):
            del timeout
            self.commands.append(command)
            return SimpleNamespace(exit_code=0, output='{"commit_sha": "abc1234"}\n')

    sandbox = FakeSandbox()
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine.ensure_daily_audit_self_repo_checkout", lambda default_branch=None: Path("/var/lib/open-review/service-repo/open-review"))

    commit_sha = apply_evolved_code_direct_merge(
        project_id="team/project",
        target_path="agent/scenes/daily_audit/orchestrator.py",
        candidate_path=candidate_path,
        sandbox=sandbox,
        default_branch="main",
    )

    assert commit_sha == "abc1234"
    assert any("apply-code" in command for command in sandbox.commands)
    assert any("--target-path agent/scenes/daily_audit/orchestrator.py" in command for command in sandbox.commands)


def test_maybe_run_daily_audit_self_evolution_returns_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", False)
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={"summary": "one", "report_markdown": "report"},
    )

    called = {"count": 0}
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_daily_audit_skill_evolution",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1),
    )

    result = maybe_run_daily_audit_self_evolution("team/project")

    assert result.status == "skipped"
    assert result.reason == "self_evolution_disabled"
    assert result.output_count == 0
    assert called["count"] == 0


def test_maybe_run_daily_audit_self_evolution_runs_without_sample_or_cooldown_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", True)
    reset_daily_audit_persistence_store()
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={"summary": "one", "report_markdown": "report"},
    )

    expected = SimpleNamespace(
        status="reported",
        outputs=[Path(tmp_path / "primary-daily-optimizer.md")],
        output_count=1,
        asset_outcomes=[
            SimpleNamespace(
                asset_type="skill",
                target="primary-daily-optimizer",
                status="candidate_generated",
                reason=None,
            )
        ],
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **_kwargs: expected,
    )

    result = maybe_run_daily_audit_self_evolution("team/project")

    assert result.status == "reported"
    assert result.outputs == [Path(tmp_path / "primary-daily-optimizer.md")]
    assert result.output_count == 1
    assert any(
        item.asset_type == "skill"
        and item.target == "primary-daily-optimizer"
        and item.status in {"candidate_generated", "applied", "skipped"}
        for item in result.asset_outcomes
    )


def test_maybe_run_daily_audit_self_evolution_triggers_skill_optimizer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_MIN_RUNS", 2)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_MIN_FRESH_RUNS", 1)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_COOLDOWN_HOURS", 0)
    reset_daily_audit_persistence_store()
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={"summary": "one", "report_markdown": "report"},
    )
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-2",
        payload={"summary": "two", "report_markdown": "report"},
    )

    expected = SimpleNamespace(
        status="reported",
        outputs=[
            Path(tmp_path / "primary-daily-optimizer.md"),
            Path(tmp_path / "primary-agent-prompt.prompt"),
            Path(tmp_path / "candidate_scout.tool.txt"),
        ],
        output_count=3,
        asset_outcomes=[
            SimpleNamespace(asset_type="skill", target="primary-daily-optimizer", status="candidate_generated"),
            SimpleNamespace(asset_type="prompt", target="workflow-auditor-prompt", status="candidate_generated"),
            SimpleNamespace(asset_type="tool_description", target="candidate_scout", status="candidate_generated"),
            SimpleNamespace(
                asset_type="code",
                target="agent/scenes/daily_audit/orchestrator.py",
                status="skipped",
                reason="code_self_evolution_placeholder",
            ),
        ],
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **_kwargs: expected,
    )

    result = maybe_run_daily_audit_self_evolution("team/project")

    assert result.status == "reported"
    assert set(result.outputs) == {
        Path(tmp_path / "primary-daily-optimizer.md"),
        Path(tmp_path / "primary-agent-prompt.prompt"),
        Path(tmp_path / "candidate_scout.tool.txt"),
    }
    assert result.output_count == 3
    assert any(
        item.asset_type == "prompt"
        and item.target in {"workflow-auditor-prompt", "primary-agent-prompt"}
        and item.status in {"candidate_generated", "applied", "skipped"}
        for item in result.asset_outcomes
    )
    assert any(item.asset_type == "skill" and item.target == "primary-daily-optimizer" for item in result.asset_outcomes)
    assert any(item.asset_type == "tool_description" and item.target == "candidate_scout" for item in result.asset_outcomes)
    assert any(item.asset_type == "code" and item.target == "agent/scenes/daily_audit/orchestrator.py" for item in result.asset_outcomes)


def test_maybe_run_daily_audit_self_evolution_applies_candidates_when_repo_context_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_MIN_RUNS", 1)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_MIN_FRESH_RUNS", 1)
    monkeypatch.setattr(settings, "DAILY_AUDIT_EVOLUTION_COOLDOWN_HOURS", 0)
    reset_daily_audit_persistence_store()
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={"summary": "one", "report_markdown": "report"},
    )

    expected = SimpleNamespace(
        status="reported",
        outputs=[
            Path(tmp_path / "primary-daily-optimizer.md"),
            Path(tmp_path / "primary-agent-prompt.prompt"),
            Path(tmp_path / "candidate_scout.tool.txt"),
        ],
        output_count=3,
        asset_outcomes=[
            SimpleNamespace(asset_type="skill", target="primary-daily-optimizer", status="applied"),
            SimpleNamespace(asset_type="prompt", target="workflow-auditor-prompt", status="applied"),
            SimpleNamespace(asset_type="tool_description", target="candidate_scout", status="applied"),
            SimpleNamespace(
                asset_type="code",
                target="agent/scenes/daily_audit/orchestrator.py",
                status="skipped",
                reason="code_self_evolution_placeholder",
            ),
        ],
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **_kwargs: expected,
    )

    result = maybe_run_daily_audit_self_evolution(
        "team/project",
        repo_dir=str(tmp_path / "repo"),
        default_branch="main",
    )

    assert result.status == "reported"
    assert set(result.outputs) == {
        Path(tmp_path / "primary-daily-optimizer.md"),
        Path(tmp_path / "primary-agent-prompt.prompt"),
        Path(tmp_path / "candidate_scout.tool.txt"),
    }
    assert result.output_count == 3
    assert any(
        item.asset_type == "prompt"
        and item.target in {"workflow-auditor-prompt", "primary-agent-prompt"}
        and item.status in {"candidate_generated", "applied", "skipped"}
        for item in result.asset_outcomes
    )
    assert any(item.asset_type == "skill" and item.target == "primary-daily-optimizer" for item in result.asset_outcomes)
    assert any(item.asset_type == "tool_description" and item.target == "candidate_scout" for item in result.asset_outcomes)
    assert any(item.asset_type == "code" and item.target == "agent/scenes/daily_audit/orchestrator.py" for item in result.asset_outcomes)


def test_maybe_run_daily_audit_self_evolution_does_not_skip_targets_for_stale_lineage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", True)
    reset_daily_audit_persistence_store()
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-1",
        payload={"summary": "one", "report_markdown": "report"},
    )
    _seed_raw_analysis_run(
        project_id="team/project",
        run_id="run-2",
        payload={"summary": "two", "report_markdown": "report"},
    )

    expected = SimpleNamespace(
        status="reported",
        outputs=[Path(tmp_path / "primary-daily-optimizer.md")],
        output_count=1,
        asset_outcomes=[
            SimpleNamespace(
                asset_type="skill",
                target="primary-daily-optimizer",
                status="candidate_generated",
                reason=None,
            )
        ],
    )
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **_kwargs: expected,
    )

    result = maybe_run_daily_audit_self_evolution("team/project")

    assert result.status == "reported"
    assert result.outputs == [Path(tmp_path / "primary-daily-optimizer.md")]
    assert result.output_count == 1
    assert any(
        item.asset_type == "skill"
        and item.target == "primary-daily-optimizer"
        and item.status in {"candidate_generated", "applied", "skipped"}
        for item in result.asset_outcomes
    )


def test_apply_evolved_skill_direct_merge_uses_temporary_worktree(monkeypatch, tmp_path):
    candidate_path = tmp_path / "candidate.md"
    candidate_path.write_text("---\nname: primary-daily-optimizer\ndescription: x\n---\n\nnew body\n")
    target_file = (
        tmp_path
        / "repo"
        / "agent"
        / "scenes"
        / "daily_audit"
        / "selfevolution"
        / "skills"
        / "primary-daily-optimizer"
        / "SKILL.md"
    )
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("---\nname: primary-daily-optimizer\ndescription: x\n---\n\nold body\n")
    worktree_dir = tmp_path / "worktree"
    worktree_target = (
        worktree_dir
        / "agent"
        / "scenes"
        / "daily_audit"
        / "selfevolution"
        / "skills"
        / "primary-daily-optimizer"
        / "SKILL.md"
    )
    worktree_target.parent.mkdir(parents=True, exist_ok=True)
    worktree_target.write_text(target_file.read_text())

    calls = {}

    monkeypatch.setattr("agent.selfevolution.apply.find_scene_skill_path", lambda agent_type, skill_name, default_branch=None: target_file)
    monkeypatch.setattr("agent.selfevolution.apply._self_repo_service_root", lambda default_branch=None: tmp_path / "repo")

    def fake_create_worktree(**kwargs):
        calls["create"] = kwargs
        return str(worktree_dir)

    def fake_cleanup(**kwargs):
        calls["cleanup"] = kwargs

    def fake_commit(**kwargs):
        calls["commit"] = kwargs
        return "abc1234"

    def fake_ff(**kwargs):
        calls["ff"] = kwargs

    monkeypatch.setattr("agent.selfevolution.apply._create_service_worktree", fake_create_worktree)
    monkeypatch.setattr("agent.selfevolution.apply._cleanup_service_worktree", fake_cleanup)
    monkeypatch.setattr("agent.selfevolution.apply._commit_all_and_get_sha_local", fake_commit)
    monkeypatch.setattr("agent.selfevolution.apply._fast_forward_service_repo", fake_ff)

    commit_sha = apply_evolved_skill_direct_merge(
        project_id="team/project",
        skill_name="primary-daily-optimizer",
        candidate_path=candidate_path,
        repo_dir=str(tmp_path / "repo"),
        default_branch="main",
    )

    assert commit_sha == "abc1234"
    assert worktree_target.read_text() == candidate_path.read_text()
    assert calls["ff"]["default_branch"] == "main"


def test_apply_evolved_code_direct_merge_runs_tests_before_push(monkeypatch, tmp_path):
    candidate_path = tmp_path / "orchestrator.py"
    candidate_path.write_text("def evolved():\n    return 1\n")
    target_file = tmp_path / "repo" / "agent" / "scenes" / "daily_audit" / "orchestrator.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def current():\n    return 0\n")
    worktree_dir = tmp_path / "worktree"
    worktree_target = worktree_dir / "agent" / "scenes" / "daily_audit" / "orchestrator.py"
    worktree_target.parent.mkdir(parents=True, exist_ok=True)
    worktree_target.write_text(target_file.read_text())

    calls = {}

    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._service_repo_root", lambda repo_dir=None, default_branch=None: Path(repo_dir))
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._detect_service_default_branch", lambda repo_root: "main")
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._create_service_worktree", lambda **kwargs: str(worktree_dir))
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._cleanup_service_worktree", lambda **kwargs: calls.setdefault("cleanup", kwargs))
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._run_code_merge_tests", lambda worktree_root: calls.setdefault("test", str(worktree_root)))
    def fake_local_commit(**kwargs):
        calls["commit"] = kwargs
        return "abc1234"

    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._commit_all_and_get_sha_local", fake_local_commit)
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._fast_forward_service_repo", lambda **kwargs: calls.setdefault("ff", kwargs))

    commit_sha = apply_evolved_code_direct_merge(
        project_id="team/project",
        target_path="agent/scenes/daily_audit/orchestrator.py",
        candidate_path=candidate_path,
        repo_dir=str(tmp_path / "repo"),
        default_branch="master",
    )

    assert commit_sha == "abc1234"
    assert worktree_target.read_text() == candidate_path.read_text()
    assert calls["test"] == str(worktree_dir)
    assert calls["ff"]["default_branch"] == "main"


def test_apply_evolved_code_direct_merge_rejects_unrelated_drift(monkeypatch, tmp_path):
    candidate_path = tmp_path / "orchestrator.py"
    candidate_path.write_text("def evolved():\n    return 1\n")
    target_file = tmp_path / "repo" / "agent" / "scenes" / "daily_audit" / "orchestrator.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def current():\n    return 0\n")
    worktree_dir = tmp_path / "worktree"
    worktree_target = worktree_dir / "agent" / "scenes" / "daily_audit" / "orchestrator.py"
    worktree_target.parent.mkdir(parents=True, exist_ok=True)
    worktree_target.write_text(target_file.read_text())

    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._service_repo_root", lambda repo_dir=None, default_branch=None: Path(repo_dir))
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._create_service_worktree", lambda **kwargs: str(worktree_dir))
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._cleanup_service_worktree", lambda **kwargs: None)
    monkeypatch.setattr("agent.scenes.daily_audit.selfevolution.engine._run_code_merge_tests", lambda worktree_root: None)
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine._list_worktree_changed_paths",
        lambda worktree_root: ["agent/scenes/daily_audit/orchestrator.py", "agent/runtime/worker.py"],
    )

    with pytest.raises(RuntimeError, match="drift"):
        apply_evolved_code_direct_merge(
            project_id="team/project",
            target_path="agent/scenes/daily_audit/orchestrator.py",
            candidate_path=candidate_path,
            repo_dir=str(tmp_path / "repo"),
            default_branch="main",
        )
