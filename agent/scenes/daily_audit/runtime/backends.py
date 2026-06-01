"""Backend wrappers for daily audit agents."""

from __future__ import annotations

import re

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)


class FileToolBackend(BackendProtocol):
    """Expose filesystem tools with optional writes and shell passthrough."""

    def __init__(self, backend: BackendProtocol, *, allow_writes: bool) -> None:
        self._backend = backend
        self._allow_writes = allow_writes
        self.cwd = getattr(backend, "cwd", None)

    def ls(self, path: str) -> LsResult:
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend.read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self._backend.grep(pattern, path, glob)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        return self._backend.glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._allow_writes:
            del content
            return WriteResult(error="read_only_backend", path=file_path)
        return self._backend.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if not self._allow_writes:
            del old_string, new_string, replace_all
            return EditResult(error="read_only_backend", path=file_path, occurrences=0)
        return self._backend.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if not self._allow_writes:
            return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]
        return self._backend.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend.download_files(paths)


_BLOCKED_GIT_COMMANDS = (
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+push\b", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+commit\b", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+(?:merge|rebase|cherry-pick|am|reset|checkout|switch|stash)\b", re.IGNORECASE),
)


class DailyAuditBackend(FileToolBackend):
    """Allow shell access while blocking branch-state-changing git commands."""

    def _blocked_command(self, command: str) -> str | None:
        normalized = command.strip()
        for pattern in _BLOCKED_GIT_COMMANDS:
            match = pattern.search(normalized)
            if match:
                return match.group(0).strip()
        return None

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        blocked = self._blocked_command(command)
        if blocked:
            return ExecuteResponse(
                output=(
                    "Error: branch-state-changing git commands are blocked in the Daily Audit "
                    f"backend: `{blocked}`. Use the orchestrator path for commit/push/MR."
                ),
                exit_code=126,
                truncated=False,
            )
        return self._backend.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)
