"""Local service-repo bootstrap and access for self-evolution."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
from pathlib import Path

from agent.config import RUNTIME_ROOT, ensure_state_layout, settings
from agent.sandbox.manager import _configure_repo_identity
from agent.selfevolution.git import run_safe_git_stdout

_SERVICE_REPO_NAME = "open-review"
_SERVICE_REPO_BRANCH = "main"
_SEED_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "reference",
    "*.pyc",
)
_REQUIRED_SELFEVOLUTION_ASSETS = (
    "agent/scenes/mention/selfevolution",
    "agent/scenes/auto_review/selfevolution",
    "agent/scenes/daily_audit/selfevolution",
)


def self_repo_enabled() -> bool:
    return True


def configured_self_repo_branch(default_branch: str | None = None) -> str:
    del default_branch
    return _SERVICE_REPO_BRANCH


def self_repo_root() -> Path:
    source_root = _source_repo_root()
    runtime_root = Path(settings.OPEN_REVIEW_RUNTIME_ROOT)
    if runtime_root == RUNTIME_ROOT and (source_root / ".git").exists():
        return source_root
    return runtime_root.parent / "service-repo" / _SERVICE_REPO_NAME


def _source_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_local_git(args: list[str], *, cwd: Path) -> str:
    return run_safe_git_stdout(args, cwd=cwd)


def _seed_local_service_repo(*, source_root: Path, repo_root: Path) -> None:
    if repo_root.exists():
        raise RuntimeError(f"service_repo_seed_target_exists:{repo_root}")
    shutil.copytree(source_root, repo_root, ignore=_SEED_IGNORE, ignore_dangling_symlinks=True)


def _git_init_local_service_repo(repo_root: Path) -> None:
    _run_local_git(["init"], cwd=repo_root)
    _configure_repo_identity(str(repo_root))
    _run_local_git(["checkout", "-B", _SERVICE_REPO_BRANCH], cwd=repo_root)
    _run_local_git(["add", "-A"], cwd=repo_root)
    _run_local_git(["commit", "-m", "chore: bootstrap local service repo"], cwd=repo_root)


def _validate_local_service_repo(repo_root: Path) -> None:
    if not (repo_root / ".git").exists():
        raise RuntimeError(f"invalid_local_service_repo_missing_git:{repo_root}")
    for relative in _REQUIRED_SELFEVOLUTION_ASSETS:
        if not (repo_root / relative).exists():
            raise RuntimeError(f"invalid_local_service_repo_missing_asset:{relative}")


def _bootstrap_lock_path(repo_root: Path) -> Path:
    return repo_root.parent / f".{repo_root.name}.bootstrap.lock"


@contextlib.contextmanager
def _local_service_repo_bootstrap_lock(repo_root: Path):
    lock_path = _bootstrap_lock_path(repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reset_local_service_repo(repo_root: Path) -> None:
    if not repo_root.exists():
        return
    if repo_root.is_symlink() or repo_root.is_file():
        repo_root.unlink()
        return
    shutil.rmtree(repo_root)


def _bootstrap_local_service_repo(*, source_root: Path, repo_root: Path) -> None:
    _seed_local_service_repo(source_root=source_root, repo_root=repo_root)
    _git_init_local_service_repo(repo_root)


def _sync_shared_skill_assets(*, source_root: Path, repo_root: Path) -> None:
    if repo_root == source_root:
        return
    source_shared = source_root / "agent" / "scenes" / "skills"
    if not source_shared.exists():
        return
    target_shared = repo_root / "agent" / "scenes" / "skills"
    shutil.copytree(source_shared, target_shared, dirs_exist_ok=True, ignore=_SEED_IGNORE, ignore_dangling_symlinks=True)


def _is_recoverable_local_service_repo_error(exc: RuntimeError) -> bool:
    return str(exc).startswith("invalid_local_service_repo_")


def ensure_self_repo_checkout(default_branch: str | None = None) -> Path:
    del default_branch
    ensure_state_layout()
    repo_root = self_repo_root()
    source_root = _source_repo_root()
    if repo_root == source_root:
        _validate_local_service_repo(repo_root)
        return repo_root

    repo_root.parent.mkdir(parents=True, exist_ok=True)
    with _local_service_repo_bootstrap_lock(repo_root):
        try:
            _validate_local_service_repo(repo_root)
        except RuntimeError as exc:
            if not _is_recoverable_local_service_repo_error(exc):
                raise
            _reset_local_service_repo(repo_root)
            _bootstrap_local_service_repo(source_root=source_root, repo_root=repo_root)
            _validate_local_service_repo(repo_root)
        _sync_shared_skill_assets(source_root=source_root, repo_root=repo_root)
    return repo_root


def self_repo_python_path(repo_root: Path | None = None) -> Path:
    root = repo_root or self_repo_root()
    return root / ".venv" / "bin" / "python"


def selfevolution_state_root() -> Path:
    return Path(os.path.dirname(settings.OPEN_REVIEW_RUNTIME_ROOT))
