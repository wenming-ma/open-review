"""Tests for project-level GitLab helpers used by daily audit."""

from __future__ import annotations

from types import SimpleNamespace

from agent.gitlab import project_ops


def test_get_project_default_branch_returns_project_default(monkeypatch):
    project = SimpleNamespace(default_branch="main")

    monkeypatch.setattr(project_ops, "get_project", lambda project_id: project)

    branch = project_ops.get_project_default_branch("team/project")

    assert branch == "main"


def test_create_project_issue_uses_project_api(monkeypatch):
    saved = {}

    def fake_create(payload):
        saved.update(payload)
        return SimpleNamespace(iid=23)

    project = SimpleNamespace(issues=SimpleNamespace(create=fake_create))

    monkeypatch.setattr(project_ops, "get_project", lambda project_id: project)

    issue_iid = project_ops.create_project_issue(
        "team/project",
        title="Daily findings",
        description="new body",
    )

    assert issue_iid == 23
    assert saved["title"] == "Daily findings"
    assert "new body" in saved["description"]


def test_create_project_merge_request_uses_project_api(monkeypatch):
    created = {}

    def fake_create(payload):
        created.update(payload)
        return SimpleNamespace(iid=9, web_url="http://gitlab/team/project/-/merge_requests/9")

    project = SimpleNamespace(mergerequests=SimpleNamespace(create=fake_create))

    monkeypatch.setattr(project_ops, "get_project", lambda project_id: project)

    result = project_ops.create_project_merge_request(
        "team/project",
        source_branch="open-review/daily-audit/foo",
        target_branch="main",
        title="fix: optimize foo",
        description="body",
        draft=True,
    )

    assert result.iid == 9
    assert created == {
        "source_branch": "open-review/daily-audit/foo",
        "target_branch": "main",
        "title": "Draft: fix: optimize foo",
        "description": "body",
        "remove_source_branch": False,
    }
