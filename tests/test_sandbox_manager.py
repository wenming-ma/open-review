"""Tests for sandbox worktree helpers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from agent.sandbox import manager


def _reset_runtime(monkeypatch) -> None:
    monkeypatch.setattr(manager, "SANDBOX_CACHE", {})
    manager.reset_runtime_sandbox_config()


class _FakeSandbox:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir
        self.commands: list[str] = []

    def execute(self, command: str):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, output="ok")


class _FakeSandboxWithCwd:
    def __init__(self, root_dir: str) -> None:
        self.cwd = Path(root_dir)
        self.commands: list[str] = []

    def execute(self, command: str):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, output="ok")


class _FakeDockerSandbox:
    def __init__(self, *, host_root_dir: str) -> None:
        self.root_dir = "/workspace"
        self.cwd = "/workspace"
        self.host_root_dir = host_root_dir
        self.commands: list[str] = []

    def execute(self, command: str):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, output="ok")


@dataclass
class _GitCall:
    args: list[str]
    cwd: str | None = None
    auth: bool = False


def test_create_temporary_worktree_adds_detached_worktree(tmp_path, monkeypatch):
    sandbox = _FakeSandbox(str(tmp_path / "thread-1"))
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)

    worktree_dir = manager.create_temporary_worktree(
        sandbox,
        repo_dir=str(tmp_path / "thread-1" / "repo"),
        head_sha="abc123",
        run_id="run-123",
    )

    assert worktree_dir == str(tmp_path / "thread-1" / "worktrees" / "run-123")
    assert calls == [
        _GitCall(
            [
                "worktree",
                "remove",
                "--force",
                str(tmp_path / "thread-1" / "worktrees" / "run-123"),
            ],
            cwd=str(tmp_path / "thread-1" / "repo"),
        ),
        _GitCall(["worktree", "prune"], cwd=str(tmp_path / "thread-1" / "repo")),
        _GitCall(
            [
                "worktree",
                "add",
                "--detach",
                str(tmp_path / "thread-1" / "worktrees" / "run-123"),
                "abc123",
            ],
            cwd=str(tmp_path / "thread-1" / "repo"),
        ),
    ]


def test_run_host_git_replaces_invalid_utf8_output(tmp_path, monkeypatch):
    git = tmp_path / "git"
    git.write_bytes(
        b"#!/usr/bin/env python3\n"
        b"import sys\n"
        b"sys.stdout.buffer.write(b'before\\xbfafter')\n"
    )
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    result = manager._run_host_git(["diff"], cwd=str(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "before\ufffdafter"
    assert result.stderr == ""


def test_create_temporary_worktree_sanitizes_path_separators_in_run_id(tmp_path, monkeypatch):
    sandbox = _FakeSandbox(str(tmp_path / "thread-1"))
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)

    worktree_dir = manager.create_temporary_worktree(
        sandbox,
        repo_dir=str(tmp_path / "thread-1" / "repo"),
        head_sha="abc123",
        run_id="review-mr:team/service:4:open:deadbeef",
    )

    expected_dir = tmp_path / "thread-1" / "worktrees" / "review-mr:team_service:4:open:deadbeef"
    assert worktree_dir == str(expected_dir)
    assert calls[-1] == _GitCall(
        [
            "worktree",
            "add",
            "--detach",
            str(expected_dir),
            "abc123",
        ],
        cwd=str(tmp_path / "thread-1" / "repo"),
    )


def test_cleanup_temporary_worktree_removes_worktree(tmp_path, monkeypatch):
    sandbox = _FakeSandbox(str(tmp_path / "thread-1"))
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)

    manager.cleanup_temporary_worktree(
        sandbox,
        repo_dir=str(tmp_path / "thread-1" / "repo"),
        worktree_dir=str(tmp_path / "thread-1" / "worktrees" / "run-123"),
    )

    assert calls == [
        _GitCall(
            [
                "worktree",
                "remove",
                "--force",
                str(tmp_path / "thread-1" / "worktrees" / "run-123"),
            ],
            cwd=str(tmp_path / "thread-1" / "repo"),
        ),
        _GitCall(["worktree", "prune"], cwd=str(tmp_path / "thread-1" / "repo")),
    ]


def test_project_cache_dir_is_sanitized():
    cache_dir = manager._project_cache_dir("group/subgroup/project.name")

    assert cache_dir.endswith("group__subgroup__project.name.git")


def test_git_auth_env_disables_interactive_prompts(monkeypatch):
    monkeypatch.setattr(manager.settings, "GITLAB_TOKEN", "secret-token")

    env = manager._git_auth_env()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/bin/false"
    assert env["SSH_ASKPASS"] == "/bin/false"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: Basic ")


def test_run_host_git_marks_cwd_as_safe_directory(monkeypatch):
    recorded = {}

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    manager._run_host_git(["status", "--short"], cwd="/var/lib/open-review/sandboxes/thread-1/repo")

    assert recorded["command"] == [
        "git",
        "-c",
        "safe.directory=/var/lib/open-review/sandboxes/thread-1/repo",
        "status",
        "--short",
    ]
    assert recorded["kwargs"]["cwd"] == "/var/lib/open-review/sandboxes/thread-1/repo"


def test_run_host_git_marks_local_clone_source_as_safe_directory(monkeypatch):
    recorded = {}

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    manager._run_host_git(
        [
            "clone",
            "--branch",
            "feature",
            "/var/lib/open-review/project-cache/group__project.git",
            "/var/lib/open-review/sandboxes/thread-1/repo",
        ],
    )

    assert recorded["command"] == [
        "git",
        "-c",
        "safe.directory=/var/lib/open-review/project-cache/group__project.git",
        "clone",
        "--branch",
        "feature",
        "/var/lib/open-review/project-cache/group__project.git",
        "/var/lib/open-review/sandboxes/thread-1/repo",
    ]


def test_ensure_repo_refs_fetches_source_and_target_via_host_git(tmp_path, monkeypatch):
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)
    monkeypatch.setattr(manager, "_git_remote_url", lambda _project_id: "https://gitlab-api.example.com/group/project.git")
    monkeypatch.setattr(manager.settings, "AUTO_REVIEW_FETCH_DEPTH", 77)

    manager.ensure_repo_refs(
        project_id="group/project",
        repo_dir=str(tmp_path / "repo"),
        source_branch="feature/router",
        target_branch="main",
    )

    assert calls == [
        _GitCall(
            ["remote", "set-url", "origin", "https://gitlab-api.example.com/group/project.git"],
            cwd=str(tmp_path / "repo"),
            auth=False,
        ),
        _GitCall(
            ["fetch", "origin", "--depth=77", "feature/router", "main"],
            cwd=str(tmp_path / "repo"),
            auth=True,
        ),
    ]


def test_commit_all_and_get_sha_commits_on_host(tmp_path, monkeypatch):
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)

    commit_sha = manager.commit_all_and_get_sha(
        worktree_dir=str(tmp_path / "worktrees" / "run-123"),
        message="fix: address mention request",
    )

    assert commit_sha == "deadbeef"
    assert calls == [
        _GitCall(["add", "-A"], cwd=str(tmp_path / "worktrees" / "run-123"), auth=False),
        _GitCall(
            ["commit", "--no-verify", "-m", "fix: address mention request"],
            cwd=str(tmp_path / "worktrees" / "run-123"),
            auth=False,
        ),
        _GitCall(["rev-parse", "HEAD"], cwd=str(tmp_path / "worktrees" / "run-123"), auth=False),
    ]


def test_push_branch_head_uses_host_git_auth(tmp_path, monkeypatch):
    calls: list[_GitCall] = []

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)
    monkeypatch.setattr(manager, "_git_remote_url", lambda _project_id: "https://gitlab-api.example.com/group/project.git")

    manager.push_branch_head(
        project_id="group/project",
        worktree_dir=str(tmp_path / "worktrees" / "run-123"),
        source_branch="feature/router",
    )

    assert calls == [
        _GitCall(
            ["remote", "set-url", "origin", "https://gitlab-api.example.com/group/project.git"],
            cwd=str(tmp_path / "worktrees" / "run-123"),
            auth=False,
        ),
        _GitCall(
            ["push", "origin", "HEAD:feature/router"],
            cwd=str(tmp_path / "worktrees" / "run-123"),
            auth=True,
        ),
    ]


def test_clone_repo_from_cache_marks_bare_cache_as_global_safe_directory(tmp_path, monkeypatch):
    calls: list[_GitCall] = []
    cache_dir = str(tmp_path / "project-cache" / "group__project.git")
    repo_dir = str(tmp_path / "sandboxes" / "thread-1" / "repo")

    def fake_run_host_git(args, *, cwd=None, auth=False):
        calls.append(_GitCall(list(args), cwd=cwd, auth=auth))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_host_git", fake_run_host_git)

    manager._clone_repo_from_cache(cache_dir=cache_dir, repo_dir=repo_dir, source_branch="feature/router")

    assert calls == [
        _GitCall(["config", "--global", "--add", "safe.directory", cache_dir]),
        _GitCall(["clone", "--branch", "feature/router", cache_dir, repo_dir]),
    ]


def test_setup_sandbox_clones_from_project_cache(tmp_path, monkeypatch):
    sandbox = _FakeSandbox(str(tmp_path / "sandboxes" / "thread-1"))
    thread_id = "thread-1"
    called = {}

    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "DOCKER_IMAGE": "",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )
    monkeypatch.setattr(manager, "create_sandbox", lambda _thread_id, *, host_root_dir=None: sandbox)
    monkeypatch.setattr(manager, "_host_repo_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        manager,
        "_ensure_project_cache",
        lambda _project_id: str(tmp_path / "project-cache" / "group__project.git"),
    )
    monkeypatch.setattr(
        manager,
        "_clone_repo_from_cache",
        lambda *, cache_dir, repo_dir, source_branch: called.setdefault(
            "clone",
            {
                "cache_dir": cache_dir,
                "repo_dir": repo_dir,
                "source_branch": source_branch,
            },
        ),
    )
    monkeypatch.setattr(
        manager,
        "_configure_repo_identity",
        lambda repo_dir: called.setdefault("identity", repo_dir),
    )
    monkeypatch.setattr(
        manager,
        "_refresh_repo_checkout",
        lambda *, project_id, sandbox, repo_dir, source_branch: called.setdefault(
            "refresh",
            {
                "project_id": project_id,
                "repo_dir": repo_dir,
                "source_branch": source_branch,
                "sandbox": sandbox,
            },
        ),
    )

    async def _run():
        return await manager.setup_sandbox(thread_id, "group/project", "feature/router")

    _sandbox, repo_dir = asyncio.run(_run())

    assert repo_dir == str(tmp_path / "sandboxes" / "thread-1" / "repo")
    assert called["clone"] == {
        "cache_dir": str(tmp_path / "project-cache" / "group__project.git"),
        "repo_dir": str(tmp_path / "sandboxes" / "thread-1" / "repo"),
        "source_branch": "feature/router",
    }
    assert called["identity"] == str(tmp_path / "sandboxes" / "thread-1" / "repo")
    assert called["refresh"] == {
        "project_id": "group/project",
        "repo_dir": str(tmp_path / "sandboxes" / "thread-1" / "repo"),
        "source_branch": "feature/router",
        "sandbox": sandbox,
    }
    assert sandbox.commands == []


def test_setup_sandbox_supports_backends_with_cwd_only(tmp_path, monkeypatch):
    sandbox = _FakeSandboxWithCwd(str(tmp_path / "sandboxes" / "thread-cwd"))
    thread_id = "thread-cwd"
    called = {}

    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "DOCKER_IMAGE": "",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )
    monkeypatch.setattr(manager, "create_sandbox", lambda _thread_id, *, host_root_dir=None: sandbox)
    monkeypatch.setattr(manager, "_host_repo_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        manager,
        "_ensure_project_cache",
        lambda _project_id: str(tmp_path / "project-cache" / "group__project.git"),
    )
    monkeypatch.setattr(
        manager,
        "_clone_repo_from_cache",
        lambda *, cache_dir, repo_dir, source_branch: called.setdefault("repo_dir", repo_dir),
    )
    monkeypatch.setattr(manager, "_configure_repo_identity", lambda _repo_dir: None)
    monkeypatch.setattr(manager, "_refresh_repo_checkout", lambda **_kwargs: None)

    async def _run():
        return await manager.setup_sandbox(thread_id, "group/project", "feature/router")

    _sandbox, repo_dir = asyncio.run(_run())

    assert repo_dir == str(tmp_path / "sandboxes" / "thread-cwd" / "repo")
    assert called["repo_dir"] == str(tmp_path / "sandboxes" / "thread-cwd" / "repo")


def test_sandbox_visible_path_translates_real_paths_to_backend_visible_paths():
    sandbox = _FakeSandbox("/tmp/open-review-sandboxes/thread-1")

    assert (
        manager.sandbox_visible_path(sandbox, "/tmp/open-review-sandboxes/thread-1/worktrees/run-123/src/router.cpp")
        == "/worktrees/run-123/src/router.cpp"
    )
    assert manager.sandbox_visible_path(sandbox, "/outside/root/file.cpp") == "/outside/root/file.cpp"


def test_sandbox_file_tool_path_uses_canonical_backend_root():
    sandbox = _FakeDockerSandbox(host_root_dir="/tmp/open-review-sandboxes/thread-1")

    assert (
        manager.sandbox_file_tool_path(
            sandbox,
            "/tmp/open-review-sandboxes/thread-1/worktrees/review-mr:team_service:5/src/router.cpp",
        )
        == "/workspace/worktrees/review-mr:team_service:5/src/router.cpp"
    )
    assert (
        manager.sandbox_file_tool_path(
            sandbox,
            "/worktrees/review-mr:team_service:5/src/router.cpp",
        )
        == "/workspace/worktrees/review-mr:team_service:5/src/router.cpp"
    )


def test_create_sandbox_uses_docker_backend_when_runtime_snapshot_requests_it(monkeypatch):
    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "docker",
            "DOCKER_IMAGE": "open-review/sandbox:test",
            "LOCAL_SANDBOX_ROOT_DIR": "/tmp/open-review-sandboxes",
            "PROJECT_CACHE_ROOT": "/tmp/open-review-project-cache",
        }
    )
    called = {}

    def fake_ensure_docker_container(*, thread_id: str, image: str, host_root_dir: str):
        called["thread_id"] = thread_id
        called["image"] = image
        called["host_root_dir"] = host_root_dir
        return _FakeSandboxWithCwd("/tmp/open-review-sandboxes/thread-cwd")

    monkeypatch.setattr(manager, "_ensure_docker_container", fake_ensure_docker_container)

    sandbox = manager.create_sandbox("thread-1")

    assert sandbox.cwd == Path("/tmp/open-review-sandboxes/thread-cwd")
    assert called == {
        "thread_id": "thread-1",
        "image": "open-review/sandbox:test",
        "host_root_dir": "/tmp/open-review-sandboxes/thread-1",
    }


def test_create_sandbox_rejects_local_backend_for_containerized_runtime(monkeypatch, tmp_path):
    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
        }
    )
    monkeypatch.setenv("OPEN_REVIEW_RUNTIME_ROLE", "worker")

    try:
        manager.create_sandbox("thread-local")
    except RuntimeError as exc:
        assert "SANDBOX_TYPE=local is disabled" in str(exc)
    else:
        raise AssertionError("containerized local sandbox should fail closed")


def test_create_sandbox_allows_local_backend_with_explicit_development_override(monkeypatch, tmp_path):
    _reset_runtime(monkeypatch)
    root_dir = tmp_path / "sandboxes"
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "LOCAL_SANDBOX_ROOT_DIR": str(root_dir),
        }
    )
    monkeypatch.setenv("OPEN_REVIEW_RUNTIME_ROLE", "worker")
    monkeypatch.setenv("OPEN_REVIEW_ALLOW_LOCAL_SANDBOX", "1")

    handle = manager.create_sandbox("thread-local")

    assert handle.cwd == root_dir / "thread-local"


def test_ensure_docker_container_mounts_only_sandbox_paths_not_state_root(tmp_path, monkeypatch):
    host_root_dir = str(tmp_path / "sandboxes" / "thread-1")
    state_root_dir = str(tmp_path / "state-root")
    recorded: dict[str, object] = {}
    monkeypatch.setenv("OPEN_REVIEW_UID", "1234")
    monkeypatch.setenv("OPEN_REVIEW_GID", "1235")

    class _FakeBackend:
        def __init__(self, *, container_name: str, root_dir: str, host_root_dir: str) -> None:
            recorded["backend"] = {
                "container_name": container_name,
                "root_dir": root_dir,
                "host_root_dir": host_root_dir,
            }

        def execute(self, command: str, timeout: int | None = None):
            recorded["init_command"] = {"command": command, "timeout": timeout}
            return SimpleNamespace(exit_code=0, output="ok")

    def fake_docker_cmd(args, *, text=True):
        recorded.setdefault("docker_calls", []).append(list(args))
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(manager, "_inspect_container", lambda _name: None)
    monkeypatch.setattr(manager, "_docker_cmd", fake_docker_cmd)
    monkeypatch.setattr(manager, "DockerSandboxBackend", _FakeBackend)
    monkeypatch.setattr(manager.settings, "OPEN_REVIEW_RUNTIME_ROOT", str(Path(state_root_dir) / "runtime"))

    backend = manager._ensure_docker_container(
        thread_id="thread-1",
        image="open-review/sandbox:test",
        host_root_dir=host_root_dir,
    )

    assert isinstance(backend, _FakeBackend)
    docker_run = recorded["docker_calls"][0]
    assert docker_run == [
        "run",
        "-d",
        "--name",
        "open-review-sandbox-thread-1",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1234:1235",
        "-e",
        "HOME=/workspace/.home",
        "-v",
        f"{host_root_dir}:/workspace",
        "-v",
        f"{host_root_dir}:{host_root_dir}",
        "open-review/sandbox:test",
        "bash",
        "-lc",
        "mkdir -p /workspace /workspace/.home && sleep infinity",
    ]
    assert f"{Path(state_root_dir)}:{Path(state_root_dir)}" not in docker_run


def test_ensure_docker_container_sanitizes_container_name(tmp_path, monkeypatch):
    host_root_dir = str(tmp_path / "sandboxes" / "team" / "service!daily_audit")
    state_root_dir = str(tmp_path / "state-root")
    recorded: dict[str, object] = {}
    monkeypatch.setenv("OPEN_REVIEW_UID", "1234")
    monkeypatch.setenv("OPEN_REVIEW_GID", "1235")

    class _FakeBackend:
        def __init__(self, *, container_name: str, root_dir: str, host_root_dir: str) -> None:
            recorded["backend"] = {
                "container_name": container_name,
                "root_dir": root_dir,
                "host_root_dir": host_root_dir,
            }

        def execute(self, command: str, timeout: int | None = None):
            return SimpleNamespace(exit_code=0, output="ok")

    def fake_docker_cmd(args, *, text=True):
        recorded.setdefault("docker_calls", []).append(list(args))
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(manager, "_inspect_container", lambda _name: None)
    monkeypatch.setattr(manager, "_docker_cmd", fake_docker_cmd)
    monkeypatch.setattr(manager, "DockerSandboxBackend", _FakeBackend)
    monkeypatch.setattr(manager.settings, "OPEN_REVIEW_RUNTIME_ROOT", str(Path(state_root_dir) / "runtime"))

    backend = manager._ensure_docker_container(
        thread_id="team/service!daily_audit",
        image="open-review/sandbox:test",
        host_root_dir=host_root_dir,
    )

    assert isinstance(backend, _FakeBackend)
    assert recorded["backend"]["container_name"] == "open-review-sandbox-team_service_daily_audit"
    assert recorded["docker_calls"][0][3] == "open-review-sandbox-team_service_daily_audit"


def test_ensure_docker_container_recreates_existing_container_when_exec_healthcheck_fails(
    tmp_path, monkeypatch
):
    host_root_dir = str(tmp_path / "sandboxes" / "thread-1")
    state_root_dir = str(tmp_path / "state-root")
    recorded: dict[str, object] = {}
    monkeypatch.setenv("OPEN_REVIEW_UID", "1234")
    monkeypatch.setenv("OPEN_REVIEW_GID", "1235")

    class _FakeBackend:
        execute_calls = 0

        def __init__(self, *, container_name: str, root_dir: str, host_root_dir: str) -> None:
            recorded.setdefault("backends", []).append(
                {
                    "container_name": container_name,
                    "root_dir": root_dir,
                    "host_root_dir": host_root_dir,
                }
            )

        def execute(self, command: str, timeout: int | None = None):
            type(self).execute_calls += 1
            recorded.setdefault("init_commands", []).append({"command": command, "timeout": timeout})
            if type(self).execute_calls == 1:
                return SimpleNamespace(
                    exit_code=128,
                    output=(
                        "OCI runtime exec failed: exec failed: unable to start container process: "
                        "current working directory is outside of container mount namespace root -- "
                        "possible container breakout detected"
                    ),
                )
            return SimpleNamespace(exit_code=0, output="ok")

    existing_container = {
        "Config": {"Image": "open-review/sandbox:test", "User": "1234:1235"},
        "Mounts": [
            {"Destination": "/workspace", "Source": host_root_dir},
            {"Destination": state_root_dir, "Source": state_root_dir},
        ],
        "State": {"Running": True},
    }

    def fake_docker_cmd(args, *, text=True):
        recorded.setdefault("docker_calls", []).append(list(args))
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(manager, "_inspect_container", lambda _name: existing_container)
    monkeypatch.setattr(manager, "_docker_cmd", fake_docker_cmd)
    monkeypatch.setattr(manager, "DockerSandboxBackend", _FakeBackend)
    monkeypatch.setattr(manager.settings, "OPEN_REVIEW_RUNTIME_ROOT", str(Path(state_root_dir) / "runtime"))

    backend = manager._ensure_docker_container(
        thread_id="thread-1",
        image="open-review/sandbox:test",
        host_root_dir=host_root_dir,
    )

    assert isinstance(backend, _FakeBackend)
    assert recorded["docker_calls"] == [
        ["rm", "-f", "open-review-sandbox-thread-1"],
        [
            "run",
            "-d",
            "--name",
            "open-review-sandbox-thread-1",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "1234:1235",
            "-e",
            "HOME=/workspace/.home",
            "-v",
            f"{host_root_dir}:/workspace",
            "-v",
            f"{host_root_dir}:{host_root_dir}",
            "open-review/sandbox:test",
            "bash",
            "-lc",
            "mkdir -p /workspace /workspace/.home && sleep infinity",
        ],
    ]
    assert _FakeBackend.execute_calls == 2


def test_setup_sandbox_uses_host_repo_paths_in_docker_mode(tmp_path, monkeypatch):
    sandbox = _FakeDockerSandbox(host_root_dir=str(tmp_path / "sandboxes" / "thread-docker"))
    called = {}
    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "docker",
            "DOCKER_IMAGE": "open-review/sandbox:test",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )
    monkeypatch.setattr(manager, "create_sandbox", lambda _thread_id, *, host_root_dir=None: sandbox)
    monkeypatch.setattr(manager, "_host_repo_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        manager,
        "_ensure_project_cache",
        lambda _project_id: str(tmp_path / "project-cache" / "group__project.git"),
    )
    monkeypatch.setattr(
        manager,
        "_clone_repo_from_cache",
        lambda *, cache_dir, repo_dir, source_branch: called.setdefault(
            "clone",
            {
                "cache_dir": cache_dir,
                "repo_dir": repo_dir,
                "source_branch": source_branch,
            },
        ),
    )
    monkeypatch.setattr(manager, "_configure_repo_identity", lambda _repo_dir: None)
    monkeypatch.setattr(manager, "_refresh_repo_checkout", lambda **_kwargs: None)

    async def _run():
        return await manager.setup_sandbox("thread-docker", "group/project", "feature/router")

    _sandbox, repo_dir = asyncio.run(_run())

    assert repo_dir == "/workspace/repo"
    assert called["clone"] == {
        "cache_dir": str(tmp_path / "project-cache" / "group__project.git"),
        "repo_dir": str(tmp_path / "sandboxes" / "thread-docker" / "repo"),
        "source_branch": "feature/router",
    }
    assert manager.sandbox_host_path(sandbox, repo_dir) == str(tmp_path / "sandboxes" / "thread-docker" / "repo")


def test_setup_sandbox_reuses_existing_repo_after_process_restart(tmp_path, monkeypatch):
    sandbox = _FakeSandbox(str(tmp_path / "sandboxes" / "thread-reuse"))
    thread_id = "thread-reuse"
    called = {}

    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "DOCKER_IMAGE": "",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )
    monkeypatch.setattr(manager, "create_sandbox", lambda _thread_id, *, host_root_dir=None: sandbox)
    monkeypatch.setattr(manager, "_host_repo_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        manager,
        "_refresh_repo_checkout",
        lambda *, project_id, sandbox, repo_dir, source_branch: called.setdefault(
            "refresh",
            {
                "project_id": project_id,
                "sandbox": sandbox,
                "repo_dir": repo_dir,
                "source_branch": source_branch,
            },
        ),
    )

    async def _run():
        return await manager.setup_sandbox(thread_id, "group/project", "feature/router")

    _sandbox, repo_dir = asyncio.run(_run())

    assert repo_dir == str(tmp_path / "sandboxes" / "thread-reuse" / "repo")
    assert called["refresh"] == {
        "project_id": "group/project",
        "sandbox": sandbox,
        "repo_dir": str(tmp_path / "sandboxes" / "thread-reuse" / "repo"),
        "source_branch": "feature/router",
    }


def test_cleanup_sandbox_removes_docker_container(monkeypatch):
    _reset_runtime(monkeypatch)
    manager.SANDBOX_CACHE["thread-1"] = manager.SandboxHandle(
        mode="docker",
        docker_image="open-review/sandbox:test",
        sandbox=SimpleNamespace(id="open-review-sandbox-thread-1"),
        repo_dir="/workspace/repo",
        host_root_dir="/tmp/open-review-sandboxes/thread-1",
    )
    called = {}
    monkeypatch.setattr(manager, "_remove_container", lambda name: called.setdefault("name", name))

    manager.cleanup_sandbox("thread-1")

    assert called["name"] == "open-review-sandbox-thread-1"
    assert "thread-1" not in manager.SANDBOX_CACHE


def test_cleanup_sandbox_removes_uncached_docker_container(monkeypatch, tmp_path):
    _reset_runtime(monkeypatch)
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "docker",
            "DOCKER_IMAGE": "open-review/sandbox:test",
            "LOCAL_SANDBOX_ROOT_DIR": str(tmp_path / "sandboxes"),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )
    called = {}
    monkeypatch.setattr(manager, "_remove_container", lambda name: called.setdefault("name", name))

    manager.cleanup_sandbox("thread-1")

    assert called["name"] == "open-review-sandbox-thread-1"


def test_cleanup_sandbox_removes_on_disk_sandbox_without_cache(tmp_path, monkeypatch):
    _reset_runtime(monkeypatch)
    sandbox_root = tmp_path / "sandboxes"
    thread_root = sandbox_root / "thread-1"
    repo_dir = thread_root / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "tracked.txt").write_text("leftover checkout\n")
    manager.configure_runtime_sandbox_config(
        {
            "SANDBOX_TYPE": "local",
            "DOCKER_IMAGE": "",
            "LOCAL_SANDBOX_ROOT_DIR": str(sandbox_root),
            "PROJECT_CACHE_ROOT": str(tmp_path / "project-cache"),
        }
    )

    manager.cleanup_sandbox("thread-1")

    assert not thread_root.exists()


def test_runtime_sandbox_snapshot_stays_fixed_after_configuration_change(monkeypatch):
    _reset_runtime(monkeypatch)
    monkeypatch.setattr("agent.sandbox.manager.settings.SANDBOX_TYPE", "local")
    monkeypatch.setattr("agent.sandbox.manager.settings.DOCKER_IMAGE", "image-a")
    manager.configure_runtime_sandbox_config()

    monkeypatch.setattr("agent.sandbox.manager.settings.SANDBOX_TYPE", "docker")
    monkeypatch.setattr("agent.sandbox.manager.settings.DOCKER_IMAGE", "image-b")

    config = manager._runtime_config()

    assert config.sandbox_type == "local"
    assert config.docker_image == "image-a"


def test_sandbox_shell_path_translates_host_paths_for_docker_backend(tmp_path):
    sandbox = _FakeDockerSandbox(host_root_dir=str(tmp_path / "sandboxes" / "thread-1"))

    assert (
        manager.sandbox_shell_path(
            sandbox,
            str(tmp_path / "sandboxes" / "thread-1" / "worktrees" / "run-123"),
        )
        == "/workspace/worktrees/run-123"
    )
