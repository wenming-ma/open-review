from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.selfevolution.common import SelfEvolutionRejected
from agent.selfevolution.common import AgentSelfEvolutionSpec
from agent.scenes.mention.selfevolution.engine import build_mention_prompt_eval_examples


def test_gepa_logger_tee_close_preserves_primary_stream():
    import agent.selfevolution.common  # noqa: F401
    from gepa.logging.logger import Tee

    class _DummyStream:
        def __init__(self):
            self.closed = False
            self.writes: list[str] = []

        def write(self, text: str):
            if self.closed:
                raise ValueError("I/O operation on closed file.")
            self.writes.append(text)

        def flush(self):
            if self.closed:
                raise ValueError("I/O operation on closed file.")

        def close(self):
            self.closed = True

        def isatty(self):
            return False

    primary = _DummyStream()
    secondary = _DummyStream()
    tee = Tee(primary, secondary)

    tee.close()
    tee.write("hello")

    assert primary.closed is False
    assert secondary.closed is True
    assert primary.writes == ["hello"]


def test_build_mention_prompt_eval_examples_include_gitlab_identifiers(tmp_path, monkeypatch):
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
            "system_prompt": "historical mention prompt",
            "input_messages_json": [{"role": "user", "content": "please investigate router regression"}],
            "messages_json": [{"role": "assistant", "content": "grounded reply"}],
            "result_json": {
                "reply_markdown": "grounded reply",
                "reply_kind": "analysis",
                "used_subagents": ["dialogs"],
            },
            "metadata_json": {
                "logical_run_id": "mention-run-1",
                "project_id": "team/project",
                "mr_iid": 42,
                "note_id": 12,
                "discussion_id": "disc-1",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base-1",
                "start_sha": "start-1",
                "head_sha": "head-1",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
            },
        },
    )
    tracking.append_feedback_event(
        "mention-runtime-1",
        {
            "feedback_kind": "mr_note",
            "author": "reviewer",
            "payload_json": {"note": "please provide stronger evidence"},
        },
    )

    examples = build_mention_prompt_eval_examples("team/project", "author-prompt")

    assert len(examples) == 1
    example = examples[0]
    assert example.task_input == "please investigate router regression"
    assert example.historical_system_prompt == "historical mention prompt"
    assert example.metadata["head_sha"] == "head-1"
    assert example.metadata["base_sha"] == "base-1"
    assert example.metadata["source_branch"] == "feature/router"
    assert example.feedback_events[0]["feedback_kind"] == "mr_note"


def test_run_gepa_prompt_self_evolution_optimizes_allowlisted_prompts_only(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_gepa_prompt_self_evolution

    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    calls: list[str] = []

    spec = AgentSelfEvolutionSpec(
        agent_type="mention",
        skill_root=lambda _branch=None: Path(tmp_path / "skills"),
        prompt_root=lambda _branch=None: Path(tmp_path / "prompts"),
        tool_metadata_path=lambda _branch=None: Path(tmp_path / "tools" / "tool_descriptions.json"),
        code_targets_path=lambda _branch=None: Path(tmp_path / "code" / "code_targets.json"),
        build_skill_examples=lambda *_args, **_kwargs: [],
        build_prompt_examples=lambda *_args, **_kwargs: [],
        build_tool_examples=lambda *_args, **_kwargs: [],
        prompt_allowlist=("author-prompt",),
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="mention",
                prompt_target=target_name,
                source_run_id="mention-run-1",
                runtime_run_id="mention-runtime-1",
                project_id=project_id,
                task_input="please explain this regression",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "mention.author"},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"head_sha": "head-1"},
            )
        ],
        render_prompt_candidate=lambda target_name, candidate_text, example: f"{target_name}\n{candidate_text}\n{example.task_input}",
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "author-prompt.md").write_text("author baseline", encoding="utf-8")
    (tmp_path / "prompts" / "reviewer-prompt.md").write_text("reviewer baseline", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text('{"review_scope": "desc"}', encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text('["agent/scenes/mention/orchestrator.py"]', encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("agent.selfevolution.common.list_skills", lambda *_args, **_kwargs: ["dialogs"])
    monkeypatch.setattr("agent.selfevolution.common.list_prompt_targets", lambda *_args, **_kwargs: ["author-prompt", "reviewer-prompt"])
    monkeypatch.setattr("agent.selfevolution.common.list_tool_description_targets", lambda *_args, **_kwargs: ["review_scope"])
    monkeypatch.setattr("agent.selfevolution.common.list_code_targets", lambda *_args, **_kwargs: ["agent/scenes/mention/orchestrator.py"])

    def _fake_run_prompt_target_gepa_evolution(**kwargs):
        calls.append(kwargs["target_name"])
        candidate_dir = Path(kwargs["candidate_dir"])
        candidate_dir.mkdir(parents=True, exist_ok=True)
        path = candidate_dir / f'{kwargs["target_name"]}.md'
        path.write_text("candidate prompt", encoding="utf-8")
        return path, {"baseline_score": 0.4, "candidate_score": 0.8, "heldout_examples": 1}

    monkeypatch.setattr(
        "agent.selfevolution.gepa.run_prompt_target_gepa_evolution",
        _fake_run_prompt_target_gepa_evolution,
    )

    result = run_gepa_prompt_self_evolution(
        spec=spec,
        project_id="team/project",
        default_branch="main",
        enabled=True,
    )

    assert calls == ["author-prompt"]
    assert result.status == "reported"
    assert [item.status for item in result.asset_outcomes] == [
        "skipped",
        "candidate_generated",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert [item.target for item in result.asset_outcomes] == [
        "dialogs",
        "author-prompt",
        "reviewer-prompt",
        "review_scope",
        "agent/scenes/mention/orchestrator.py",
    ]
    assert {item.reason for item in result.asset_outcomes if item.status == "skipped"} == {"not_in_v1_scope"}


def test_run_gepa_prompt_self_evolution_auto_applies_prompt_when_self_repo_enabled(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_gepa_prompt_self_evolution

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
        prompt_allowlist=("author-prompt",),
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="mention",
                prompt_target=target_name,
                source_run_id="mention-run-1",
                runtime_run_id="mention-runtime-1",
                project_id=project_id,
                task_input="please explain this regression",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "mention.author"},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"head_sha": "head-1"},
            )
        ],
        render_prompt_candidate=lambda _target_name, candidate_text, _example: candidate_text,
        apply_prompt_candidate=lambda _project_id, _target, _candidate_path, _default_branch=None: SimpleNamespace(
            status="applied",
            commit_sha="abc123",
        ),
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "author-prompt.md").write_text("author baseline", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("agent.selfevolution.common.list_skills", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("agent.selfevolution.common.list_prompt_targets", lambda *_args, **_kwargs: ["author-prompt"])
    monkeypatch.setattr("agent.selfevolution.common.list_tool_description_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("agent.selfevolution.common.list_code_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "agent.selfevolution.gepa.run_prompt_target_gepa_evolution",
        lambda **kwargs: (
            kwargs["candidate_dir"].joinpath("author-prompt.md"),
            {"baseline_score": 0.4, "candidate_score": 0.8, "heldout_examples": 1},
        ),
    )
    candidate_dir = tmp_path / "runtime" / "mention" / "evolution" / "team__project" / "candidates" / "prompts" / "author-prompt"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "author-prompt.md").write_text("candidate prompt", encoding="utf-8")

    result = run_gepa_prompt_self_evolution(
        spec=spec,
        project_id="team/project",
        default_branch="main",
        enabled=True,
    )

    assert result.asset_outcomes[0].status == "applied"
    assert result.asset_outcomes[0].commit_sha == "abc123"


def test_run_gepa_prompt_self_evolution_skips_when_prompt_candidate_is_only_rejected(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_gepa_prompt_self_evolution

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
        prompt_allowlist=("author-prompt",),
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="mention",
                prompt_target=target_name,
                source_run_id="mention-run-1",
                runtime_run_id="mention-runtime-1",
                project_id=project_id,
                task_input="please explain this regression",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "mention.author"},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"head_sha": "head-1"},
            )
        ],
        render_prompt_candidate=lambda _target_name, candidate_text, _example: candidate_text,
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "author-prompt.md").write_text("author baseline", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("agent.selfevolution.common.list_skills", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("agent.selfevolution.common.list_prompt_targets", lambda *_args, **_kwargs: ["author-prompt"])
    monkeypatch.setattr("agent.selfevolution.common.list_tool_description_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("agent.selfevolution.common.list_code_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "agent.selfevolution.gepa.run_prompt_target_gepa_evolution",
        lambda **_kwargs: (_ for _ in ()).throw(
            SelfEvolutionRejected("held-out regression for author-prompt: baseline=0.800 candidate=0.500")
        ),
    )

    result = run_gepa_prompt_self_evolution(
        spec=spec,
        project_id="team/project",
        default_branch="main",
        enabled=True,
    )

    assert result.status == "skipped"
    assert result.reason == "held-out regression for author-prompt: baseline=0.800 candidate=0.500"
    assert result.asset_outcomes[0].status == "rejected"


def test_run_prompt_target_gepa_evolution_supports_current_gepa_engine_config(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_prompt_target_gepa_evolution

    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    captured = {}

    spec = AgentSelfEvolutionSpec(
        agent_type="mention",
        skill_root=lambda _branch=None: Path(tmp_path / "skills"),
        prompt_root=lambda _branch=None: Path(tmp_path / "prompts"),
        tool_metadata_path=lambda _branch=None: Path(tmp_path / "tools" / "tool_descriptions.json"),
        code_targets_path=lambda _branch=None: Path(tmp_path / "code" / "code_targets.json"),
        build_skill_examples=lambda *_args, **_kwargs: [],
        build_prompt_examples=lambda *_args, **_kwargs: [],
        build_tool_examples=lambda *_args, **_kwargs: [],
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="mention",
                prompt_target=target_name,
                source_run_id="mention-run-1",
                runtime_run_id="mention-runtime-1",
                project_id=project_id,
                task_input="please explain this regression",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "mention.author", "result_json": {"reply_markdown": "reply"}},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"head_sha": "head-1"},
            )
        ]
        * 3,
        render_prompt_candidate=lambda _target_name, candidate_text, _example: candidate_text,
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "author-prompt.md").write_text("author baseline", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.gepa._make_reflection_lm", lambda: (lambda _prompt: "reflection"))

    def _fake_optimize_anything(**kwargs):
        captured["config"] = kwargs["config"]
        return SimpleNamespace(best_candidate="candidate prompt")

    monkeypatch.setattr("agent.selfevolution.gepa.optimize_anything", _fake_optimize_anything)

    candidate_path, evaluation = run_prompt_target_gepa_evolution(
        spec=spec,
        project_id="team/project",
        target_name="author-prompt",
        default_branch="main",
    )

    assert candidate_path.exists()
    assert candidate_path.read_text(encoding="utf-8") == "candidate prompt\n"
    assert evaluation["train_examples"] >= 1
    assert evaluation["val_examples"] >= 1
    assert captured["config"].engine.candidate_selection_strategy == "pareto"


def test_run_prompt_target_gepa_evolution_extracts_prompt_content_from_json_candidate(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_prompt_target_gepa_evolution

    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    spec = AgentSelfEvolutionSpec(
        agent_type="daily_audit",
        skill_root=lambda _branch=None: Path(tmp_path / "skills"),
        prompt_root=lambda _branch=None: Path(tmp_path / "prompts"),
        tool_metadata_path=lambda _branch=None: Path(tmp_path / "tools" / "tool_descriptions.json"),
        code_targets_path=lambda _branch=None: Path(tmp_path / "code" / "code_targets.json"),
        build_skill_examples=lambda *_args, **_kwargs: [],
        build_prompt_examples=lambda *_args, **_kwargs: [],
        build_tool_examples=lambda *_args, **_kwargs: [],
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="daily_audit",
                prompt_target=target_name,
                source_run_id="daily-run-1",
                runtime_run_id="daily-runtime-1",
                project_id=project_id,
                task_input="Audit one bounded workflow.",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "daily_audit.analysis", "result_json": {"summary_markdown": "ok"}},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"repo_dir": "/repo", "default_branch": "main"},
            )
        ]
        * 3,
        render_prompt_candidate=lambda _target_name, candidate_text, _example: candidate_text,
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "workflow-auditor-prompt.md").write_text("baseline prompt", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.gepa._make_reflection_lm", lambda: (lambda _prompt: "reflection"))

    wrapped_candidate = json.dumps(
        {
            "component_type": "prompt",
            "name": "workflow-auditor-prompt",
            "version": "2.1",
            "content": "You are the Daily Audit workflow-auditor.\nRepository root: {repo_dir}\n",
        }
    )

    monkeypatch.setattr(
        "agent.selfevolution.gepa.optimize_anything",
        lambda **_kwargs: SimpleNamespace(best_candidate=wrapped_candidate),
    )

    candidate_path, _evaluation = run_prompt_target_gepa_evolution(
        spec=spec,
        project_id="team/project",
        target_name="workflow-auditor-prompt",
        default_branch="main",
    )

    assert candidate_path.read_text(encoding="utf-8") == (
        "You are the Daily Audit workflow-auditor.\nRepository root: {repo_dir}\n"
    )


def test_run_prompt_target_gepa_evolution_uses_dedicated_run_dir(monkeypatch, tmp_path):
    from agent.selfevolution.gepa import PromptTaskExample, run_prompt_target_gepa_evolution

    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    captured = {}

    spec = AgentSelfEvolutionSpec(
        agent_type="mention",
        skill_root=lambda _branch=None: Path(tmp_path / "skills"),
        prompt_root=lambda _branch=None: Path(tmp_path / "prompts"),
        tool_metadata_path=lambda _branch=None: Path(tmp_path / "tools" / "tool_descriptions.json"),
        code_targets_path=lambda _branch=None: Path(tmp_path / "code" / "code_targets.json"),
        build_skill_examples=lambda *_args, **_kwargs: [],
        build_prompt_examples=lambda *_args, **_kwargs: [],
        build_tool_examples=lambda *_args, **_kwargs: [],
        build_prompt_eval_examples=lambda project_id, target_name, limit=20: [
            PromptTaskExample(
                agent_type="mention",
                prompt_target=target_name,
                source_run_id="mention-run-1",
                runtime_run_id="mention-runtime-1",
                project_id=project_id,
                task_input="Summarize the MR.",
                historical_system_prompt="old prompt",
                agent_record={"record_kind": "mention.author", "result_json": {"reply_markdown": "reply"}},
                trigger_events=[],
                feedback_events=[],
                published_objects=[],
                metadata={"repo_dir": "/repo"},
            )
        ]
        * 3,
        render_prompt_candidate=lambda _target_name, candidate_text, _example: candidate_text,
        evaluation_profile=lambda _target_name: {"instruction_following": 0.5, "task_accuracy": 0.5},
    )

    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "author-prompt.md").write_text("author baseline", encoding="utf-8")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "tool_descriptions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "code").mkdir(parents=True, exist_ok=True)
    (tmp_path / "code" / "code_targets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("agent.selfevolution.gepa._make_reflection_lm", lambda: (lambda _prompt: "reflection"))

    def _fake_optimize_anything(**kwargs):
        captured["run_dir"] = kwargs["config"].engine.run_dir
        return SimpleNamespace(best_candidate="candidate prompt")

    monkeypatch.setattr("agent.selfevolution.gepa.optimize_anything", _fake_optimize_anything)

    candidate_dir = tmp_path / "runtime" / "mention" / "evolution" / "team__project" / "candidates" / "prompts" / "author-prompt"
    candidate_path, _evaluation = run_prompt_target_gepa_evolution(
        spec=spec,
        project_id="team/project",
        target_name="author-prompt",
        default_branch="main",
        candidate_dir=candidate_dir,
    )

    assert candidate_path.parent == candidate_dir
    assert Path(captured["run_dir"]) != candidate_dir
    run_dir = Path(captured["run_dir"])
    assert run_dir.parts[-4:-1] == ("runs", "prompts", "author-prompt")
