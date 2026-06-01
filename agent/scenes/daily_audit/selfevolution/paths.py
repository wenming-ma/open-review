"""Path helpers for daily-audit selfevolution assets."""

from __future__ import annotations

from pathlib import Path

_SCENE_ROOT = Path(__file__).resolve().parent
_SCENE_REPO_RELATIVE_ROOT = Path("agent") / "scenes" / "daily_audit"


def local_daily_audit_selfevolution_root() -> Path:
    return _SCENE_ROOT


def local_daily_audit_skill_root() -> Path:
    return local_daily_audit_selfevolution_root() / "skills"


def local_daily_audit_prompt_root() -> Path:
    return local_daily_audit_selfevolution_root() / "prompts"


def local_daily_audit_tool_metadata_path() -> Path:
    return local_daily_audit_selfevolution_root() / "tools" / "tool_descriptions.json"


def local_daily_audit_code_targets_path() -> Path:
    return local_daily_audit_selfevolution_root() / "code" / "code_targets.json"


def repo_daily_audit_selfevolution_root(repo_root: Path) -> Path:
    return Path(repo_root) / _SCENE_REPO_RELATIVE_ROOT / "selfevolution"


def repo_daily_audit_skill_root(repo_root: Path) -> Path:
    return repo_daily_audit_selfevolution_root(repo_root) / "skills"
