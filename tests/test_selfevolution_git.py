from __future__ import annotations

from agent.selfevolution.git import build_safe_git_command


def test_build_safe_git_command_marks_repo_and_worktree_as_safe(tmp_path):
    repo_root = tmp_path / "service-repo" / "open-review"
    worktree_root = repo_root / ".worktrees" / "evolution" / "run-1"
    repo_root.mkdir(parents=True)
    worktree_root.mkdir(parents=True)

    command = build_safe_git_command(
        ["worktree", "add", str(worktree_root), "main"],
        cwd=repo_root,
        safe_paths=[worktree_root],
    )

    assert command[:5] == [
        "git",
        "-c",
        f"safe.directory={repo_root.resolve()}",
        "-c",
        f"safe.directory={worktree_root.resolve()}",
    ]
    assert command[5:] == ["worktree", "add", str(worktree_root), "main"]


def test_build_safe_git_command_deduplicates_safe_paths(tmp_path):
    repo_root = tmp_path / "service-repo" / "open-review"
    repo_root.mkdir(parents=True)

    command = build_safe_git_command(["status"], cwd=repo_root, safe_paths=[repo_root])

    assert command.count(f"safe.directory={repo_root.resolve()}") == 1
