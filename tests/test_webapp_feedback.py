from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient
import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
import agent.webapp as webapp_module
from agent.webapp import app


@pytest.mark.asyncio
async def test_webapp_lifespan_does_not_block_on_gitlab_identity_prime(monkeypatch):
    finished = threading.Event()

    def slow_prime():
        time.sleep(0.2)
        finished.set()

    def schedule_slow_prime(*, logger, context):
        del logger, context
        return asyncio.create_task(asyncio.to_thread(slow_prime))

    monkeypatch.setattr(webapp_module, "configure_phoenix_tracing", lambda: None)
    monkeypatch.setattr(webapp_module, "schedule_bot_identity_prime", schedule_slow_prime)

    started_at = time.perf_counter()
    async with webapp_module._app_lifespan(app):
        elapsed = time.perf_counter() - started_at
        assert elapsed < 0.1
        await asyncio.sleep(0.25)

    assert finished.is_set()


def test_webhook_records_mr_note_feedback_without_enqueuing_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(settings, "GITLAB_TARGET_PROJECTS", ["team/project"])
    monkeypatch.setattr("agent.webapp.get_bot_username", lambda: "open-review-bot")
    monkeypatch.setattr(
        "agent.webapp.schedule_bot_identity_prime",
        lambda **_kwargs: asyncio.create_task(asyncio.sleep(0)),
    )
    monkeypatch.setattr("agent.webapp.configure_phoenix_tracing", lambda: None)

    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "succeeded",
            "head_sha": "head123",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    tracking.append_published_object(
        "runtime-run-1",
        {
            "channel": "mr_note",
            "external_id": "91",
            "object_kind": "mr_note",
            "mr_iid": 42,
            "body_snapshot": "summary body",
            "marker_map": {"open-review-summary-kind": "auto-review"},
        },
    )

    enqueued = []

    async def fake_enqueue(event):
        enqueued.append(event)

    monkeypatch.setattr("agent.webapp.enqueue_gitlab_event", fake_enqueue)

    client = TestClient(app)
    response = client.post(
        "/webhooks/gitlab",
        headers={"X-Gitlab-Token": "secret"},
        json={
            "object_kind": "note",
            "event_type": "note",
            "user": {"username": "reviewer"},
            "project": {"path_with_namespace": "team/project"},
            "merge_request": {
                "iid": 42,
                "source_branch": "feature/router",
                "last_commit": {"id": "head123"},
            },
            "object_attributes": {
                "id": 100,
                "note": "这个建议不错，不过还需要补充证据。",
                "created_at": "2026-04-20T10:06:00+08:00",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["scene"] == "feedback"
    assert enqueued == []
    run = tracking.get_run("runtime-run-1")
    assert run is not None
    assert run["feedback_events"][0]["feedback_kind"] == "mr_note"
    assert run["feedback_events"][0]["association_method"] == "latest_auto_review_same_head_sha"


def test_webhook_records_issue_note_feedback_for_daily_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(settings, "GITLAB_TARGET_PROJECTS", ["team/project"])
    monkeypatch.setattr("agent.webapp.get_bot_username", lambda: "open-review-bot")
    monkeypatch.setattr(
        "agent.webapp.schedule_bot_identity_prime",
        lambda **_kwargs: asyncio.create_task(asyncio.sleep(0)),
    )
    monkeypatch.setattr("agent.webapp.configure_phoenix_tracing", lambda: None)

    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    tracking.set_published_issue_iid("runtime-run-1", 17)

    client = TestClient(app)
    response = client.post(
        "/webhooks/gitlab",
        headers={"X-Gitlab-Token": "secret"},
        json={
            "object_kind": "note",
            "event_type": "note",
            "user": {"username": "reviewer"},
            "project": {"path_with_namespace": "team/project"},
            "issue": {"iid": 17},
            "object_attributes": {
                "id": 201,
                "note": "这里的结论可以再补一个复现步骤。",
                "created_at": "2026-04-20T10:08:00+08:00",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["scene"] == "feedback"
    run = tracking.get_run("runtime-run-1")
    assert run is not None
    assert run["feedback_events"][0]["feedback_kind"] == "issue_note"
    assert run["feedback_events"][0]["association_method"] == "published_issue_iid"
