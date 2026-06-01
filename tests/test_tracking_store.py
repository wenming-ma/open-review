"""Tests for persistent run tracking in the control-plane store."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.runtime.models import EventEnvelope
from agent.runtime.queue import enqueue_gitlab_event
from agent.runtime.store import InMemoryRuntimeStore
from agent.runtime.worker import RuntimeHandlers, drain_mr_actor


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


def _daily_event(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type="daily_audit",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit run",
        payload={"kind": "daily_audit", "default_branch": "main"},
    )


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def enqueue_job(self, job_name: str, *args):
        self.calls.append((job_name, args))


@pytest.mark.asyncio
async def test_runtime_worker_persists_tracked_run_history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-1", "head-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: SimpleNamespace(
            status="published",
            review_run_id="review-1",
            review_mode="full",
            compressed_review=False,
            confirmed_findings_count=2,
            suspicious_findings_count=1,
            open_questions_count=1,
            inline_comments_count=1,
        ),
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    runs = get_tracking_service().list_recent_runs(limit=5)

    assert len(runs) == 1
    assert runs[0]["actor_key"] == "team/project!42"
    assert runs[0]["state"] == "succeeded"
    assert runs[0]["event_type"] == "auto_review"
    assert runs[0]["confirmed_findings_count"] == 2
    assert "failed_lanes" not in runs[0]


@pytest.mark.asyncio
async def test_runtime_worker_persists_trace_identifiers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_auto_event("evt-2", "head-2"), store=store, queue=queue)

    @contextmanager
    def fake_span(*_args, **_kwargs):
        yield SimpleNamespace(
            trace_id="trace-123",
            trace_url="http://phoenix.local/redirects/traces/trace-123",
            session_id="team/project!42",
            set_input=lambda *_args, **_kwargs: None,
            set_output=lambda *_args, **_kwargs: None,
        )

    monkeypatch.setattr("agent.runtime.worker.start_open_review_span", fake_span)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: SimpleNamespace(status="published"),
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, queue=queue, handlers=handlers)

    runs = get_tracking_service().list_recent_runs(limit=5)

    assert len(runs) == 1
    assert runs[0]["trace_id"] == "trace-123"
    assert runs[0]["trace_url"] == "http://phoenix.local/redirects/traces/trace-123"
    assert runs[0]["session_id"] == "team/project!42"


@pytest.mark.asyncio
async def test_runtime_worker_persists_project_level_daily_audit_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_daily_event("evt-daily-1"), store=store, queue=queue)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=lambda _event: SimpleNamespace(
            status="reported",
            unit_type="function",
            unit_label="foo()",
            finding_count=2,
        ),
    )

    await drain_mr_actor("team/project!daily_audit", store=store, queue=queue, handlers=handlers)

    runs = get_tracking_service().list_recent_runs(limit=5)

    assert len(runs) == 1
    assert runs[0]["actor_key"] == "team/project!daily_audit"
    assert runs[0]["event_type"] == "daily_audit"
    assert runs[0]["mr_iid"] is None


@pytest.mark.asyncio
async def test_runtime_worker_seeds_tracked_run_before_daily_audit_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    store = InMemoryRuntimeStore()
    queue = _FakeQueue()

    await enqueue_gitlab_event(_daily_event("evt-daily-2"), store=store, queue=queue)

    def run_daily_audit(event):
        runtime = event.payload["_runtime"]
        run_id = runtime["run_id"]
        tracking = get_tracking_service()
        current = tracking.get_run(run_id)
        assert current is not None
        assert current["state"] == "running"
        tracking.append_agent_record(
            run_id,
            {
                "record_kind": "daily_audit.direction",
                "thread_id": "daily_audit:team/project:run-1:direction",
            },
        )
        return SimpleNamespace(status="reported")

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
        run_daily_audit=run_daily_audit,
    )

    await drain_mr_actor("team/project!daily_audit", store=store, queue=queue, handlers=handlers)

    runs = get_tracking_service().list_recent_runs(limit=5)

    assert len(runs) == 1
    assert runs[0]["state"] == "succeeded"
    assert runs[0]["agent_records"] == [
        {
            "record_kind": "daily_audit.direction",
            "thread_id": "daily_audit:team/project:run-1:direction",
        }
    ]


def test_tracking_service_migrates_tracked_runs_with_raw_record_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tracked_runs (
                run_id TEXT PRIMARY KEY,
                execution_key TEXT,
                actor_key TEXT NOT NULL,
                project_id TEXT NOT NULL,
                mr_iid INTEGER,
                event_type TEXT NOT NULL,
                state TEXT NOT NULL,
                reason TEXT,
                error TEXT,
                head_sha TEXT,
                note_id INTEGER,
                discussion_id TEXT,
                batch_size INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                review_run_id TEXT,
                review_mode TEXT,
                compressed_review INTEGER NOT NULL DEFAULT 0,
                published_findings_count INTEGER NOT NULL DEFAULT 0,
                suppressed_findings_count INTEGER NOT NULL DEFAULT 0,
                confirmed_findings_count INTEGER NOT NULL DEFAULT 0,
                suspicious_findings_count INTEGER NOT NULL DEFAULT 0,
                open_questions_count INTEGER NOT NULL DEFAULT 0,
                inline_comments_count INTEGER NOT NULL DEFAULT 0,
                failed_lanes_json TEXT NOT NULL DEFAULT '[]',
                mention_intent TEXT,
                mention_status TEXT,
                mention_degraded_reason TEXT,
                changed_files_count INTEGER NOT NULL DEFAULT 0,
                commit_sha TEXT,
                covered_note_ids_json TEXT NOT NULL DEFAULT '[]',
                trace_id TEXT,
                trace_url TEXT,
                session_id TEXT
            );
            """
        )

    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_tracking_service()

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tracked_runs)").fetchall()
        }

    assert "trigger_events_json" in columns
    assert "agent_records_json" in columns
    assert "published_objects_json" in columns
    assert "feedback_events_json" in columns
    assert "related_run_ids_json" in columns
    assert "published_issue_iid" in columns
    assert "published_merge_request_iid" in columns
    assert "failed_lanes_json" not in columns
    assert service.list_recent_runs(limit=5) == []


def test_tracking_service_append_helpers_accumulate_raw_record_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))

    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_tracking_service()
    service.record_run(
        {
            "run_id": "run-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )

    service.append_trigger_event("run-1", {"event_id": "evt-1", "kind": "daily_audit"})
    service.append_agent_record("run-1", {"record_kind": "daily_audit.direction", "thread_id": "direction"})
    service.append_agent_record("run-1", {"record_kind": "daily_audit.analysis", "thread_id": "analysis"})
    service.append_published_object("run-1", {"object_kind": "issue", "issue_iid": 17})
    service.append_feedback_event("run-1", {"feedback_kind": "note", "note_id": 99})
    service.append_related_run_id("run-1", "run-0")
    service.set_published_issue_iid("run-1", 17)
    service.set_published_merge_request_iid("run-1", 23)

    service.record_run(
        {
            "run_id": "run-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
            "completed_at": "2026-04-20T10:05:00+08:00",
        }
    )

    run = service.list_recent_runs(limit=1)[0]

    assert run["state"] == "succeeded"
    assert run["trigger_events"] == [{"event_id": "evt-1", "kind": "daily_audit"}]
    assert run["agent_records"] == [
        {"record_kind": "daily_audit.direction", "thread_id": "direction"},
        {"record_kind": "daily_audit.analysis", "thread_id": "analysis"},
    ]
    assert run["published_objects"] == [{"object_kind": "issue", "issue_iid": 17}]
    assert run["feedback_events"] == [{"feedback_kind": "note", "note_id": 99}]
    assert run["related_run_ids"] == ["run-0"]
    assert run["published_issue_iid"] == 17
    assert run["published_merge_request_iid"] == 23
