"""Tests for the runtime-managed daily-audit self repo."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agent.config import settings


def test_ensure_daily_audit_self_repo_checkout_bootstraps_runtime_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent.selfevolution import repo as self_repo

    calls = {}
    source_root = tmp_path / "image-root"
    source_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_repo, "_source_repo_root", lambda: source_root)
    monkeypatch.setattr(
        self_repo,
        "_seed_local_service_repo",
        lambda *, source_root, repo_root: (
            Path(repo_root).mkdir(parents=True, exist_ok=True),
            [(Path(repo_root) / relative).mkdir(parents=True, exist_ok=True) for relative in self_repo._REQUIRED_SELFEVOLUTION_ASSETS],
            (Path(repo_root) / ".git").mkdir(parents=True, exist_ok=True),
            calls.setdefault(
                "seed",
                {"source_root": source_root, "repo_root": repo_root},
            ),
        )[-1],
    )
    monkeypatch.setattr(self_repo, "_git_init_local_service_repo", lambda repo_root: calls.setdefault("init", str(repo_root)))
    original_validate = self_repo._validate_local_service_repo
    monkeypatch.setattr(
        self_repo,
        "_validate_local_service_repo",
        lambda repo_root: (
            original_validate(repo_root),
            calls.setdefault("validate", str(repo_root)),
        )[-1],
    )

    repo_root = self_repo.ensure_self_repo_checkout()

    assert repo_root == tmp_path / "service-repo" / "open-review"
    assert calls["seed"]["repo_root"] == repo_root
    assert calls["seed"]["source_root"] == source_root
    assert calls["init"] == str(repo_root)
    assert calls["validate"] == str(repo_root)


def test_ensure_daily_audit_self_repo_checkout_repairs_incomplete_runtime_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent.selfevolution import repo as self_repo

    calls = {}
    source_root = tmp_path / "image-root"
    source_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_repo, "_source_repo_root", lambda: source_root)

    repo_root = tmp_path / "service-repo" / "open-review"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "partial.txt").write_text("incomplete bootstrap", encoding="utf-8")

    def fake_seed_local_service_repo(*, source_root, repo_root):
        assert not Path(repo_root).exists()
        Path(repo_root).mkdir(parents=True, exist_ok=True)
        for relative in self_repo._REQUIRED_SELFEVOLUTION_ASSETS:
            (Path(repo_root) / relative).mkdir(parents=True, exist_ok=True)
        calls.setdefault(
            "seed",
            {"source_root": source_root, "repo_root": repo_root},
        )

    monkeypatch.setattr(self_repo, "_seed_local_service_repo", fake_seed_local_service_repo)
    monkeypatch.setattr(
        self_repo,
        "_git_init_local_service_repo",
        lambda repo_root: (
            (Path(repo_root) / ".git").mkdir(parents=True, exist_ok=True),
            calls.setdefault("init", str(repo_root)),
        )[-1],
    )
    original_validate = self_repo._validate_local_service_repo
    monkeypatch.setattr(
        self_repo,
        "_validate_local_service_repo",
        lambda repo_root: (
            original_validate(repo_root),
            calls.setdefault("validate", str(repo_root)),
        )[-1],
    )

    repaired_root = self_repo.ensure_self_repo_checkout()

    assert repaired_root == repo_root
    assert calls["seed"]["repo_root"] == repo_root
    assert calls["seed"]["source_root"] == source_root
    assert calls["init"] == str(repo_root)
    assert calls["validate"] == str(repo_root)
    assert not (repo_root / "partial.txt").exists()


def test_ensure_daily_audit_self_repo_checkout_syncs_shared_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent.selfevolution import repo as self_repo

    source_root = tmp_path / "image-root"
    source_skill = source_root / "agent" / "scenes" / "skills" / "using-superpowers" / "SKILL.md"
    source_skill.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("---\nname: using-superpowers\ndescription: shared\n---\n\nUse skills.\n", encoding="utf-8")
    monkeypatch.setattr(self_repo, "_source_repo_root", lambda: source_root)

    repo_root = tmp_path / "service-repo" / "open-review"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    for relative in self_repo._REQUIRED_SELFEVOLUTION_ASSETS:
        (repo_root / relative).mkdir(parents=True, exist_ok=True)

    result = self_repo.ensure_self_repo_checkout()

    assert result == repo_root
    assert (
        repo_root / "agent" / "scenes" / "skills" / "using-superpowers" / "SKILL.md"
    ).read_text(encoding="utf-8") == source_skill.read_text(encoding="utf-8")


def test_ensure_daily_audit_self_repo_checkout_serializes_concurrent_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent.selfevolution import repo as self_repo

    source_root = tmp_path / "image-root"
    source_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_repo, "_source_repo_root", lambda: source_root)

    bootstrap_started = threading.Event()
    allow_bootstrap_finish = threading.Event()
    errors: list[Exception] = []
    results: list[Path] = []

    def fake_seed_local_service_repo(*, source_root, repo_root):
        Path(repo_root).mkdir(parents=True, exist_ok=True)
        for relative in self_repo._REQUIRED_SELFEVOLUTION_ASSETS:
            (Path(repo_root) / relative).mkdir(parents=True, exist_ok=True)

    def fake_git_init_local_service_repo(repo_root):
        bootstrap_started.set()
        allow_bootstrap_finish.wait(timeout=5)
        (Path(repo_root) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(self_repo, "_seed_local_service_repo", fake_seed_local_service_repo)
    monkeypatch.setattr(self_repo, "_git_init_local_service_repo", fake_git_init_local_service_repo)

    def invoke_checkout():
        try:
            results.append(self_repo.ensure_self_repo_checkout())
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=invoke_checkout)
    second = threading.Thread(target=invoke_checkout)
    first.start()
    assert bootstrap_started.wait(timeout=1)
    second.start()

    time.sleep(0.2)
    assert errors == []
    assert results == []

    allow_bootstrap_finish.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert results[0] == tmp_path / "service-repo" / "open-review"
    assert results[1] == tmp_path / "service-repo" / "open-review"
