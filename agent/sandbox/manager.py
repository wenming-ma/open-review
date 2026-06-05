"""Sandbox lifecycle management — one sandbox (with cloned repo) per MR thread."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import SandboxBackendProtocol

from agent.config import ensure_state_layout, settings
from agent.sandbox.docker_backend import DockerSandboxBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxRuntimeConfig:
    sandbox_type: str
    docker_image: str
    local_root_dir: str
    project_cache_root: str


@dataclass
class SandboxHandle:
    mode: str
    docker_image: str | None
    sandbox: SandboxBackendProtocol
    repo_dir: str
    host_root_dir: str


SANDBOX_CACHE: dict[str, SandboxHandle] = {}
_RUNTIME_CONFIG: SandboxRuntimeConfig | None = None


def configure_runtime_sandbox_config(snapshot: dict[str, object] | None = None) -> None:
    """Freeze sandbox settings for the lifetime of the current worker process."""
    data = snapshot or settings.current_snapshot().model_dump()
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = SandboxRuntimeConfig(
        sandbox_type=str(data.get("SANDBOX_TYPE", "local")).strip().lower() or "local",
        docker_image=str(data.get("DOCKER_IMAGE", "")).strip(),
        local_root_dir=str(data.get("LOCAL_SANDBOX_ROOT_DIR", settings.LOCAL_SANDBOX_ROOT_DIR)),
        project_cache_root=str(data.get("PROJECT_CACHE_ROOT", settings.PROJECT_CACHE_ROOT)),
    )


def reset_runtime_sandbox_config() -> None:
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = None


def _runtime_config() -> SandboxRuntimeConfig:
    if _RUNTIME_CONFIG is not None:
        return _RUNTIME_CONFIG
    snapshot = settings.current_snapshot().model_dump()
    return SandboxRuntimeConfig(
        sandbox_type=str(snapshot.get("SANDBOX_TYPE", "local")).strip().lower() or "local",
        docker_image=str(snapshot.get("DOCKER_IMAGE", "")).strip(),
        local_root_dir=str(snapshot.get("LOCAL_SANDBOX_ROOT_DIR", settings.LOCAL_SANDBOX_ROOT_DIR)),
        project_cache_root=str(snapshot.get("PROJECT_CACHE_ROOT", settings.PROJECT_CACHE_ROOT)),
    )


def _project_cache_dir(project_id: str) -> str:
    config = _runtime_config()
    safe_name = project_id.replace("/", "__")
    return os.path.join(config.project_cache_root, f"{safe_name}.git")


def _git_remote_url(project_id: str) -> str:
    return f"{settings.GITLAB_API_URL.rstrip('/')}/{project_id}.git"


def _git_auth_env() -> dict[str, str]:
    token = settings.GITLAB_TOKEN
    auth = b64encode(f"oauth2:{token}".encode()).decode("ascii")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["SSH_ASKPASS"] = "/bin/false"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: Basic {auth}"
    return env


def _run_host_git(
    args: list[str],
    *,
    cwd: str | None = None,
    auth: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    safe_directories: list[str] = []
    if cwd:
        safe_directories.append(os.path.realpath(cwd))
    for arg in args:
        if arg.startswith("/") and (arg.endswith(".git") or os.path.isdir(arg)):
            safe_directories.append(os.path.realpath(arg))
    for path in dict.fromkeys(safe_directories):
        command.extend(["-c", f"safe.directory={path}"])
    command.extend(args)
    return subprocess.run(
        command,
        cwd=cwd,
        env=_git_auth_env() if auth else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_host_git_or_raise(
    args: list[str],
    *,
    cwd: str | None = None,
    auth: bool = False,
    safe_target: str | None = None,
) -> str:
    result = _run_host_git(args, cwd=cwd, auth=auth)
    if result.returncode != 0:
        target = safe_target or cwd or "git"
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or target)
    return result.stdout


def _ensure_project_cache(project_id: str) -> str:
    ensure_state_layout()
    cache_dir = _project_cache_dir(project_id)
    os.makedirs(_runtime_config().project_cache_root, exist_ok=True)
    remote_url = _git_remote_url(project_id)
    if not os.path.isdir(cache_dir):
        logger.info("Creating project mirror cache for %s", project_id)
        result = _run_host_git(["clone", "--mirror", remote_url, cache_dir], auth=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or remote_url)
        return cache_dir

    _run_host_git_or_raise(["remote", "set-url", "origin", remote_url], cwd=cache_dir, safe_target=cache_dir)
    result = _run_host_git(["remote", "update", "--prune"], cwd=cache_dir, auth=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or cache_dir)
    return cache_dir


def _run_or_raise(sandbox: SandboxBackendProtocol, command: str) -> str:
    result = sandbox.execute(command)
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or command)
    return result.output


def _sandbox_root_dir(sandbox: SandboxBackendProtocol) -> str:
    root_dir = getattr(sandbox, "root_dir", None)
    if root_dir:
        return os.fspath(root_dir)
    cwd = getattr(sandbox, "cwd", None)
    if cwd:
        return os.fspath(cwd)
    raise AttributeError(f"{type(sandbox).__name__} does not expose root_dir or cwd")


def _sandbox_host_root_dir(sandbox: SandboxBackendProtocol) -> str:
    host_root_dir = getattr(sandbox, "host_root_dir", None)
    if host_root_dir:
        return os.fspath(host_root_dir)
    return _sandbox_root_dir(sandbox)


def sandbox_shell_path(sandbox: SandboxBackendProtocol, path: str) -> str:
    host_root = _sandbox_host_root_dir(sandbox).rstrip("/")
    visible_root = _sandbox_root_dir(sandbox).rstrip("/")
    candidate = os.fspath(path)
    if candidate == host_root:
        return visible_root
    if candidate.startswith(host_root + "/"):
        return visible_root + candidate[len(host_root) :]
    return candidate


def sandbox_host_path(sandbox: SandboxBackendProtocol, path: str) -> str:
    host_root = _sandbox_host_root_dir(sandbox).rstrip("/")
    visible_root = _sandbox_root_dir(sandbox).rstrip("/")
    candidate = os.fspath(path)
    if candidate == visible_root:
        return host_root
    if candidate.startswith(visible_root + "/"):
        return host_root + candidate[len(visible_root) :]
    if candidate.startswith("/"):
        return os.path.join(host_root, candidate.lstrip("/"))
    return os.path.join(host_root, candidate)


def sandbox_visible_path(sandbox: SandboxBackendProtocol, path: str) -> str:
    """Translate a real sandbox path into the file-tool-visible path space."""
    visible_root = _sandbox_root_dir(sandbox).rstrip("/")
    host_root = _sandbox_host_root_dir(sandbox).rstrip("/")
    candidate = os.fspath(path)
    if candidate == visible_root:
        return "/"
    if candidate.startswith(visible_root + "/"):
        return f"/{candidate[len(visible_root) + 1:]}"
    if candidate == host_root:
        return "/"
    if candidate.startswith(host_root + "/"):
        return f"/{candidate[len(host_root) + 1:]}"
    root = Path(host_root).resolve()
    candidate_path = Path(candidate)
    resolved = (
        candidate_path.resolve(strict=False)
        if candidate_path.is_absolute()
        else (root / candidate_path).resolve(strict=False)
    )
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return path
    return f"/{relative.as_posix()}"


def sandbox_file_tool_path(sandbox: SandboxBackendProtocol, path: str) -> str:
    """Return a canonical absolute path that file tools can actually access."""
    candidate = os.fspath(path)
    host_root = _sandbox_host_root_dir(sandbox).rstrip("/")
    visible_root = _sandbox_root_dir(sandbox).rstrip("/")
    if candidate == visible_root:
        return visible_root
    if candidate.startswith(visible_root + "/"):
        return candidate
    if candidate == host_root:
        return visible_root
    if candidate.startswith(host_root + "/"):
        return visible_root + candidate[len(host_root) :]
    if candidate.startswith("/"):
        return f"{visible_root}/{candidate.lstrip('/')}"
    return f"{visible_root}/{candidate.lstrip('/')}"


def _host_repo_exists(sandbox: SandboxBackendProtocol, repo_dir: str) -> bool:
    host_repo_dir = sandbox_host_path(sandbox, repo_dir)
    return os.path.isdir(os.path.join(host_repo_dir, ".git"))


def _create_local_sandbox(thread_id: str, *, host_root_dir: str) -> LocalShellBackend:
    ensure_state_layout()
    del thread_id
    os.makedirs(host_root_dir, exist_ok=True)
    return LocalShellBackend(root_dir=host_root_dir, virtual_mode=True, inherit_env=False)


def _docker_container_name(thread_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", thread_id or "").strip("._-")
    normalized = normalized or "sandbox"
    return f"open-review-sandbox-{normalized[:24]}"


def _safe_worktree_name(run_id: str) -> str:
    value = (run_id or "").strip()
    if not value:
        return "run"
    return re.sub(r"[^A-Za-z0-9._:-]+", "_", value)


def _docker_cmd(args: list[str], *, text: bool = True) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        check=False,
    )


def _docker_user_spec() -> str:
    uid = os.environ.get("OPEN_REVIEW_UID") or str(os.getuid())
    gid = os.environ.get("OPEN_REVIEW_GID") or str(os.getgid())
    return f"{uid}:{gid}"


def _docker_run_user_args() -> list[str]:
    return ["--user", _docker_user_spec(), "-e", "HOME=/workspace/.home"]


def _inspect_container(container_name: str) -> dict | None:
    result = _docker_cmd(["inspect", container_name])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list) and payload:
        return payload[0]
    return None


def _remove_container(container_name: str) -> None:
    _docker_cmd(["rm", "-f", container_name])


def _ensure_docker_container(*, thread_id: str, image: str, host_root_dir: str) -> DockerSandboxBackend:
    container_name = _docker_container_name(thread_id)
    resolved_host_root = os.path.realpath(host_root_dir)
    os.makedirs(resolved_host_root, exist_ok=True)
    desired_user = _docker_user_spec()
    container = _inspect_container(container_name)
    reused_existing_container = False
    if container is not None:
        current_image = str(container.get("Config", {}).get("Image", ""))
        current_user = str(container.get("Config", {}).get("User", ""))
        current_mount = ""
        for mount in container.get("Mounts", []) or []:
            if mount.get("Destination") == "/workspace":
                current_mount = str(mount.get("Source", ""))
        if (
            current_image != image
            or current_user != desired_user
            or os.path.realpath(current_mount or "") != resolved_host_root
        ):
            logger.info(
                "Recreating sandbox container %s because image, user, or mount changed",
                container_name,
            )
            _remove_container(container_name)
            container = None
        else:
            reused_existing_container = True
            if not bool(container.get("State", {}).get("Running")):
                start = _docker_cmd(["start", container_name])
                if start.returncode != 0:
                    raise RuntimeError(start.stderr.strip() or start.stdout.strip() or container_name)

    def _create_container() -> None:
        create = _docker_cmd(
            [
                "run",
                "-d",
                "--name",
                container_name,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                *_docker_run_user_args(),
                "-v",
                f"{resolved_host_root}:/workspace",
                "-v",
                f"{resolved_host_root}:{resolved_host_root}",
                image,
                "bash",
                "-lc",
                "mkdir -p /workspace /workspace/.home && sleep infinity",
            ]
        )
        if create.returncode != 0:
            raise RuntimeError(create.stderr.strip() or create.stdout.strip() or image)

    if container is None:
        _create_container()

    def _build_backend() -> DockerSandboxBackend:
        return DockerSandboxBackend(
            container_name=container_name,
            root_dir="/workspace",
            host_root_dir=resolved_host_root,
        )

    backend = _build_backend()
    init = backend.execute("mkdir -p /workspace", timeout=30)
    if init.exit_code != 0 and reused_existing_container:
        logger.warning(
            "Recreating sandbox container %s after init exec failed against an existing container: %s",
            container_name,
            init.output.strip() or "<no output>",
        )
        _remove_container(container_name)
        _create_container()
        backend = _build_backend()
        init = backend.execute("mkdir -p /workspace", timeout=30)
    if init.exit_code != 0:
        raise RuntimeError(init.output.strip() or container_name)
    return backend


def create_sandbox(thread_id: str, *, host_root_dir: str | None = None) -> SandboxBackendProtocol:
    config = _runtime_config()
    host_root_dir = host_root_dir or os.path.join(config.local_root_dir, thread_id)
    if config.sandbox_type == "local":
        if os.environ.get("OPEN_REVIEW_RUNTIME_ROLE") in {"web", "worker"} and os.environ.get("OPEN_REVIEW_ALLOW_LOCAL_SANDBOX") != "1":
            raise RuntimeError(
                "SANDBOX_TYPE=local is disabled for containerized Open Review web/worker processes. "
                "Use SANDBOX_TYPE=docker or set OPEN_REVIEW_ALLOW_LOCAL_SANDBOX=1 for explicit development-only override."
            )
        return _create_local_sandbox(thread_id, host_root_dir=host_root_dir)
    if config.sandbox_type == "docker":
        if not config.docker_image:
            raise ValueError("DOCKER_IMAGE is required when SANDBOX_TYPE=docker")
        return _ensure_docker_container(
            thread_id=thread_id,
            image=config.docker_image,
            host_root_dir=host_root_dir,
        )
    raise ValueError(f"Unknown sandbox type: {config.sandbox_type}")


def _host_repo_dir_for_thread(thread_id: str) -> str:
    return os.path.join(_runtime_config().local_root_dir, thread_id, "repo")


def _clone_repo_from_cache(*, cache_dir: str, repo_dir: str, source_branch: str) -> None:
    parent_dir = os.path.dirname(repo_dir)
    os.makedirs(parent_dir, exist_ok=True)
    _run_host_git(["config", "--global", "--add", "safe.directory", os.path.realpath(cache_dir)])
    result = _run_host_git(["clone", "--branch", source_branch, cache_dir, repo_dir])
    if result.returncode == 0:
        return
    result = _run_host_git(["clone", cache_dir, repo_dir])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or repo_dir)
    _run_host_git_or_raise(["checkout", source_branch], cwd=repo_dir, safe_target=repo_dir)


def _refresh_repo_checkout(*, project_id: str, sandbox: SandboxBackendProtocol, repo_dir: str, source_branch: str) -> None:
    repo_host_dir = sandbox_host_path(sandbox, repo_dir)
    public_url = _git_remote_url(project_id)
    _run_host_git_or_raise(["remote", "set-url", "origin", public_url], cwd=repo_host_dir, safe_target=repo_host_dir)
    _run_host_git_or_raise(["fetch", "origin", source_branch], cwd=repo_host_dir, auth=True, safe_target=repo_host_dir)
    _run_host_git_or_raise(
        ["checkout", "-B", source_branch, f"origin/{source_branch}"],
        cwd=repo_host_dir,
        safe_target=repo_host_dir,
    )
    _run_host_git_or_raise(["clean", "-fd"], cwd=repo_host_dir, safe_target=repo_host_dir)


def _configure_repo_identity(repo_dir: str) -> None:
    _run_host_git_or_raise(["config", "user.email", "open-review-bot@open_review.ai"], cwd=repo_dir, safe_target=repo_dir)
    _run_host_git_or_raise(["config", "user.name", "Open Review Bot"], cwd=repo_dir, safe_target=repo_dir)


def ensure_repo_refs(
    *,
    project_id: str,
    repo_dir: str,
    source_branch: str,
    target_branch: str,
    sandbox: SandboxBackendProtocol | None = None,
    fetch_depth: int | None = None,
) -> None:
    repo_host_dir = sandbox_host_path(sandbox, repo_dir) if sandbox is not None else repo_dir
    public_url = _git_remote_url(project_id)
    _run_host_git_or_raise(["remote", "set-url", "origin", public_url], cwd=repo_host_dir, safe_target=repo_host_dir)
    branches = [source_branch]
    if target_branch and target_branch not in branches:
        branches.append(target_branch)
    _run_host_git_or_raise(
        ["fetch", "origin", f"--depth={int(fetch_depth or settings.AUTO_REVIEW_FETCH_DEPTH)}", *branches],
        cwd=repo_host_dir,
        auth=True,
        safe_target=repo_host_dir,
    )


def commit_all_and_get_sha(
    *,
    worktree_dir: str,
    message: str,
    sandbox: SandboxBackendProtocol | None = None,
) -> str:
    worktree_host_dir = sandbox_host_path(sandbox, worktree_dir) if sandbox is not None else worktree_dir
    _run_host_git_or_raise(["add", "-A"], cwd=worktree_host_dir, safe_target=worktree_host_dir)
    _run_host_git_or_raise(
        ["commit", "--no-verify", "-m", message],
        cwd=worktree_host_dir,
        safe_target=worktree_host_dir,
    )
    return _run_host_git_or_raise(["rev-parse", "HEAD"], cwd=worktree_host_dir, safe_target=worktree_host_dir).strip()


def push_branch_head(
    *,
    project_id: str,
    worktree_dir: str,
    source_branch: str,
    sandbox: SandboxBackendProtocol | None = None,
) -> None:
    worktree_host_dir = sandbox_host_path(sandbox, worktree_dir) if sandbox is not None else worktree_dir
    public_url = _git_remote_url(project_id)
    _run_host_git_or_raise(["remote", "set-url", "origin", public_url], cwd=worktree_host_dir, safe_target=worktree_host_dir)
    _run_host_git_or_raise(
        ["push", "origin", f"HEAD:{source_branch}"],
        cwd=worktree_host_dir,
        auth=True,
        safe_target=worktree_host_dir,
    )


async def setup_sandbox(
    thread_id: str,
    project_id: str,
    source_branch: str,
) -> tuple[SandboxBackendProtocol, str]:
    """Get or create a sandbox with the MR branch already cloned.

    Returns (sandbox, repo_dir) where repo_dir is the absolute path
    to the cloned repository inside the sandbox.
    """
    config = _runtime_config()
    host_root_dir = os.path.join(config.local_root_dir, thread_id)
    os.makedirs(host_root_dir, exist_ok=True)
    cached = SANDBOX_CACHE.get(thread_id)
    if cached is not None:
        if cached.mode != config.sandbox_type or cached.docker_image != (config.docker_image or None):
            cleanup_sandbox(thread_id)
            cached = None
    if cached is not None:
        sandbox, repo_dir = cached.sandbox, cached.repo_dir
        logger.debug("Reusing sandbox for thread %s", thread_id)
        _refresh_repo_checkout(project_id=project_id, sandbox=sandbox, repo_dir=repo_dir, source_branch=source_branch)
        return sandbox, repo_dir

    logger.info("Creating sandbox and cloning repo for thread %s", thread_id)
    sandbox = create_sandbox(thread_id, host_root_dir=host_root_dir)
    repo_dir = os.path.join(_sandbox_root_dir(sandbox), "repo")
    repo_host_dir = sandbox_host_path(sandbox, repo_dir)

    if _host_repo_exists(sandbox, repo_dir):
        logger.info("Reusing existing on-disk sandbox repo for thread %s", thread_id)
        _refresh_repo_checkout(project_id=project_id, sandbox=sandbox, repo_dir=repo_dir, source_branch=source_branch)
        SANDBOX_CACHE[thread_id] = SandboxHandle(
            mode=config.sandbox_type,
            docker_image=config.docker_image or None,
            sandbox=sandbox,
            repo_dir=repo_dir,
            host_root_dir=host_root_dir,
        )
        return sandbox, repo_dir

    project_cache_dir = _ensure_project_cache(project_id)
    logger.info("Cloning %s branch=%s via cache=%s", _git_remote_url(project_id), source_branch, project_cache_dir)
    _clone_repo_from_cache(cache_dir=project_cache_dir, repo_dir=repo_host_dir, source_branch=source_branch)
    _configure_repo_identity(repo_host_dir)
    _refresh_repo_checkout(project_id=project_id, sandbox=sandbox, repo_dir=repo_dir, source_branch=source_branch)

    SANDBOX_CACHE[thread_id] = SandboxHandle(
        mode=config.sandbox_type,
        docker_image=config.docker_image or None,
        sandbox=sandbox,
        repo_dir=repo_dir,
        host_root_dir=host_root_dir,
    )
    return sandbox, repo_dir


def cleanup_sandbox(thread_id: str) -> None:
    """Remove a sandbox when the MR is closed/merged."""
    handle = SANDBOX_CACHE.get(thread_id)
    config = _runtime_config()
    host_root_dir = handle.host_root_dir if handle is not None else os.path.join(config.local_root_dir, thread_id)
    if handle is not None:
        logger.info("Cleaning up sandbox for thread %s", thread_id)
        if handle.mode == "docker":
            _remove_container(handle.sandbox.id)
        del SANDBOX_CACHE[thread_id]
    else:
        logger.info("Cleaning up on-disk sandbox for thread %s", thread_id)
        if config.sandbox_type == "docker":
            _remove_container(_docker_container_name(thread_id))
    shutil.rmtree(host_root_dir, ignore_errors=True)


def create_temporary_worktree(
    sandbox: SandboxBackendProtocol,
    *,
    repo_dir: str,
    head_sha: str,
    run_id: str,
) -> str:
    """Create a detached temporary worktree for a single mention run."""
    repo_host_dir = sandbox_host_path(sandbox, repo_dir)
    worktrees_root = os.path.join(_sandbox_host_root_dir(sandbox), "worktrees")
    worktree_host_dir = os.path.join(worktrees_root, _safe_worktree_name(run_id))
    os.makedirs(worktrees_root, exist_ok=True)
    _run_host_git(["worktree", "remove", "--force", worktree_host_dir], cwd=repo_host_dir)
    _run_host_git(["worktree", "prune"], cwd=repo_host_dir)
    shutil.rmtree(worktree_host_dir, ignore_errors=True)
    _run_host_git_or_raise(
        ["worktree", "add", "--detach", worktree_host_dir, head_sha],
        cwd=repo_host_dir,
        safe_target=repo_host_dir,
    )
    return sandbox_shell_path(sandbox, worktree_host_dir)


def cleanup_temporary_worktree(
    sandbox: SandboxBackendProtocol,
    *,
    repo_dir: str,
    worktree_dir: str,
) -> None:
    """Remove a temporary worktree and prune worktree metadata."""
    repo_host_dir = sandbox_host_path(sandbox, repo_dir)
    worktree_host_dir = sandbox_host_path(sandbox, worktree_dir)
    _run_host_git(["worktree", "remove", "--force", worktree_host_dir], cwd=repo_host_dir)
    result = _run_host_git(["worktree", "prune"], cwd=repo_host_dir)
    if result.returncode != 0:
        logger.warning("Failed to clean up worktree %s: %s", worktree_dir, result.stderr.strip() or result.stdout[:300])
