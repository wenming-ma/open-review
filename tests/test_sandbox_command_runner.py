"""Tests for host/sandbox command runner output handling."""

from __future__ import annotations

import sys

from agent.sandbox.command_runner import run_command


def test_run_command_text_mode_replaces_invalid_utf8_output() -> None:
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'before\\xbfafter')",
        ]
    )

    assert result.returncode == 0
    assert result.stdout == "before\ufffdafter"
    assert result.stderr == ""
