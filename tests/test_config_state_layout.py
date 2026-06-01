from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import StateLayoutError, ensure_writable_directory, state_directory_fix_commands


def test_ensure_writable_directory_creates_and_checks_path(tmp_path: Path) -> None:
    target = tmp_path / "state" / "runtime"

    ensure_writable_directory(target)

    assert target.is_dir()
    assert not list(target.glob(".open-review-write-test.*"))


def test_ensure_writable_directory_reports_actionable_permission_fix(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state"
    target.mkdir()

    def fake_open(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("agent.config.os.open", fake_open)

    with pytest.raises(StateLayoutError) as exc_info:
        ensure_writable_directory(target)

    message = str(exc_info.value)
    assert "Open Review cannot write its state directory" in message
    assert "sudo install -d" in message
    assert "sudo chown -R" in message


def test_state_directory_fix_commands_target_state_root_for_children() -> None:
    commands = state_directory_fix_commands("/var/lib/open-review/runtime")

    assert commands[0].endswith("/var/lib/open-review")
    assert commands[1].endswith("/var/lib/open-review")
