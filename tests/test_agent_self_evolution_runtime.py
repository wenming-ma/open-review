from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.selfevolution.common import AgentSelfEvolutionSpec
from agent.selfevolution.gepa import run_gepa_self_evolution_for_spec
from agent.selfevolution.runtime import run_agent_self_evolution_cycle
from agent.scenes.auto_review.selfevolution.engine import (
    build_auto_review_skill_eval_examples,
    run_auto_review_evolution_cycle,
)
from agent.scenes.daily_audit.selfevolution.engine import run_daily_audit_evolution_cycle
from agent.scenes.mention.selfevolution.engine import (
    build_mention_skill_eval_examples,
    run_mention_evolution_cycle,
)


def test_run_agent_self_evolution_cycle_dispatches_daily_audit(monkeypatch):
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_daily_audit_evolution_cycle",
        lambda **kwargs: SimpleNamespace(status="reported", output_count=1, kwargs=kwargs),
    )

    result = run_agent_self_evolution_cycle(
        agent_type="daily_audit",
        project_id="team/project",
        default_branch="main",
    )

    assert result.status == "reported"
    assert result.kwargs["project_id"] == "team/project"


def test_run_agent_self_evolution_cycle_dispatches_mention(monkeypatch):
    monkeypatch.setattr(
        "agent.scenes.mention.selfevolution.engine.run_mention_evolution_cycle",
        lambda **kwargs: SimpleNamespace(status="reported", output_count=1, kwargs=kwargs),
    )

    result = run_agent_self_evolution_cycle(
        agent_type="mention",
        project_id="team/project",
        default_branch="main",
    )

    assert result.status == "reported"
    assert result.kwargs["project_id"] == "team/project"


def test_run_agent_self_evolution_cycle_dispatches_auto_review(monkeypatch):
    monkeypatch.setattr(
        "agent.scenes.auto_review.selfevolution.engine.run_auto_review_evolution_cycle",
        lambda **kwargs: SimpleNamespace(status="reported", output_count=1, kwargs=kwargs),
    )

    result = run_agent_self_evolution_cycle(
        agent_type="auto_review",
        project_id="team/project",
        default_branch="main",
    )

    assert result.status == "reported"
    assert result.kwargs["project_id"] == "team/project"


def test_run_mention_evolution_cycle_uses_shared_gepa_runner(monkeypatch):
    monkeypatch.setattr(settings, "MENTION_SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(
        "agent.scenes.mention.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **kwargs: SimpleNamespace(status="reported", output_count=1, kwargs=kwargs),
    )

    result = run_mention_evolution_cycle(project_id="team/project", default_branch="main")

    assert result.status == "reported"
    assert result.kwargs["project_id"] == "team/project"
    assert result.kwargs["enabled"] is True


def test_run_auto_review_evolution_cycle_uses_shared_gepa_runner(monkeypatch):
    monkeypatch.setattr(settings, "AUTO_REVIEW_SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(
        "agent.scenes.auto_review.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **kwargs: SimpleNamespace(status="reported", output_count=1, kwargs=kwargs),
    )

    result = run_auto_review_evolution_cycle(project_id="team/project", default_branch="main")

    assert result.status == "reported"
    assert result.kwargs["project_id"] == "team/project"
    assert result.kwargs["enabled"] is True


def test_run_daily_audit_evolution_cycle_skips_when_no_targets_are_configured(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_AUDIT_SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(
        "agent.scenes.daily_audit.selfevolution.engine.run_gepa_self_evolution_for_spec",
        lambda **kwargs: SimpleNamespace(status="skipped", reason="no_targets_configured", output_count=0, asset_outcomes=[], kwargs=kwargs),
    )

    result = run_daily_audit_evolution_cycle(project_id="team/project", default_branch="main")

    assert result.status == "skipped"
    assert result.reason == "no_targets_configured"
    assert result.output_count == 0
    assert result.asset_outcomes == []
    assert result.kwargs["project_id"] == "team/project"


def test_run_gepa_self_evolution_for_spec_applies_text_assets_and_skips_code_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    spec = AgentSelfEvolutionSpec(
        agent_type="mention",
        skill_root=lambda _branch=None: Path(tmp_path / "skills"),
        prompt_root=lambda _branch=None: Path(tmp_path / "prompts"),
        tool_metadata_path=lambda _branch=None: Path(tmp_path / "tools" / "tool_descriptions.json"),
        code_targets_path=lambda _branch=None: Path(tmp_path / "code" / "code_targets.json"),
        build_skill_examples=lambda *_args, **_kwargs: [],
        build_prompt_examples=lambda *_args, **_kwargs: [],
        build_tool_examples=lambda *_args, **_kwargs: [],
        apply_skill_candidate=lambda _project_id, _target, _candidate_path, _default_branch=None: SimpleNamespace(
            status="applied",
            commit_sha="skill-sha",
        ),
        apply_tool_description_candidate=lambda _project_id, _target, _candidate_path, _default_branch=None: SimpleNamespace(
            status="applied",
            commit_sha="tool-sha",
        ),
    )

    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.common.list_skills", lambda *_args, **_kwargs: ["review-swarm"])
    monkeypatch.setattr("agent.selfevolution.common.list_prompt_targets", lambda *_args, **_kwargs: ["author-prompt"])
    monkeypatch.setattr("agent.selfevolution.common.list_tool_description_targets", lambda *_args, **_kwargs: ["review_scope"])
    monkeypatch.setattr("agent.selfevolution.common.list_code_targets", lambda *_args, **_kwargs: ["agent/scenes/mention/orchestrator.py"])
    monkeypatch.setattr(
        "agent.selfevolution.common.run_skill_evolution",
        lambda **kwargs: (Path(tmp_path / "runtime" / "skill.md"), SimpleNamespace(baseline_score=0.1, candidate_score=0.2, heldout_examples=1, gate_reason=None)),
    )
    monkeypatch.setattr(
        "agent.selfevolution.common.run_tool_description_evolution",
        lambda **kwargs: (Path(tmp_path / "runtime" / "tool.txt"), SimpleNamespace(baseline_score=0.1, candidate_score=0.2, heldout_examples=1, gate_reason=None)),
    )
    monkeypatch.setattr(
        "agent.selfevolution.gepa.run_prompt_target_gepa_evolution",
        lambda **kwargs: (
            Path(tmp_path / "runtime" / f"{kwargs['target_name']}.md"),
            {
                "baseline_score": 0.1,
                "candidate_score": 0.2,
                "heldout_examples": 1,
                "train_examples": 1,
                "val_examples": 1,
                "dimension_scores_summary": {},
                "feedback_coverage": 0,
                "materialization_failures": 0,
            },
        ),
    )
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "skill.md").write_text("skill", encoding="utf-8")
    (tmp_path / "runtime" / "tool.txt").write_text("tool", encoding="utf-8")
    (tmp_path / "runtime" / "author-prompt.md").write_text("prompt", encoding="utf-8")

    result = run_gepa_self_evolution_for_spec(
        spec=spec,
        project_id="team/project",
        default_branch="main",
        enabled=True,
    )

    assert [item.asset_type for item in result.asset_outcomes] == ["skill", "prompt", "tool_description", "code"]
    assert [item.status for item in result.asset_outcomes] == ["applied", "candidate_generated", "applied", "skipped"]
    assert [item.commit_sha for item in result.asset_outcomes[:3]] == ["skill-sha", None, "tool-sha"]
    assert result.asset_outcomes[-1].reason == "code_self_evolution_placeholder"


def test_build_mention_skill_eval_examples_uses_raw_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "mention-runtime-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "mention",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-21T10:00:00+08:00",
        }
    )
    tracking.append_agent_record(
        "mention-runtime-1",
        {
            "record_kind": "mention.author",
            "input_messages_json": [{"role": "user", "content": "please explain the regression"}],
            "result_json": {
                "reply_markdown": "这是 grounded 的回复。",
                "reply_kind": "analysis",
                "used_subagents": ["dialogs"],
            },
            "metadata_json": {"logical_run_id": "mention-run-1", "note_id": 12},
        },
    )

    examples = build_mention_skill_eval_examples("team/project")

    assert len(examples) == 1
    assert examples[0].task_input == "please explain the regression"
    assert examples[0].expected_behavior == "这是 grounded 的回复。"
    assert examples[0].source_run_id == "mention-run-1"


def test_build_auto_review_skill_eval_examples_uses_raw_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "review-runtime-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-21T10:00:00+08:00",
        }
    )
    tracking.append_agent_record(
        "review-runtime-1",
        {
            "record_kind": "auto_review.director",
            "input_messages_json": [{"role": "user", "content": "review this merge request"}],
            "result_json": {
                "summary": "存在一个高风险问题",
                "recommendation": "建议重新修改",
                "confirmed_findings": [{"summary": "router 会在空指针下崩溃"}],
                "suspicious_findings": [],
                "open_questions": [],
                "specialist_reports": [{"lane": "correctness"}],
            },
            "metadata_json": {"logical_run_id": "review-run-1"},
        },
    )

    examples = build_auto_review_skill_eval_examples("team/project")

    assert len(examples) == 1
    assert examples[0].task_input == "review this merge request"
    assert "router 会在空指针下崩溃" in examples[0].expected_behavior
    assert examples[0].source_run_id == "review-run-1"
