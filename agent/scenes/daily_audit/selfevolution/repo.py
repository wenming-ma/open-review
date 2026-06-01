"""Backwards-compatible wrappers over the local self-evolution service repo."""

from __future__ import annotations

from pathlib import Path

from agent.selfevolution.repo import (
    configured_self_repo_branch as _shared_configured_self_repo_branch,
    ensure_self_repo_checkout as ensure_daily_audit_self_repo_checkout,
    self_repo_python_path as daily_audit_self_repo_python_path,
    self_repo_root as daily_audit_self_repo_root,
    selfevolution_state_root as daily_audit_state_root,
)
from agent.scenes.daily_audit.selfevolution.paths import repo_daily_audit_skill_root


def _configured_self_repo_branch(default_branch: str | None = None) -> str:
    return _shared_configured_self_repo_branch(default_branch)


def daily_audit_self_skill_root(repo_root: Path | None = None) -> Path:
    root = repo_root or daily_audit_self_repo_root()
    return repo_daily_audit_skill_root(root)
