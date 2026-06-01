"""Helpers for running commands on the host or inside a sandbox backend."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Any


def _normalize_output(output: str) -> str:
    normalized = output.strip()
    if normalized == "<no output>":
        return ""
    return output


def run_command(
    cmd: list[str],
    *,
    cwd: str | None = None,
    sandbox=None,
    text: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run a command locally or through a sandbox backend."""
    if sandbox is None:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
            timeout=timeout,
        )

    command = shlex.join(cmd)
    if cwd:
        command = f"cd {shlex.quote(cwd)} && {command}"
    result = sandbox.execute(command, timeout=timeout)
    output = _normalize_output(result.output)

    if text:
        stdout: str | bytes = output if result.exit_code == 0 else ""
        stderr: str | bytes = "" if result.exit_code == 0 else output
    else:
        encoded = output.encode("utf-8", errors="replace")
        stdout = encoded if result.exit_code == 0 else b""
        stderr = b"" if result.exit_code == 0 else encoded

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=result.exit_code,
        stdout=stdout,
        stderr=stderr,
    )


def which(binary: str, *, cwd: str | None = None, sandbox=None) -> str | None:
    """Resolve a binary path locally or inside a sandbox backend."""
    if sandbox is None:
        return shutil.which(binary)

    command = f"command -v {shlex.quote(binary)}"
    if cwd:
        command = f"cd {shlex.quote(cwd)} && {command}"
    result = sandbox.execute(command)
    if result.exit_code != 0 or not result.output.strip() or result.output.strip() == "<no output>":
        return None
    output = result.output.strip().splitlines()[-1]
    return output.replace("[stderr] ", "").strip()
