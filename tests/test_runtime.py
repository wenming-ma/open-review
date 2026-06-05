"""Tests for the durable MR actor runtime."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import threading
import time

import pytest

from agent.controlplane import get_tracking_service
from agent.config import settings
from agent.runtime import worker as runtime_worker
from agent.runtime.models import EventEnvelope, PublishReceipt, RunCheckpoint, RunJournalEvent
from agent.runtime.queue import (
    MR_ACTOR_JOB_NAME,
    enqueue_gitlab_event,
    reset_runtime_clients,
    resume_runtime_processing,
)
from agent.runtime.store import InMemoryRuntimeStore, SQLiteRuntimeStore
from agent.runtime.worker import RuntimeHandlers, drain_mr_actor


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def enqueue_job(self, job_name: str, *args):
        self.calls.append((job_name, args))


@pytest.fixture(autouse=True)
def _reset_runtime_test_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    from agent.controlplane import reset_controlplane_services

    reset_controlplane_services()
    reset_runtime_clients()
    yield
    reset_controlplane_services()
    reset_runtime_clients()


def _auto_event(event_id: str, head_sha: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        target_branch="main",
        title="Fix router regression",
        head_sha=head_sha,
        payload={"kind": "merge_request"},
    )


def _mention_event(event_id: str, note_id: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type="mention",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-mention",
        note_id=note_id,
        discussion_id="disc-1",
        note_body="please explain this change",
        note_author="developer",
        received_at=datetime.now(UTC).isoformat(),
        payload={"kind": "note"},
    )


def _daily_event(event_id: str, branch: str = "main", event_type: str = "daily_audit") -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        project_id="team/project",
        mr_iid=None,
        source_branch=branch,
        target_branch=branch,
        title="Daily audit run",
        payload={"kind": event_type, "default_branch": branch},
    )


def test_runtime_model_defaults_use_beijing_offset():
    event = EventEnvelope(
        event_id="evt-1",
        event_type="daily_audit",
        project_id="team/project",
    )
    run = runtime_worker.RunRecord(
        run_id="run-1",
        actor_key="team/project!daily_audit",
        event_type="daily_audit",
        project_id="team/project",
    )
    receipt = PublishReceipt(
        actor_key="team/project!daily_audit",
        op_key="publish-1",
        channel="mr_note",
    )
    checkpoint = RunCheckpoint(
        execution_key="exec-1",
        actor_key="team/project!daily_audit",
        scene="daily_audit",
        workflow_version="daily_audit.v1",
        stage_key="analysis",
    )
    journal = RunJournalEvent(
        execution_key="exec-1",
        actor_key="team/project!daily_audit",
        scene="daily_audit",
        workflow_version="daily_audit.v1",
        event_type="stage_started",
        status="running",
    )

    assert datetime.fromisoformat(event.received_at).utcoffset() == timedelta(hours=8)
    assert datetime.fromisoformat(run.started_at).utcoffset() == timedelta(hours=8)
    assert datetime.fromisoformat(receipt.created_at).utcoffset() == timedelta(hours=8)
    assert datetime.fromisoformat(checkpoint.updated_at).utcoffset() == timedelta(hours=8)
    assert datetime.fromisoformat(journal.created_at).utcoffset() == timedelta(hours=8)


@pytest.mark.asyncio
async def test_enqueue_gitlab_event_only_schedules_actor_once():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)
    await enqueue_gitlab_event(_auto_event("evt-2", "head-2"), store=store, queue=queue)

    assert queue.calls == [(MR_ACTOR_JOB_NAME, ("team/project!42",))]
    assert [item.event_id for item in await store.list_actor_events("team/project!42")] == ["evt-1", "evt-2"]


@pytest.mark.asyncio
async def test_enqueue_gitlab_event_drops_duplicate_event_ids():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    event = _auto_event("evt-1", "head-1")

    await enqueue_gitlab_event(event, store=store, queue=queue)
    await enqueue_gitlab_event(event, store=store, queue=queue)

    assert queue.calls == [(MR_ACTOR_JOB_NAME, ("team/project!42",))]
    assert [item.event_id for item in await store.list_actor_events("team/project!42")] == ["evt-1"]


def test_daily_audit_event_uses_project_level_actor_key():
    event = _daily_event("evt-daily-1")

    assert event.actor_key == "team/project!daily_audit"


def test_agent_self_evolution_event_uses_agent_scoped_actor_key():
    event = EventEnvelope(
        event_id="evt-evo-1",
        event_type="agent_self_evolution",
        project_id="team/project",
        payload={"agent_type": "daily_audit"},
    )

    assert event.actor_key == "team/project!self_evolution"


@pytest.mark.asyncio
async def test_worker_startup_does_not_block_on_gitlab_identity_prime(monkeypatch):
    finished = threading.Event()

    def slow_prime():
        time.sleep(0.2)
        finished.set()

    def schedule_slow_prime(*, logger, context):
        del logger, context
        return asyncio.create_task(asyncio.to_thread(slow_prime))

    monkeypatch.setattr(runtime_worker, "configure_phoenix_tracing", lambda: None)
    monkeypatch.setattr(runtime_worker, "schedule_bot_identity_prime", schedule_slow_prime)
    monkeypatch.setattr(
        "agent.sandbox.manager.configure_runtime_sandbox_config",
        lambda *_args, **_kwargs: None,
    )

    async def fake_resume_runtime_processing():
        return 0

    monkeypatch.setattr(runtime_worker, "resume_runtime_processing", fake_resume_runtime_processing)

    started_at = time.perf_counter()
    await runtime_worker._worker_startup(None)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.15
    await asyncio.sleep(0.25)
    assert finished.is_set()


def test_daily_audit_direction_persistence_event_uses_dedicated_actor_key():
    event = _daily_event("evt-daily-direction-1", event_type="daily_audit_direction_persistence")

    assert event.actor_key == "team/project!daily_audit_direction_persistence"


def test_daily_audit_short_term_persistence_event_uses_dedicated_actor_key():
    event = _daily_event("evt-daily-short-1", event_type="daily_audit_short_term_persistence")

    assert event.actor_key == "team/project!daily_audit_short_term_persistence"


def test_daily_audit_long_term_persistence_event_uses_dedicated_actor_key():
    event = _daily_event("evt-daily-long-1", event_type="daily_audit_long_term_persistence")

    assert event.actor_key == "team/project!daily_audit_long_term_persistence"


def test_daily_audit_skill_persistence_event_uses_dedicated_actor_key():
    event = _daily_event("evt-daily-skill-1", event_type="daily_audit_skill_persistence")

    assert event.actor_key == "team/project!daily_audit_skill_persistence"


@pytest.mark.asyncio
async def test_drain_mr_actor_serializes_mixed_events_and_coalesces_auto_review():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[tuple[str, str | int]] = []

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)
    await enqueue_gitlab_event(_auto_event("evt-2", "head-2"), store=store, queue=queue)
    await enqueue_gitlab_event(_mention_event("evt-3", 101), store=store, queue=queue)
    await enqueue_gitlab_event(_auto_event("evt-4", "head-3"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda event: calls.append(("auto_review", event.head_sha or "")),
        run_mention=lambda event: calls.append(("mention", event.note_id or 0)),
    )

    await drain_mr_actor("team/project!42", store=store, handlers=handlers)

    assert calls == [
        ("auto_review", "head-2"),
        ("mention", 101),
        ("auto_review", "head-3"),
    ]
    assert await store.list_actor_events("team/project!42") == []
    runs = await store.list_runs("team/project!42")
    assert [(item.event_type, item.state, item.batch_size) for item in runs] == [
        ("auto_review", "succeeded", 1),
        ("mention", "succeeded", 1),
        ("auto_review", "succeeded", 2),
    ]


@pytest.mark.asyncio
async def test_drain_mr_actor_batches_same_discussion_mentions():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[tuple[str, str | int]] = []
    first = _mention_event("evt-1", 101)
    second = _mention_event("evt-2", 102)
    second.received_at = (datetime.fromisoformat(first.received_at) + timedelta(seconds=3)).isoformat()

    await enqueue_gitlab_event(first, store=store, queue=queue)
    await enqueue_gitlab_event(second, store=store, queue=queue)
    await enqueue_gitlab_event(_auto_event("evt-3", "head-3"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda event: calls.append(("auto_review", event.head_sha or "")),
        run_mention=lambda event: calls.append(("mention", event.note_id or 0)),
    )

    await drain_mr_actor("team/project!42", store=store, handlers=handlers)

    assert calls == [
        ("mention", 102),
        ("auto_review", "head-3"),
    ]


@pytest.mark.asyncio
async def test_mention_batch_window_uses_project_agent_config():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    from agent.controlplane import get_config_service

    service = get_config_service()
    service.set_values({"GITLAB_TARGET_PROJECTS": ["team/project"]}, actor="test-suite")
    service.set_project_agent_config(
        "team/project",
        {"MENTION_BATCH_WINDOW_SECONDS": "1"},
        actor="test-suite",
    )
    first = _mention_event("evt-1", 101)
    second = _mention_event("evt-2", 102)
    second.received_at = (datetime.fromisoformat(first.received_at) + timedelta(seconds=3)).isoformat()

    await enqueue_gitlab_event(first, store=store, queue=queue)
    await enqueue_gitlab_event(second, store=store, queue=queue)

    batch = await store.pop_next_batch("team/project!42")

    assert [event.event_id for event in batch] == ["evt-1"]


@pytest.mark.asyncio
async def test_drain_mr_actor_keeps_pending_events_when_lease_is_held():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)
    acquired = await store.acquire_lease("team/project!42", "other-worker", ttl_seconds=30)
    assert acquired is True

    handlers = RuntimeHandlers(run_auto_review=lambda _event: None, run_mention=lambda _event: None)
    drained = await drain_mr_actor("team/project!42", store=store, handlers=handlers, worker_id="worker-1")

    assert drained is False
    assert [item.event_id for item in await store.list_actor_events("team/project!42")] == ["evt-1"]


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_daily_audit_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(_daily_event("evt-daily-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!daily_audit", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-1"]
    runs = await store.list_runs("team/project!daily_audit")
    assert runs[0].event_type == "daily_audit"


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_agent_self_evolution_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(
        EventEnvelope(
            event_id="evt-daily-evo-1",
            event_type="agent_self_evolution",
            project_id="team/project",
            payload={"agent_type": "daily_audit"},
        ),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_agent_self_evolution=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!self_evolution", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-evo-1"]
    runs = await store.list_runs("team/project!self_evolution")
    assert runs[0].event_type == "agent_self_evolution"


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_daily_audit_direction_persistence_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(
        _daily_event("evt-daily-direction-1", event_type="daily_audit_direction_persistence"),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_daily_audit_evolution=lambda _event: None,
        run_daily_audit_direction_persistence=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!daily_audit_direction_persistence", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-direction-1"]
    runs = await store.list_runs("team/project!daily_audit_direction_persistence")
    assert runs[0].event_type == "daily_audit_direction_persistence"


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_daily_audit_short_term_persistence_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(
        _daily_event("evt-daily-short-1", event_type="daily_audit_short_term_persistence"),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_daily_audit_evolution=lambda _event: None,
        run_daily_audit_direction_persistence=lambda _event: None,
        run_daily_audit_short_term_persistence=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!daily_audit_short_term_persistence", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-short-1"]
    runs = await store.list_runs("team/project!daily_audit_short_term_persistence")
    assert runs[0].event_type == "daily_audit_short_term_persistence"


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_daily_audit_long_term_persistence_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(
        _daily_event("evt-daily-long-1", event_type="daily_audit_long_term_persistence"),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_daily_audit_evolution=lambda _event: None,
        run_daily_audit_direction_persistence=lambda _event: None,
        run_daily_audit_short_term_persistence=lambda _event: None,
        run_daily_audit_long_term_persistence=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!daily_audit_long_term_persistence", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-long-1"]
    runs = await store.list_runs("team/project!daily_audit_long_term_persistence")
    assert runs[0].event_type == "daily_audit_long_term_persistence"


@pytest.mark.asyncio
async def test_drain_mr_actor_dispatches_daily_audit_skill_persistence_events():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(
        _daily_event("evt-daily-skill-1", event_type="daily_audit_skill_persistence"),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_daily_audit_evolution=lambda _event: None,
        run_daily_audit_direction_persistence=lambda _event: None,
        run_daily_audit_short_term_persistence=lambda _event: None,
        run_daily_audit_long_term_persistence=lambda _event: None,
        run_daily_audit_skill_persistence=lambda event: calls.append(event.event_id),
    )

    await drain_mr_actor("team/project!daily_audit_skill_persistence", store=store, queue=queue, handlers=handlers)

    assert calls == ["evt-daily-skill-1"]
    runs = await store.list_runs("team/project!daily_audit_skill_persistence")
    assert runs[0].event_type == "daily_audit_skill_persistence"


@pytest.mark.asyncio
async def test_drain_mr_actor_restores_inflight_batch_after_failure():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[str] = []

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    def failing_review(_event):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await drain_mr_actor(
            "team/project!42",
            store=store,
            queue=queue,
            handlers=RuntimeHandlers(run_auto_review=failing_review, run_mention=lambda _event: None),
            worker_id="worker-1",
        )

    assert queue.calls == [
        (MR_ACTOR_JOB_NAME, ("team/project!42",)),
        (MR_ACTOR_JOB_NAME, ("team/project!42",)),
    ]

    await drain_mr_actor(
        "team/project!42",
        store=store,
        queue=queue,
        handlers=RuntimeHandlers(
            run_auto_review=lambda event: calls.append(event.event_id),
            run_mention=lambda _event: None,
        ),
        worker_id="worker-2",
    )

    assert calls == ["evt-1"]
    runs = await store.list_runs("team/project!42")
    assert runs[0].state == "succeeded"
    assert runs[1].state == "failed"
    assert runs[0].execution_key == runs[1].execution_key
    assert runs[0].execution_key is not None


@pytest.mark.asyncio
async def test_drain_mr_actor_dead_letters_after_retry_budget(monkeypatch):
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    monkeypatch.setattr(settings, "RUNTIME_MAX_EVENT_ATTEMPTS", 2, raising=False)

    await enqueue_gitlab_event(_auto_event("evt-poison", "head-1"), store=store, queue=queue)

    def failing_review(_event):
        raise RuntimeError("permanent boom")

    handlers = RuntimeHandlers(run_auto_review=failing_review, run_mention=lambda _event: None)

    with pytest.raises(RuntimeError, match="permanent boom"):
        await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)
    with pytest.raises(RuntimeError, match="permanent boom"):
        await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    assert await store.list_actor_statuses() == []
    assert queue.calls.count((MR_ACTOR_JOB_NAME, ("team/project!42",))) == 2
    runs = await store.list_runs("team/project!42")
    assert runs[0].reason == "retry_budget_exhausted"


@pytest.mark.asyncio
async def test_drain_mr_actor_marks_interrupted_prior_run_stale_before_restart():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    event = _auto_event("evt-1", "head-1")

    await enqueue_gitlab_event(event, store=store, queue=queue)

    batch = await store.pop_next_batch("team/project!42")
    assert [item.event_id for item in batch] == ["evt-1"]

    interrupted = runtime_worker._make_run_record("team/project!42", batch)
    await store.write_run(interrupted)
    get_tracking_service().record_run(
        {
            "run_id": interrupted.run_id,
            "execution_key": interrupted.execution_key,
            "actor_key": interrupted.actor_key,
            "project_id": interrupted.project_id,
            "mr_iid": interrupted.mr_iid,
            "event_type": interrupted.event_type,
            "state": "running",
            "reason": None,
            "error": None,
            "head_sha": interrupted.head_sha,
            "note_id": interrupted.note_id,
            "discussion_id": interrupted.discussion_id,
            "batch_size": interrupted.batch_size,
            "started_at": interrupted.started_at,
            "completed_at": None,
        }
    )

    await drain_mr_actor(
        "team/project!42",
        store=store,
        queue=queue,
        handlers=RuntimeHandlers(
            run_auto_review=lambda _event: SimpleNamespace(status="published"),
            run_mention=lambda _event: None,
        ),
        worker_id="worker-2",
    )

    runtime_runs = await store.list_runs("team/project!42")
    tracking_runs = get_tracking_service().list_runs_for_actor("team/project!42", limit=10)

    interrupted_runtime = next(item for item in runtime_runs if item.run_id == interrupted.run_id)
    interrupted_tracking = next(item for item in tracking_runs if item["run_id"] == interrupted.run_id)

    assert interrupted_runtime.state == "stale"
    assert interrupted_runtime.reason == "interrupted_run_restarted"
    assert interrupted_tracking["state"] == "stale"
    assert interrupted_tracking["reason"] == "interrupted_run_restarted"


@pytest.mark.asyncio
async def test_drain_mr_actor_heartbeats_during_long_running_run(monkeypatch):
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls = {"heartbeat": 0}

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    original_heartbeat = store.heartbeat_lease

    async def counting_heartbeat(actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
        calls["heartbeat"] += 1
        return await original_heartbeat(actor_key, worker_id, ttl_seconds)

    async def slow_review(_event):
        import asyncio

        await asyncio.sleep(0.05)

    monkeypatch.setattr(store, "heartbeat_lease", counting_heartbeat)
    monkeypatch.setattr("agent.runtime.worker.settings.RUN_HEARTBEAT_SECONDS", 0.01)

    await drain_mr_actor(
        "team/project!42",
        store=store,
        queue=queue,
        handlers=RuntimeHandlers(run_auto_review=slow_review, run_mention=lambda _event: None),
        worker_id="worker-1",
    )

    assert calls["heartbeat"] >= 2


@pytest.mark.asyncio
async def test_drain_mr_actor_does_not_ack_when_lease_is_lost(monkeypatch):
    class _LeaseLossStore(InMemoryRuntimeStore):
        def __init__(self) -> None:
            super().__init__()
            self.heartbeat_calls = 0

        async def heartbeat_lease(self, actor_key: str, worker_id: str, ttl_seconds: int) -> bool:
            self.heartbeat_calls += 1
            return self.heartbeat_calls == 1

    store = _LeaseLossStore()
    queue = _FakeQueue()
    await enqueue_gitlab_event(_auto_event("evt-lease-loss", "head-1"), store=store, queue=queue)
    monkeypatch.setattr("agent.runtime.worker.settings.RUN_HEARTBEAT_SECONDS", 3600)

    with pytest.raises(RuntimeError, match="lost actor lease"):
        await drain_mr_actor(
            "team/project!42",
            store=store,
            queue=queue,
            handlers=RuntimeHandlers(
                run_auto_review=lambda _event: {"status": "ok"},
                run_mention=lambda _event: None,
            ),
            worker_id="worker-1",
        )

    assert [item.event_id for item in await store.list_actor_events("team/project!42")] == []
    status = (await store.list_actor_statuses())[0]
    assert status.inflight_count == 1
    assert queue.calls[-1] == (MR_ACTOR_JOB_NAME, ("team/project!42",))


@pytest.mark.asyncio
async def test_runtime_store_records_publish_receipts():
    from agent.runtime.models import PublishReceipt

    store = InMemoryRuntimeStore()
    receipt = PublishReceipt(
        actor_key="team/project!42",
        op_key="summary:auto-review:head123",
        channel="mr_note",
        external_id="91",
    )

    await store.record_publish_receipt(receipt)

    stored = await store.get_publish_receipt("team/project!42", "summary:auto-review:head123")

    assert stored is not None
    assert stored.external_id == "91"


@pytest.mark.asyncio
async def test_runtime_store_records_run_journal_events_and_checkpoint():
    store = InMemoryRuntimeStore()
    event = RunJournalEvent(
        execution_key="exec-1",
        run_id="run-1",
        actor_key="team/project!42",
        scene="auto_review",
        workflow_version="1",
        stage_key="intake",
        event_type="stage_started",
        status="running",
        summary="collecting review context",
    )
    checkpoint = RunCheckpoint(
        execution_key="exec-1",
        actor_key="team/project!42",
        scene="auto_review",
        workflow_version="1",
        stage_key="intake",
        artifact_refs={"context": "/tmp/context.json"},
    )

    await store.record_run_journal_event(event)
    await store.write_run_checkpoint(checkpoint)

    journal = await store.list_run_journal("exec-1")
    stored = await store.get_run_checkpoint("exec-1")

    assert [item.event_type for item in journal] == ["stage_started"]
    assert stored is not None
    assert stored.stage_key == "intake"
    assert stored.artifact_refs["context"] == "/tmp/context.json"


@pytest.mark.asyncio
async def test_runtime_store_can_remove_one_pending_event_without_touching_others():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_mention_event("evt-mention-1", 101), store=store, queue=queue)
    await enqueue_gitlab_event(_auto_event("evt-auto-1", "head-1"), store=store, queue=queue)
    await enqueue_gitlab_event(_mention_event("evt-mention-2", 102), store=store, queue=queue)

    removed = await store.remove_pending_event("team/project!42", "evt-auto-1")

    assert removed is True
    remaining = await store.list_actor_events("team/project!42")
    assert [item.event_id for item in remaining] == ["evt-mention-1", "evt-mention-2"]


@pytest.mark.asyncio
async def test_runtime_store_refuses_to_remove_event_that_is_already_inflight():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-auto-1", "head-1"), store=store, queue=queue)

    batch = await store.pop_next_batch("team/project!42")
    removed = await store.remove_pending_event("team/project!42", "evt-auto-1")

    assert [item.event_id for item in batch] == ["evt-auto-1"]
    assert removed is False


@pytest.mark.asyncio
async def test_runtime_store_records_run_termination_requests():
    store = InMemoryRuntimeStore()

    request = await store.request_run_termination(
        "run-terminate-1",
        actor_key="team/project!42",
        requested_by="admin",
    )

    fetched = await store.get_run_termination("run-terminate-1")

    assert request.run_id == "run-terminate-1"
    assert request.actor_key == "team/project!42"
    assert request.requested_by == "admin"
    assert fetched is not None
    assert fetched.run_id == "run-terminate-1"
    assert await store.is_run_termination_requested("run-terminate-1") is True


@pytest.mark.asyncio
async def test_drain_mr_actor_marks_stale_terminal_runs():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: SimpleNamespace(status="skipped", reason="stale_webhook_head_sha"),
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    runs = await store.list_runs("team/project!42")
    assert runs[0].state == "stale"
    assert runs[0].reason == "stale_webhook_head_sha"


@pytest.mark.asyncio
async def test_drain_mr_actor_marks_skipped_terminal_runs():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(
        EventEnvelope(
            event_id="evt-evo-skip-1",
            event_type="agent_self_evolution",
            project_id="team/project",
            payload={"agent_type": "mention"},
        ),
        store=store,
        queue=queue,
    )

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: None,
        run_agent_self_evolution=lambda _event: SimpleNamespace(status="skipped", reason="no_targets_configured"),
    )

    await drain_mr_actor("team/project!self_evolution", store=store, queue=queue, handlers=handlers)

    runs = await store.list_runs("team/project!self_evolution")
    assert runs[0].state == "skipped"
    assert runs[0].reason == "no_targets_configured"

    tracked = get_tracking_service().get_run(runs[0].run_id)
    assert tracked is not None
    assert tracked["state"] == "skipped"
    assert tracked["reason"] == "no_targets_configured"


@pytest.mark.asyncio
async def test_drain_mr_actor_marks_terminated_terminal_runs():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: SimpleNamespace(status="terminated", reason="user_terminated"),
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    runs = await store.list_runs("team/project!42")
    assert runs[0].state == "terminated"
    assert runs[0].reason == "user_terminated"


@pytest.mark.asyncio
async def test_drain_mr_actor_treats_termination_exception_as_terminal_and_continues_queue():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    calls: list[tuple[str, str | int]] = []

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)
    await enqueue_gitlab_event(_mention_event("evt-2", 202), store=store, queue=queue)

    def terminate_auto_review(event):
        calls.append(("auto_review", event.head_sha or ""))
        raise runtime_worker.RunTerminationRequested(
            run_id=str(event.payload["_runtime"]["run_id"]),
            actor_key=event.actor_key,
            reason="user_terminated",
        )

    handlers = RuntimeHandlers(
        run_auto_review=terminate_auto_review,
        run_mention=lambda event: calls.append(("mention", event.note_id or 0)),
    )

    drained = await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    assert drained is True
    assert calls == [
        ("auto_review", "head-1"),
        ("mention", 202),
    ]
    runs = await store.list_runs("team/project!42")
    assert [(item.event_type, item.state, item.reason) for item in runs] == [
        ("mention", "succeeded", None),
        ("auto_review", "terminated", "user_terminated"),
    ]


@pytest.mark.asyncio
async def test_drain_mr_actor_marks_failed_terminal_runs():
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: SimpleNamespace(status="failed", reason="gitlab_bot_identity_mismatch"),
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    runs = await store.list_runs("team/project!42")
    assert runs[0].state == "failed"


@pytest.mark.asyncio
async def test_sqlite_runtime_store_persists_pending_cancellation_and_run_termination(tmp_path):
    db_path = str(tmp_path / "runtime.db")
    store = await SQLiteRuntimeStore.from_path(db_path)

    await store.append_event(_auto_event("evt-1", "head-1"))
    await store.append_event(_mention_event("evt-2", 102))

    removed = await store.remove_pending_event("team/project!42", "evt-1")
    request = await store.request_run_termination(
        "run-terminate-sqlite",
        actor_key="team/project!42",
        requested_by="admin",
    )

    second_store = await SQLiteRuntimeStore.from_path(db_path)
    events = await second_store.list_actor_events("team/project!42")
    fetched = await second_store.get_run_termination("run-terminate-sqlite")

    assert removed is True
    assert [item.event_id for item in events] == ["evt-2"]
    assert request.run_id == "run-terminate-sqlite"
    assert fetched is not None
    assert fetched.requested_by == "admin"


@pytest.mark.asyncio
async def test_sqlite_runtime_store_persists_events_and_receipts(tmp_path):
    db_path = str(tmp_path / "runtime.db")
    store = await SQLiteRuntimeStore.from_path(db_path)

    appended = await store.append_event(_auto_event("evt-1", "head-1"))
    await store.record_publish_receipt(
        PublishReceipt(
            actor_key="team/project!42",
            op_key="summary:auto-review:head-1",
            channel="mr_note",
            external_id="91",
        )
    )
    second_store = await SQLiteRuntimeStore.from_path(db_path)

    events = await second_store.list_actor_events("team/project!42")
    receipt = await second_store.get_publish_receipt("team/project!42", "summary:auto-review:head-1")

    assert appended is True
    assert [item.event_id for item in events] == ["evt-1"]
    assert receipt is not None
    assert receipt.external_id == "91"


@pytest.mark.asyncio
async def test_resume_runtime_processing_enqueues_pending_sqlite_actors(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    store = await SQLiteRuntimeStore.from_path(str(tmp_path / "controlplane.db"))
    queue = _FakeQueue()

    await store.append_event(_auto_event("evt-1", "head-1"))
    await store.mark_actor_scheduled("team/project!42")

    resumed = await resume_runtime_processing(store=store, queue=queue)

    assert resumed == 1
    assert queue.calls == [(MR_ACTOR_JOB_NAME, ("team/project!42",))]


@pytest.mark.asyncio
async def test_maybe_enqueue_daily_audit_events_only_schedules_once_per_day(monkeypatch):
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    from agent.controlplane import get_config_service

    service = get_config_service()
    service.set_values({"GITLAB_TARGET_PROJECTS": ["team/project"]}, actor="test-suite")
    service.set_project_agent_config(
        "team/project",
        {"DAILY_AUDIT_ENABLED": "1", "DAILY_AUDIT_START_TIME_LOCAL": "02:00"},
        actor="test-suite",
    )
    monkeypatch.setattr("agent.runtime.worker.get_project_default_branch", lambda _project_id: "main")
    beijing = ZoneInfo("Asia/Shanghai")

    first = await runtime_worker.maybe_enqueue_daily_audit_events(
        now=datetime(2026, 4, 13, 2, 5, tzinfo=beijing),
        store=store,
        queue=queue,
    )
    second = await runtime_worker.maybe_enqueue_daily_audit_events(
        now=datetime(2026, 4, 13, 8, 0, tzinfo=beijing),
        store=store,
        queue=queue,
    )
    third = await runtime_worker.maybe_enqueue_daily_audit_events(
        now=datetime(2026, 4, 14, 2, 5, tzinfo=beijing),
        store=store,
        queue=queue,
    )

    assert first == 1
    assert second == 0
    assert third == 1
    assert queue.calls == [
        (MR_ACTOR_JOB_NAME, ("team/project!daily_audit",)),
    ]
    assert [item.event_id for item in await store.list_actor_events("team/project!daily_audit")] == [
        "daily_audit:team/project:2026-04-13",
        "daily_audit:team/project:2026-04-14",
    ]


@pytest.mark.asyncio
async def test_maybe_enqueue_agent_self_evolution_events_respects_global_interval(monkeypatch):
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()
    from agent.controlplane import get_config_service

    get_config_service().set_values({"GITLAB_TARGET_PROJECTS": ["team/project"]}, actor="test-suite")
    monkeypatch.setattr(settings, "SELF_EVOLUTION_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVOLUTION_INTERVAL_DAYS", 2)
    monkeypatch.setattr(settings, "SELF_EVOLUTION_TIME_LOCAL", "02:00")
    monkeypatch.setattr("agent.runtime.worker.get_project_default_branch", lambda _project_id: "main")
    beijing = ZoneInfo("Asia/Shanghai")

    first = await runtime_worker.maybe_enqueue_agent_self_evolution_events(
        now=datetime(2026, 4, 13, 2, 5, tzinfo=beijing),
        store=store,
        queue=queue,
    )
    second = await runtime_worker.maybe_enqueue_agent_self_evolution_events(
        now=datetime(2026, 4, 14, 2, 5, tzinfo=beijing),
        store=store,
        queue=queue,
    )
    third = await runtime_worker.maybe_enqueue_agent_self_evolution_events(
        now=datetime(2026, 4, 15, 2, 5, tzinfo=beijing),
        store=store,
        queue=queue,
    )

    assert first == 1
    assert second == 0
    assert third == 1
    assert queue.calls == [
        (MR_ACTOR_JOB_NAME, ("team/project!self_evolution",)),
    ]
    assert [item.event_id for item in await store.list_actor_events("team/project!self_evolution")] == [
        "agent_self_evolution:team/project:2026-04-13",
        "agent_self_evolution:team/project:2026-04-15",
    ]
