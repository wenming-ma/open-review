"""Tests for GitLab API client creation."""

from __future__ import annotations

from types import SimpleNamespace

import agent.gitlab.client as gitlab_client


def test_get_gitlab_client_uses_api_url_and_keeps_base_url(monkeypatch):
    captured = {}

    class _FakeGitlab:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(gitlab_client, "gitlab", SimpleNamespace(Gitlab=_FakeGitlab))
    monkeypatch.setattr(gitlab_client.settings, "GITLAB_API_URL", "https://gitlab-api.example.com")
    monkeypatch.setattr(gitlab_client.settings, "GITLAB_TOKEN", "secret-token")
    monkeypatch.setattr(gitlab_client.settings, "GITLAB_SSL_VERIFY", False)

    gitlab_client.get_gitlab_client()

    assert captured == {
        "url": "https://gitlab-api.example.com",
        "private_token": "secret-token",
        "ssl_verify": False,
        "keep_base_url": True,
    }
