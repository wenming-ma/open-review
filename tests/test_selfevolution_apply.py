from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from agent.selfevolution import apply as apply_mod
from agent.selfevolution.common import (
    SelfEvolutionAssetOutcome,
    finalize_agent_self_evolution_result,
)


def test_finalize_agent_self_evolution_result_fails_when_any_asset_failed_even_with_outputs():
    result = finalize_agent_self_evolution_result(
        agent_type="mention",
        outputs=[Path("/tmp/review-swarm.md")],
        asset_outcomes=[
            SelfEvolutionAssetOutcome(
                asset_type="skill",
                target="review-swarm",
                status="applied",
                commit_sha="abc123",
            ),
            SelfEvolutionAssetOutcome(
                asset_type="tool_description",
                target="task",
                status="failed",
                reason="git worktree add failed",
            ),
        ],
    )

    assert result.status == "failed"
    assert result.reason == "git worktree add failed"
    assert result.outputs == [Path("/tmp/review-swarm.md")]


def test_finalize_agent_self_evolution_result_skips_when_only_rejected_and_nonproductive_skips():
    result = finalize_agent_self_evolution_result(
        agent_type="daily_audit",
        outputs=[],
        asset_outcomes=[
            SelfEvolutionAssetOutcome(
                asset_type="prompt",
                target="direction-finder-prompt",
                status="rejected",
                reason="held-out regression for direction-finder-prompt: baseline=0.3 candidate=0.2",
                gate_reason="held-out regression for direction-finder-prompt: baseline=0.3 candidate=0.2",
            ),
            SelfEvolutionAssetOutcome(
                asset_type="code",
                target="agent/scenes/daily_audit/orchestrator.py",
                status="skipped",
                reason="code_self_evolution_placeholder",
            ),
        ],
    )

    assert result.status == "skipped"
    assert result.reason == "held-out regression for direction-finder-prompt: baseline=0.3 candidate=0.2"


def test_shared_skills_are_not_text_layer_evolution_targets():
    allowed, reason = apply_mod.validate_text_layer_change_set(
        "auto_review",
        ["agent/scenes/skills/superpowers/test-driven-development/SKILL.md"],
    )

    assert allowed is False
    assert reason == "non_text_layer_change_detected"


def test_apply_skill_candidate_direct_merge_uses_unique_worktree_run_ids(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    target_file = repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "skills" / "review-swarm" / "SKILL.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("old body\n", encoding="utf-8")
    candidate_path = tmp_path / "candidate.md"
    candidate_path.write_text("new body\n", encoding="utf-8")

    calls: list[dict[str, object]] = []

    monkeypatch.setattr(apply_mod, "_self_repo_service_root", lambda default_branch=None: repo_root)
    monkeypatch.setattr(apply_mod, "find_scene_skill_path", lambda agent_type, skill_name, default_branch=None: target_file)

    def fake_create_worktree(**kwargs):
        calls.append(kwargs)
        worktree_dir = tmp_path / f"worktree-{len(calls)}"
        worktree_dir.mkdir(parents=True, exist_ok=True)
        return str(worktree_dir)

    monkeypatch.setattr(apply_mod, "_create_service_worktree", fake_create_worktree)
    monkeypatch.setattr(apply_mod, "_cleanup_service_worktree", lambda **kwargs: None)
    monkeypatch.setattr(apply_mod, "_commit_all_and_get_sha_local", lambda **kwargs: "abc123")
    monkeypatch.setattr(apply_mod, "_fast_forward_service_repo", lambda **kwargs: None)

    first = apply_mod.apply_skill_candidate_direct_merge(
        agent_type="mention",
        skill_name="review-swarm",
        candidate_path=candidate_path,
        default_branch="main",
        commit_message="chore: evolve mention skill review-swarm",
    )
    second = apply_mod.apply_skill_candidate_direct_merge(
        agent_type="mention",
        skill_name="review-swarm",
        candidate_path=candidate_path,
        default_branch="main",
        commit_message="chore: evolve mention skill review-swarm",
    )

    assert first.status == "applied"
    assert second.status == "applied"
    assert len(calls) == 2
    assert calls[0]["run_id"] != calls[1]["run_id"]
    assert calls[0]["run_id"] != "SKILL"
    assert "review-swarm" in str(calls[0]["run_id"])


def test_apply_text_candidate_via_service_worktree_holds_repo_lock_for_entire_transaction(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    target_file = repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "prompts" / "author-prompt.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("old prompt\n", encoding="utf-8")
    candidate_path = tmp_path / "candidate.md"
    candidate_path.write_text("new prompt\n", encoding="utf-8")
    worktree_dir = tmp_path / "worktree"
    events: list[str] = []

    monkeypatch.setattr(apply_mod, "_self_repo_service_root", lambda default_branch=None: repo_root)

    @contextmanager
    def fake_lock(_repo_root: Path):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(apply_mod, "_service_repo_apply_lock", fake_lock)

    def fake_create_worktree(**kwargs):
        events.append("create")
        worktree_target = worktree_dir / "agent" / "scenes" / "mention" / "selfevolution" / "prompts" / "author-prompt.md"
        worktree_target.parent.mkdir(parents=True, exist_ok=True)
        worktree_target.write_text(target_file.read_text(encoding="utf-8"), encoding="utf-8")
        return str(worktree_dir)

    monkeypatch.setattr(apply_mod, "_create_service_worktree", fake_create_worktree)
    monkeypatch.setattr(apply_mod, "_commit_all_and_get_sha_local", lambda **kwargs: events.append("commit") or "abc123")
    monkeypatch.setattr(apply_mod, "_fast_forward_service_repo", lambda **kwargs: events.append("ff"))
    monkeypatch.setattr(apply_mod, "_cleanup_service_worktree", lambda **kwargs: events.append("cleanup"))

    result = apply_mod._apply_text_candidate_via_service_worktree(
        agent_type="mention",
        asset_type="prompt",
        target="author-prompt",
        target_relative_path="agent/scenes/mention/selfevolution/prompts/author-prompt.md",
        candidate_path=candidate_path,
        commit_message="chore: evolve mention prompt author-prompt",
        default_branch="main",
    )

    assert result.status == "applied"
    assert events == ["lock-enter", "create", "commit", "ff", "cleanup", "lock-exit"]


def test_apply_uses_self_repo_branch_not_project_default_branch(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    target_file = repo_root / "agent" / "scenes" / "mention" / "selfevolution" / "prompts" / "author-prompt.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("old prompt\n", encoding="utf-8")
    candidate_path = tmp_path / "candidate.md"
    candidate_path.write_text("new prompt\n", encoding="utf-8")
    worktree_dir = tmp_path / "worktree"
    calls: dict[str, object] = {}

    monkeypatch.setattr(apply_mod, "_self_repo_service_root", lambda default_branch=None: repo_root)
    monkeypatch.setattr(apply_mod, "_detect_service_default_branch", lambda repo_root: "main")

    @contextmanager
    def fake_lock(_repo_root: Path):
        yield

    monkeypatch.setattr(apply_mod, "_service_repo_apply_lock", fake_lock)

    def fake_create_worktree(**kwargs):
        calls["create"] = kwargs
        worktree_target = worktree_dir / "agent" / "scenes" / "mention" / "selfevolution" / "prompts" / "author-prompt.md"
        worktree_target.parent.mkdir(parents=True, exist_ok=True)
        worktree_target.write_text(target_file.read_text(encoding="utf-8"), encoding="utf-8")
        return str(worktree_dir)

    monkeypatch.setattr(apply_mod, "_create_service_worktree", fake_create_worktree)
    monkeypatch.setattr(apply_mod, "_commit_all_and_get_sha_local", lambda **kwargs: "abc123")
    monkeypatch.setattr(apply_mod, "_fast_forward_service_repo", lambda **kwargs: calls.setdefault("ff", kwargs))
    monkeypatch.setattr(apply_mod, "_cleanup_service_worktree", lambda **kwargs: None)

    result = apply_mod._apply_text_candidate_via_service_worktree(
        agent_type="mention",
        asset_type="prompt",
        target="author-prompt",
        target_relative_path="agent/scenes/mention/selfevolution/prompts/author-prompt.md",
        candidate_path=candidate_path,
        commit_message="chore: evolve mention prompt author-prompt",
        default_branch="master",
    )

    assert result.status == "applied"
    assert calls["create"]["default_branch"] == "main"
    assert calls["ff"]["default_branch"] == "main"
