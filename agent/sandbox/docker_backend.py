"""Docker-backed sandbox backend for MR execution environments."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox


class DockerSandboxBackend(BaseSandbox):
    """Run shell commands and file operations inside a Docker container."""

    def __init__(
        self,
        *,
        container_name: str,
        root_dir: str = "/workspace",
        host_root_dir: str | None = None,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        self.container_name = container_name
        self.root_dir = root_dir
        self.cwd = root_dir
        self.host_root_dir = host_root_dir
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes

    @property
    def id(self) -> str:
        return self.container_name

    def _docker(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
            timeout=timeout,
        )

    def _docker_with_input(
        self,
        args: list[str],
        *,
        input_bytes: bytes,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["docker", *args],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        try:
            result = self._docker(
                [
                    "exec",
                    "-w",
                    self.root_dir,
                    self.container_name,
                    "bash",
                    "-lc",
                    command,
                ],
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
                truncated=False,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
                truncated=False,
            )

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.extend(
                f"[stderr] {line}" for line in result.stderr.strip().splitlines() if line
            )
        output = "\n".join(output_parts) if output_parts else "<no output>"
        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=result.returncode,
            truncated=truncated,
        )

    def _path_kind(self, path: str) -> str:
        if not path.startswith("/"):
            return "invalid"
        check = self._docker(
            [
                "exec",
                "-e",
                f"TARGET={path}",
                self.container_name,
                "bash",
                "-lc",
                "if [ -d \"$TARGET\" ]; then echo directory; "
                "elif [ -f \"$TARGET\" ]; then echo file; "
                "else echo missing; fi",
            ],
            timeout=30,
        )
        if check.returncode != 0:
            return "missing"
        return check.stdout.strip() or "missing"

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            kind = self._path_kind(path)
            if kind == "invalid":
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path")
                )
                continue
            if kind == "directory":
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="is_directory")
                )
                continue
            if kind != "file":
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="file_not_found")
                )
                continue

            tmp_dir = tempfile.mkdtemp(prefix="open-review-docker-download-")
            local_path = os.path.join(tmp_dir, Path(path).name or "download.bin")
            try:
                result = self._docker(
                    ["cp", f"{self.container_name}:{path}", local_path],
                    text=False,
                    timeout=30,
                )
                if result.returncode != 0:
                    responses.append(
                        FileDownloadResponse(
                            path=path,
                            content=None,
                            error="file_not_found",
                        )
                    )
                    continue
                with open(local_path, "rb") as handle:
                    responses.append(
                        FileDownloadResponse(path=path, content=handle.read(), error=None)
                    )
            finally:
                try:
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    os.rmdir(tmp_dir)
                except OSError:
                    pass
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            mkdir_result = self._docker(
                [
                    "exec",
                    self.container_name,
                    "bash",
                    "-lc",
                    f"mkdir -p {shlex.quote(Path(path).parent.as_posix())}",
                ],
                timeout=30,
            )
            if mkdir_result.returncode != 0:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            copy_result = self._docker_with_input(
                [
                    "exec",
                    "-i",
                    self.container_name,
                    "bash",
                    "-lc",
                    f"cat > {shlex.quote(path)}",
                ],
                input_bytes=content,
                timeout=30,
            )
            if copy_result.returncode != 0:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            responses.append(FileUploadResponse(path=path, error=None))
        return responses
