"""Tests for GitLab deployment verification and webhook sync helpers."""

from __future__ import annotations

from types import SimpleNamespace

import agent.gitlab.deploy as deploy


def test_verify_gitlab_configuration_reports_missing_required_fields():
    result = deploy.verify_gitlab_configuration(
        snapshot={
            "GITLAB_API_URL": "",
            "GITLAB_TOKEN": "",
            "GITLAB_WEBHOOK_SECRET": "",
            "GITLAB_TARGET_PROJECTS": [],
            "GITLAB_EXTERNAL_URL": "",
            "OPEN_REVIEW_EXTERNAL_URL": "",
        }
    )

    assert result["status"] == "invalid"
    checks = {item["key"]: item for item in result["checks"]}
    assert checks["api_url"]["status"] == "error"
    assert checks["token"]["status"] == "error"
    assert checks["target_projects"]["status"] == "error"
    assert checks["open_review_external_url"]["status"] == "error"


def test_verify_gitlab_configuration_checks_api_identity_projects_and_webhook_health(monkeypatch):
    class _FakeClient:
        def __init__(self):
            self.projects = SimpleNamespace(
                get=lambda project_id: SimpleNamespace(
                    id=7 if str(project_id) == "team/service" else 8,
                    path_with_namespace=str(project_id),
                    archived=False,
                )
            )

        def http_get(self, path):
            assert path == "/user"
            return {"username": "open-review-bot", "name": "Open Review Bot"}

    monkeypatch.setattr(deploy, "get_gitlab_client", lambda: _FakeClient())
    monkeypatch.setattr(deploy, "_probe_health_url", lambda url: (True, f"{url} ok"))

    result = deploy.verify_gitlab_configuration(
        snapshot={
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_TOKEN": "secret-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"],
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "OPEN_REVIEW_EXTERNAL_URL": "https://open_review.example.com",
        }
    )

    assert result["status"] == "ready"
    checks = {item["key"]: item for item in result["checks"]}
    assert checks["api"]["status"] == "ok"
    assert checks["bot_identity"]["message"] == "当前 Token 对应用户：open-review-bot。"
    assert checks["target_projects"]["status"] == "ok"
    assert checks["webhook_health"]["status"] == "ok"
    assert result["target_projects"] == ["team/service", "team/webapp"]
    assert [(item["project_id"], item["project_path"], item["status"], item["detail"]) for item in result["results"]] == [
        (7, "team/service", "ok", "Project 可访问。"),
        (8, "team/webapp", "ok", "Project 可访问。"),
    ]
    assert result["webhook_url"] == "https://open_review.example.com/webhooks/gitlab"


def test_verify_gitlab_configuration_returns_partial_when_some_projects_are_inaccessible(monkeypatch):
    class _FakeClient:
        def __init__(self):
            def _get(project_id):
                if str(project_id) == "team/missing":
                    raise RuntimeError("404 Project Not Found")
                return SimpleNamespace(id=7, path_with_namespace=str(project_id), archived=False)

            self.projects = SimpleNamespace(get=_get)

        def http_get(self, path):
            assert path == "/user"
            return {"username": "open-review-bot", "name": "Open Review Bot"}

    monkeypatch.setattr(deploy, "get_gitlab_client", lambda: _FakeClient())
    monkeypatch.setattr(deploy, "_probe_health_url", lambda url: (True, f"{url} ok"))

    result = deploy.verify_gitlab_configuration(
        snapshot={
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_TOKEN": "secret-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "GITLAB_TARGET_PROJECTS": ["team/service", "team/missing"],
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "OPEN_REVIEW_EXTERNAL_URL": "https://open_review.example.com",
        }
    )

    assert result["status"] == "partial"
    checks = {item["key"]: item for item in result["checks"]}
    assert checks["target_projects"]["status"] == "ok"
    assert result["target_projects"] == ["team/service", "team/missing"]
    assert result["results"][0]["status"] == "ok"
    assert result["results"][1]["status"] == "error"


def test_verify_gitlab_configuration_normalizes_gitlab_url_targets(monkeypatch):
    class _FakeClient:
        def __init__(self):
            self.projects = SimpleNamespace(
                get=lambda project_id: SimpleNamespace(
                    id=7 if str(project_id) == "team/service" else 8,
                    path_with_namespace=str(project_id),
                    archived=False,
                )
            )

        def http_get(self, path):
            assert path == "/user"
            return {"username": "open-review-bot", "name": "Open Review Bot"}

    monkeypatch.setattr(deploy, "get_gitlab_client", lambda: _FakeClient())
    monkeypatch.setattr(deploy, "_probe_health_url", lambda url: (True, f"{url} ok"))

    result = deploy.verify_gitlab_configuration(
        snapshot={
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_TOKEN": "secret-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "GITLAB_TARGET_PROJECTS": [
                "https://gitlab.example.com/team/service.git",
                "https://gitlab.example.com/team/webapp/",
            ],
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "OPEN_REVIEW_EXTERNAL_URL": "https://open_review.example.com",
        }
    )

    assert result["status"] == "ready"
    assert result["target_projects"] == ["team/service", "team/webapp"]


def test_sync_gitlab_webhooks_returns_manual_fallback_for_permission_errors(monkeypatch):
    class _ForbiddenError(RuntimeError):
        response_code = 403

    projects = [
        SimpleNamespace(id=7, path_with_namespace="team/service"),
        SimpleNamespace(id=8, path_with_namespace="team/webapp"),
    ]

    class _FakeClient:
        def __init__(self):
            def _get(project_id):
                for project in projects:
                    if project.path_with_namespace == str(project_id):
                        return project
                raise RuntimeError("404 Project Not Found")

            self.projects = SimpleNamespace(get=_get)

        def http_get(self, path):
            assert path == "/user"
            return {"username": "open-review-bot"}

    monkeypatch.setattr(deploy, "get_gitlab_client", lambda: _FakeClient())
    monkeypatch.setattr(
        deploy,
        "_list_target_projects",
        lambda _client, *, target_projects: projects,
    )
    monkeypatch.setattr(deploy, "_probe_health_url", lambda url: (True, f"{url} ok"))

    def fake_sync(project, *, webhook_url, webhook_secret):
        assert webhook_url == "https://open_review.example.com/webhooks/gitlab"
        assert webhook_secret == "secret-webhook"
        if project.id == 8:
            raise _ForbiddenError("403 Forbidden")
        return {"status": "updated", "detail": "Webhook 已更新。"}

    monkeypatch.setattr(deploy, "_sync_project_webhook", fake_sync)

    result = deploy.sync_gitlab_webhooks(
        snapshot={
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_TOKEN": "secret-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"],
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "OPEN_REVIEW_EXTERNAL_URL": "https://open_review.example.com",
        }
    )

    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "updated"
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["detail"] == "403 Forbidden"
    assert result["target_projects"] == ["team/service", "team/webapp"]
    assert "team/webapp" in result["manual_instructions"]


def test_sync_gitlab_webhooks_supports_multiple_project_targets(monkeypatch):
    projects = [
        SimpleNamespace(id=7, path_with_namespace="team/service"),
        SimpleNamespace(id=8, path_with_namespace="team/webapp"),
    ]

    class _FakeClient:
        def __init__(self):
            def _get(project_id):
                for project in projects:
                    if project.path_with_namespace == str(project_id):
                        return project
                raise RuntimeError("404 Project Not Found")

            self.projects = SimpleNamespace(get=_get)

        def http_get(self, path):
            assert path == "/user"
            return {"username": "open-review-bot"}

    monkeypatch.setattr(deploy, "get_gitlab_client", lambda: _FakeClient())
    monkeypatch.setattr(deploy, "_probe_health_url", lambda url: (True, f"{url} ok"))
    monkeypatch.setattr(
        deploy,
        "_list_target_projects",
        lambda _client, *, target_projects: projects,
    )
    monkeypatch.setattr(
        deploy,
        "_sync_project_webhook",
        lambda project, *, webhook_url, webhook_secret: {"status": "updated", "detail": f"{project.path_with_namespace} ok"},
    )

    result = deploy.sync_gitlab_webhooks(
        snapshot={
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_TOKEN": "secret-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"],
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "OPEN_REVIEW_EXTERNAL_URL": "https://open_review.example.com",
        }
    )

    assert result["status"] == "ok"
    assert result["target_projects"] == ["team/service", "team/webapp"]
    assert result["results"] == [
        {
            "project_id": 7,
            "project_path": "team/service",
            "status": "updated",
            "detail": "team/service ok",
        },
        {
            "project_id": 8,
            "project_path": "team/webapp",
            "status": "updated",
            "detail": "team/webapp ok",
        }
    ]
