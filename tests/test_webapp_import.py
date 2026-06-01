"""Smoke tests for fresh webapp imports."""

from __future__ import annotations

import subprocess
import sys


def test_webapp_can_be_imported_in_fresh_python_process():
    result = subprocess.run(
        [sys.executable, "-c", "import agent.webapp"],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
