"""Tests for the Docker sandbox backend."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from agent.sandbox.docker_backend import DockerSandboxBackend


def test_execute_replaces_invalid_utf8_from_docker_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    docker = tmp_path / "docker"
    docker.write_bytes(
        b"#!/usr/bin/env python3\n"
        b"import sys\n"
        b"sys.stdout.buffer.write(b'before\\xbfafter')\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    backend = DockerSandboxBackend(container_name="sandbox", root_dir="/workspace")

    result = backend.execute("git diff")

    assert result.exit_code == 0
    assert result.output == "before\ufffdafter"
    assert "UnicodeDecodeError" not in result.output


def test_upload_files_streams_content_through_container_user(monkeypatch) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent.sandbox.docker_backend.subprocess.run", fake_run)

    backend = DockerSandboxBackend(container_name="sandbox", root_dir="/workspace")
    result = backend.upload_files([("/workspace/src/app.py", b"print('ok')\n")])

    assert result[0].error is None
    assert calls == [
        (
            [
                "docker",
                "exec",
                "sandbox",
                "bash",
                "-lc",
                "mkdir -p /workspace/src",
            ],
            None,
        ),
        (
            [
                "docker",
                "exec",
                "-i",
                "sandbox",
                "bash",
                "-lc",
                "cat > /workspace/src/app.py",
            ],
            b"print('ok')\n",
        ),
    ]
