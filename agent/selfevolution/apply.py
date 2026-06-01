"""Shared apply helpers for agent-scoped self-evolution assets."""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.selfevolution.assets import (
    find_scene_skill_path,
    list_scene_code_targets,
    scene_prompt_root,
    scene_tool_metadata_path,
)
from agent.selfevolution.git import run_safe_git, run_safe_git_stdout
from agent.selfevolution.repo import configured_self_repo_branch, ensure_self_repo_checkout


@dataclass(frozen=True)
class SelfEvolutionApplyResult:
    status: str
    reason: str | None = None
    commit_sha: str | None = None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _run_local_git(args: list[str], *, cwd: Path) -> str:
    return run_safe_git_stdout(args, cwd=cwd)


def _detect_service_default_branch(repo_root: Path) -> str:
    try:
        branch = _run_local_git(["branch", "--show-current"], cwd=repo_root)
        if branch:
            return branch
    except Exception:
        pass
    return configured_self_repo_branch()


def _slug_component(value: str, *, fallback: str, max_length: int = 40) -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    if not text:
        text = fallback
    return text[:max_length].rstrip("-") or fallback


def _build_apply_run_id(*, agent_type: str, asset_type: str, target: str) -> str:
    unique = uuid4().hex[:10]
    return "-".join(
        [
            _slug_component(agent_type, fallback="agent", max_length=16),
            _slug_component(asset_type, fallback="asset", max_length=16),
            _slug_component(target, fallback="target", max_length=48),
            unique,
        ]
    )


def _apply_lock_path(repo_root: Path) -> Path:
    return repo_root.parent / f".{repo_root.name}.apply.lock"


@contextlib.contextmanager
def _service_repo_apply_lock(repo_root: Path):
    lock_path = _apply_lock_path(repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _create_service_worktree(*, repo_root: Path, default_branch: str, run_id: str) -> str:
    worktrees_root = repo_root / ".worktrees" / "evolution"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = worktrees_root / run_id
    temp_branch = f"open-review-evolution-{Path(run_id).name}"
    run_safe_git(["worktree", "remove", "--force", str(worktree_dir)], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["worktree", "prune"], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    if run_safe_git(["show-ref", "--verify", "--quiet", f"refs/heads/{temp_branch}"], cwd=repo_root, check=False).returncode == 0:
        _run_local_git(["branch", "-D", temp_branch], cwd=repo_root)
    run_safe_git_stdout(["worktree", "add", "-b", temp_branch, str(worktree_dir), default_branch], cwd=repo_root, safe_paths=[worktree_dir])
    return str(worktree_dir)


def _cleanup_service_worktree(*, repo_root: Path, worktree_dir: str) -> None:
    run_safe_git(["worktree", "remove", "--force", worktree_dir], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["worktree", "prune"], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    branch_name = f"open-review-evolution-{Path(worktree_dir).name}"
    run_safe_git(["branch", "-D", branch_name], cwd=repo_root, check=False)


def _commit_all_and_get_sha_local(*, worktree_root: Path, message: str) -> str:
    _run_local_git(["add", "-A"], cwd=worktree_root)
    _run_local_git(["commit", "-m", message], cwd=worktree_root)
    return _run_local_git(["rev-parse", "HEAD"], cwd=worktree_root)


def _fast_forward_service_repo(*, repo_root: Path, default_branch: str, commit_sha: str) -> None:
    _run_local_git(["checkout", default_branch], cwd=repo_root)
    _run_local_git(["merge", "--ff-only", commit_sha], cwd=repo_root)


def _self_repo_service_root(default_branch: str | None = None) -> Path:
    return ensure_self_repo_checkout(default_branch)


def _apply_text_candidate_via_service_worktree(
    *,
    agent_type: str,
    asset_type: str,
    target: str,
    target_relative_path: str,
    candidate_path: Path,
    commit_message: str,
    default_branch: str | None = None,
    verify_fn: Callable[[Path], None] | None = None,
) -> SelfEvolutionApplyResult:
    repo_root = _self_repo_service_root(default_branch)
    with _service_repo_apply_lock(repo_root):
        branch = _detect_service_default_branch(repo_root)
        target_path = repo_root / target_relative_path
        candidate_content = candidate_path.read_text(encoding="utf-8")
        current_content = target_path.read_text(encoding="utf-8")
        if candidate_content == current_content:
            return SelfEvolutionApplyResult(status="skipped", reason="already_applied")

        worktree_dir = _create_service_worktree(
            repo_root=repo_root,
            default_branch=branch,
            run_id=_build_apply_run_id(agent_type=agent_type, asset_type=asset_type, target=target),
        )
        try:
            worktree_root = Path(worktree_dir)
            worktree_target = worktree_root / target_relative_path
            worktree_target.parent.mkdir(parents=True, exist_ok=True)
            worktree_target.write_text(candidate_content, encoding="utf-8")
            if callable(verify_fn):
                verify_fn(worktree_root)
            commit_sha = _commit_all_and_get_sha_local(worktree_root=worktree_root, message=commit_message)
            _fast_forward_service_repo(repo_root=repo_root, default_branch=branch, commit_sha=commit_sha)
            return SelfEvolutionApplyResult(status="applied", commit_sha=commit_sha)
        finally:
            _cleanup_service_worktree(repo_root=repo_root, worktree_dir=worktree_dir)


def is_text_layer_evolution_path(agent_type: str, path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    prefix = f"agent/scenes/{agent_type}/selfevolution/"
    if normalized.startswith(f"{prefix}prompts/") and normalized.endswith(".py"):
        return True
    if normalized.startswith(f"{prefix}skills/") and normalized.endswith("/SKILL.md"):
        return True
    if normalized.startswith(f"{prefix}prompts/") and normalized.endswith(".md"):
        return True
    if normalized == f"{prefix}tools/tool_descriptions.json":
        return True
    return False


def validate_text_layer_change_set(agent_type: str, paths: list[str]) -> tuple[bool, str | None]:
    if not paths:
        return False, "empty_change_set"
    if all(is_text_layer_evolution_path(agent_type, path) for path in paths):
        return True, None
    return False, "non_text_layer_change_detected"


def validate_code_layer_change_set(agent_type: str, paths: list[str], *, default_branch: str | None = None) -> tuple[bool, str | None]:
    if not paths:
        return False, "empty_change_set"
    allowed_targets = set(list_scene_code_targets(agent_type, default_branch=default_branch))
    if all(path in allowed_targets for path in paths):
        return True, None
    return False, "non_code_target_change_detected"


def apply_skill_candidate_direct_merge(
    *,
    agent_type: str,
    skill_name: str,
    candidate_path: Path,
    default_branch: str | None = None,
    commit_message: str,
) -> SelfEvolutionApplyResult:
    target_skill = find_scene_skill_path(agent_type, skill_name, default_branch=default_branch)
    repo_root = _self_repo_service_root(default_branch)
    relative_target = target_skill.resolve().relative_to(repo_root)
    allowed, reason = validate_text_layer_change_set(agent_type, [relative_target.as_posix()])
    if not allowed:
        return SelfEvolutionApplyResult(status="failed", reason=reason or "skill_candidate_not_eligible")
    return _apply_text_candidate_via_service_worktree(
        agent_type=agent_type,
        asset_type="skill",
        target=skill_name,
        target_relative_path=relative_target.as_posix(),
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=commit_message,
    )


def apply_prompt_candidate_direct_merge(
    *,
    agent_type: str,
    target_name: str,
    candidate_path: Path,
    default_branch: str | None = None,
    commit_message: str,
) -> SelfEvolutionApplyResult:
    repo_root = _self_repo_service_root(default_branch)
    target_prompt = scene_prompt_root(agent_type, default_branch=default_branch) / f"{target_name}.md"
    relative_target = target_prompt.resolve().relative_to(repo_root)
    allowed, reason = validate_text_layer_change_set(agent_type, [relative_target.as_posix()])
    if not allowed:
        return SelfEvolutionApplyResult(status="failed", reason=reason or "prompt_candidate_not_eligible")
    return _apply_text_candidate_via_service_worktree(
        agent_type=agent_type,
        asset_type="prompt",
        target=target_name,
        target_relative_path=relative_target.as_posix(),
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=commit_message,
    )


def apply_tool_description_candidate_direct_merge(
    *,
    agent_type: str,
    target_name: str,
    candidate_path: Path,
    default_branch: str | None = None,
    commit_message: str,
) -> SelfEvolutionApplyResult:
    repo_root = _self_repo_service_root(default_branch)
    target_path = scene_tool_metadata_path(agent_type, default_branch=default_branch)
    relative_target = target_path.resolve().relative_to(repo_root)
    allowed, reason = validate_text_layer_change_set(agent_type, [relative_target.as_posix()])
    if not allowed:
        return SelfEvolutionApplyResult(status="failed", reason=reason or "tool_candidate_not_eligible")

    with _service_repo_apply_lock(repo_root):
        candidate_content = candidate_path.read_text(encoding="utf-8").strip()
        current_descriptions = json.loads(target_path.read_text(encoding="utf-8"))
        if current_descriptions.get(target_name, "").strip() == candidate_content:
            return SelfEvolutionApplyResult(status="skipped", reason="already_applied")

        branch = _detect_service_default_branch(repo_root)
        worktree_dir = _create_service_worktree(
            repo_root=repo_root,
            default_branch=branch,
            run_id=_build_apply_run_id(agent_type=agent_type, asset_type="tool", target=target_name),
        )
        try:
            worktree_root = Path(worktree_dir)
            worktree_target = worktree_root / relative_target
            worktree_target.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(worktree_target.read_text(encoding="utf-8"))
            data[target_name] = candidate_content
            worktree_target.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            commit_sha = _commit_all_and_get_sha_local(worktree_root=worktree_root, message=commit_message)
            _fast_forward_service_repo(repo_root=repo_root, default_branch=branch, commit_sha=commit_sha)
            return SelfEvolutionApplyResult(status="applied", commit_sha=commit_sha)
        finally:
            _cleanup_service_worktree(repo_root=repo_root, worktree_dir=worktree_dir)


def apply_code_candidate_direct_merge(
    *,
    agent_type: str,
    target_path: str,
    candidate_path: Path,
    default_branch: str | None = None,
    commit_message: str,
    verify_fn: Callable[[Path], None] | None = None,
) -> SelfEvolutionApplyResult:
    allowed, reason = validate_code_layer_change_set(agent_type, [target_path], default_branch=default_branch)
    if not allowed:
        return SelfEvolutionApplyResult(status="failed", reason=reason or "code_candidate_not_eligible")
    relative_target = Path(target_path).as_posix()
    return _apply_text_candidate_via_service_worktree(
        agent_type=agent_type,
        asset_type="code",
        target=target_path,
        target_relative_path=relative_target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=commit_message,
        verify_fn=verify_fn,
    )
