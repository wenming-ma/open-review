"""Test webhook event parsing and routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.config import settings
from agent.webapp import app

client = TestClient(app)

HEADERS = {"X-Gitlab-Token": "test-secret"}


@pytest.fixture(autouse=True)
def _set_webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "test-secret")


@pytest.fixture(autouse=True)
def _set_target_projects(monkeypatch):
    monkeypatch.setattr(settings, "GITLAB_TARGET_PROJECTS", ["team/service-project"])


@pytest.fixture(autouse=True)
def _mock_enqueue(monkeypatch):
    """Mock the durable runtime enqueue path so tests stay isolated."""
    import agent.webapp as wa

    enqueue = AsyncMock()
    monkeypatch.setattr(wa, "enqueue_gitlab_event", enqueue)
    return enqueue


@pytest.fixture(autouse=True)
def _mock_bot_username(monkeypatch):
    import agent.webapp as wa

    monkeypatch.setattr(wa, "get_bot_username", lambda **_kwargs: "open-review-bot")


# -- Health check --

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# -- Auth --

def test_rejects_bad_token():
    r = client.post("/webhooks/gitlab", json={}, headers={"X-Gitlab-Token": "wrong"})
    assert r.status_code == 401


# -- MR events --

def test_mr_open_triggers_auto_review(_mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer", "name": "Dev"},
        "project": {"path_with_namespace": "team/service-project", "web_url": "http://gitlab/team/service-project"},
        "object_attributes": {
            "iid": 42,
            "action": "open",
            "url": "http://gitlab/team/service-project/-/merge_requests/42",
            "title": "Fix report export",
            "source_branch": "fix/report-export",
            "target_branch": "main",
            "draft": False,
        },
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "accepted"
    assert data["scene"] == "auto_review"
    _mock_enqueue.assert_awaited_once()


def test_draft_mr_is_ignored():
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "iid": 43,
            "action": "open",
            "title": "Draft: WIP feature",
            "draft": True,
            "source_branch": "feat/wip",
            "target_branch": "main",
        },
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.json()["reason"] == "draft MR"


def test_bot_mr_open_triggers_auto_review(_mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "open-review-bot"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "iid": 44,
            "action": "open",
            "draft": False,
            "title": "Bot generated fix",
            "source_branch": "x",
            "target_branch": "main",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["scene"] == "auto_review"
    _mock_enqueue.assert_awaited_once()


def test_bot_mr_update_triggers_auto_review_push(_mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "open-review-bot"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "iid": 44,
            "action": "update",
            "oldrev": "abc123",
            "draft": False,
            "title": "Bot generated fix",
            "source_branch": "x",
            "target_branch": "main",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["scene"] == "auto_review_push"
    _mock_enqueue.assert_awaited_once()


def test_push_to_mr_triggers_review(_mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "iid": 42,
            "action": "update",
            "oldrev": "abc123",
            "title": "Fix report export",
            "draft": False,
            "source_branch": "fix/report-export",
            "target_branch": "main",
        },
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    data = r.json()
    assert data["status"] == "accepted"
    assert data["scene"] == "auto_review_push"
    _mock_enqueue.assert_awaited_once()


@pytest.mark.parametrize("action", ["close", "merge"])
def test_mr_terminal_event_triggers_sandbox_cleanup(action, _mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "iid": 42,
            "action": action,
            "title": "Fix report export",
            "draft": False,
            "source_branch": "fix/report-export",
            "target_branch": "main",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "accepted"
    assert data["scene"] == "sandbox_cleanup"
    event = _mock_enqueue.await_args.args[0]
    assert event.event_type == "sandbox_cleanup"
    assert event.project_id == "team/service-project"
    assert event.mr_iid == 42


def test_mr_open_is_not_filtered_by_legacy_ignore_settings(monkeypatch, _mock_enqueue):
    monkeypatch.setattr(settings, "IGNORED_PROJECTS", [".*"], raising=False)
    monkeypatch.setattr(settings, "IGNORED_SOURCE_BRANCHES", [".*"], raising=False)
    monkeypatch.setattr(settings, "IGNORED_TARGET_BRANCHES", [".*"], raising=False)
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer", "name": "Dev"},
        "project": {"path_with_namespace": "team/service-project", "web_url": "http://gitlab/team/service-project"},
        "object_attributes": {
            "iid": 42,
            "action": "open",
            "url": "http://gitlab/team/service-project/-/merge_requests/42",
            "title": "Fix report export",
            "source_branch": "fix/report-export",
            "target_branch": "main",
            "draft": False,
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["scene"] == "auto_review"
    _mock_enqueue.assert_awaited_once()


def test_mr_event_for_unconfigured_project_is_ignored(_mock_enqueue):
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/other-project"},
        "object_attributes": {
            "iid": 45,
            "action": "open",
            "title": "Unconfigured project",
            "draft": False,
            "source_branch": "feat/x",
            "target_branch": "main",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["reason"] == "project not configured"
    _mock_enqueue.assert_not_awaited()


# -- Note events --

def test_mention_triggers_scene(_mock_enqueue):
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 999,
            "note": "@open-review-bot fix the null pointer issue",
            "type": "DiscussionNote",
        },
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    data = r.json()
    assert data["status"] == "accepted"
    assert data["scene"] == "mention"
    _mock_enqueue.assert_awaited_once()


def test_bot_note_is_ignored_to_prevent_comment_loops(_mock_enqueue):
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "open-review-bot"},
        "project": {"path_with_namespace": "team/service-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 1005,
            "note": "@open-review-bot follow up",
            "type": "DiscussionNote",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert r.json()["reason"] == "bot user"
    _mock_enqueue.assert_not_awaited()


def test_comment_without_mention_is_ignored():
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 1000,
            "note": "LGTM, looks good",
        },
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.json()["reason"] == "no @mention"


def test_comment_with_bot_username_prefix_only_is_ignored(_mock_enqueue):
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 1004,
            "note": "@open-review-bot2 should not trigger the bot",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["reason"] == "no @mention"
    _mock_enqueue.assert_not_awaited()


def test_mention_matching_uses_resolved_bot_username(monkeypatch, _mock_enqueue):
    import agent.webapp as wa

    monkeypatch.setattr(wa, "get_bot_username", lambda **_kwargs: "root")
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 1002,
            "note": "@root fix the null pointer issue",
            "type": "DiscussionNote",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["scene"] == "mention"


def test_comment_on_issue_is_ignored():
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/service-project"},
        "object_attributes": {
            "id": 1001,
            "note": "@open-review-bot help",
        },
        # No "merge_request" key → this is an issue comment
    }
    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)
    assert r.json()["reason"] == "not a MR comment"


def test_mention_for_unconfigured_project_is_ignored(_mock_enqueue):
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "developer"},
        "project": {"path_with_namespace": "team/other-project"},
        "merge_request": {"iid": 42},
        "object_attributes": {
            "id": 1003,
            "note": "@open-review-bot help",
        },
    }

    r = client.post("/webhooks/gitlab", json=payload, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["reason"] == "project not configured"
    _mock_enqueue.assert_not_awaited()
