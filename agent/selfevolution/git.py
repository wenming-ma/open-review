"""Git helpers for runtime-managed self-evolution repositories."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path


def _normalized_safe_path(path: Path | str) -> str:
    return str(Path(path).resolve())


def build_safe_git_command(
    args: list[str],
    *,
    cwd: Path | str,
    safe_paths: Iterable[Path | str] = (),
) -> list[str]:
    safe_directories: list[str] = []
    for path in (cwd, *safe_paths):
        normalized = _normalized_safe_path(path)
        if normalized not in safe_directories:
            safe_directories.append(normalized)

    command = ["git"]
    for path in safe_directories:
        command.extend(["-c", f"safe.directory={path}"])
    command.extend(args)
    return command


def run_safe_git(
    args: list[str],
    *,
    cwd: Path | str,
    safe_paths: Iterable[Path | str] = (),
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_safe_git_command(args, cwd=cwd, safe_paths=safe_paths),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def run_safe_git_stdout(
    args: list[str],
    *,
    cwd: Path | str,
    safe_paths: Iterable[Path | str] = (),
) -> str:
    return run_safe_git(args, cwd=cwd, safe_paths=safe_paths, check=True).stdout.strip()
