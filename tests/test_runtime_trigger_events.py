from __future__ import annotations

import pytest

from agent.config import settings
from agent.controlplane import reset_controlplane_services
from agent.controlplane import get_tracking_service
from agent.runtime.models import EventEnvelope
from agent.runtime.store import InMemoryRuntimeStore
from agent.runtime.worker import RuntimeHandlers, drain_mr_actor


@pytest.fixture(autouse=True)
def _reset_tracking(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    yield
    reset_controlplane_services()


@pytest.mark.asyncio
async def test_drain_mr_actor_persists_trigger_event_identifiers():
    from agent.runtime.queue import enqueue_gitlab_event

    store = InMemoryRuntimeStore()
    event = EventEnvelope(
        event_id="evt-1",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        target_branch="main",
        head_sha="head-123",
        title="Fix router regression",
        payload={"kind": "merge_request", "action": "update"},
    )
    await enqueue_gitlab_event(event, store=store, queue=None)

    handlers = RuntimeHandlers(
        run_auto_review=lambda _event: None,
        run_mention=lambda _event: None,
    )

    await drain_mr_actor("team/project!42", store=store, handlers=handlers)

    tracked = get_tracking_service().list_recent_runs(limit=1)
    assert len(tracked) == 1
    trigger_events = tracked[0]["trigger_events"]
    assert trigger_events == [
        {
            "event_id": "evt-1",
            "event_type": "auto_review",
            "project_id": "team/project",
            "mr_iid": 42,
            "source_branch": "feature/router",
            "target_branch": "main",
            "head_sha": "head-123",
            "note_id": None,
            "discussion_id": None,
            "payload": {"kind": "merge_request", "action": "update"},
        }
    ]
