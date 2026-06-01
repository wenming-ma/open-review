"""Tests for runtime publish idempotency helpers."""

from __future__ import annotations

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.runtime.models import PublishReceipt
from agent.runtime.publish import GitLabPublishService
from agent.runtime.store import InMemoryRuntimeStore


@pytest.mark.asyncio
async def test_publish_service_skips_duplicate_mr_note_ops(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    store = InMemoryRuntimeStore()
    service = GitLabPublishService(store=store, actor_key="team/project!42", tracking_run_id="runtime-run-1")
    calls = {"count": 0}

    def fake_publish():
        calls["count"] += 1
        return 91

    first = await service.publish_mr_note(
        op_key="summary:auto-review:head123",
        publisher=fake_publish,
        record={
            "object_kind": "mr_note",
            "mr_iid": 42,
            "body_snapshot": "summary body",
            "marker_map": {"open-review-summary-kind": "auto-review"},
        },
    )
    second = await service.publish_mr_note(
        op_key="summary:auto-review:head123",
        publisher=fake_publish,
        record={
            "object_kind": "mr_note",
            "mr_iid": 42,
            "body_snapshot": "summary body",
            "marker_map": {"open-review-summary-kind": "auto-review"},
        },
    )
    run = tracking.list_recent_runs(limit=1)[0]

    assert calls["count"] == 1
    assert first.external_id == "91"
    assert second.external_id == "91"
    assert len(run["published_objects"]) == 1
    assert run["published_objects"][0]["channel"] == "mr_note"
    assert run["published_objects"][0]["external_id"] == "91"
    assert run["published_objects"][0]["object_kind"] == "mr_note"
    assert run["published_objects"][0]["mr_iid"] == 42
    assert run["published_objects"][0]["body_snapshot"] == "summary body"
    assert run["published_objects"][0]["marker_map"] == {"open-review-summary-kind": "auto-review"}
    assert run["published_objects"][0]["created_at"]


@pytest.mark.asyncio
async def test_publish_service_skips_duplicate_discussion_reply_ops():
    store = InMemoryRuntimeStore()
    service = GitLabPublishService(store=store, actor_key="team/project!42")
    calls = {"count": 0}

    def fake_publish():
        calls["count"] += 1
        return 77

    first = await service.publish_discussion_reply(
        op_key="mention-reply:head123:12,13",
        publisher=fake_publish,
    )
    second = await service.publish_discussion_reply(
        op_key="mention-reply:head123:12,13",
        publisher=fake_publish,
    )

    assert calls["count"] == 1
    assert first.external_id == "77"
    assert second.external_id == "77"


@pytest.mark.asyncio
async def test_publish_service_uses_existing_claim_without_republishing():
    store = InMemoryRuntimeStore()
    service = GitLabPublishService(store=store, actor_key="team/project!42")
    await store.record_publish_receipt(
        service._make_receipt(
            op_key="summary:auto-review:head123",
            channel="mr_note",
            external_id=None,
            status="claimed",
        )
    )
    calls = {"count": 0}

    def fake_publish():
        calls["count"] += 1
        return 91

    receipt = await service.publish_mr_note(
        op_key="summary:auto-review:head123",
        publisher=fake_publish,
    )

    assert calls["count"] == 0
    assert receipt.status == "claimed"
    assert receipt.external_id is None


@pytest.mark.asyncio
async def test_publish_service_reclaims_stale_claim_and_republishes(monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_PUBLISH_CLAIM_TTL_SECONDS", 1, raising=False)
    store = InMemoryRuntimeStore()
    service = GitLabPublishService(store=store, actor_key="team/project!42")
    await store.record_publish_receipt(
        PublishReceipt(
            actor_key="team/project!42",
            op_key="summary:auto-review:head123",
            channel="mr_note",
            external_id=None,
            status="claimed",
            created_at="2020-01-01T00:00:00+08:00",
        )
    )
    calls = {"count": 0}

    def fake_publish():
        calls["count"] += 1
        return 92

    receipt = await service.publish_mr_note(
        op_key="summary:auto-review:head123",
        publisher=fake_publish,
    )

    stored = await store.get_publish_receipt("team/project!42", "summary:auto-review:head123")
    assert calls["count"] == 1
    assert receipt.status == "completed"
    assert receipt.external_id == "92"
    assert stored is not None
    assert stored.status == "completed"
    assert stored.external_id == "92"


@pytest.mark.asyncio
async def test_publish_service_retries_failed_receipt():
    store = InMemoryRuntimeStore()
    service = GitLabPublishService(store=store, actor_key="team/project!42")
    await store.record_publish_receipt(
        PublishReceipt(
            actor_key="team/project!42",
            op_key="summary:auto-review:head123",
            channel="mr_note",
            external_id=None,
            status="failed",
        )
    )
    calls = {"count": 0}

    def fake_publish():
        calls["count"] += 1
        return 93

    receipt = await service.publish_mr_note(
        op_key="summary:auto-review:head123",
        publisher=fake_publish,
    )

    assert calls["count"] == 1
    assert receipt.status == "completed"
    assert receipt.external_id == "93"
