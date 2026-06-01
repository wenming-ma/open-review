"""Tests for daily audit deepagents runtime helpers."""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from agent.config import settings
from agent.scenes.daily_audit.models import AuditUnit, DailyAuditAgentResponse, DailyAuditSelectionResponse
from agent.scenes.daily_audit.runtime.deepagents import (
    SQLiteCheckpointSaver,
    SQLiteStore,
    archive_daily_audit_run_transcript,
    daily_audit_session_id,
)


def test_sqlite_checkpoint_saver_round_trips_checkpoints(tmp_path):
    db_path = tmp_path / "controlplane.db"
    saver = SQLiteCheckpointSaver(db_path)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [{"role": "user", "content": "hello"}]}
    checkpoint["channel_versions"] = {"messages": "0001"}
    checkpoint["versions_seen"] = {"agent": {"messages": "0001"}}
    config = {"configurable": {"thread_id": "daily_audit:team/project:primary", "checkpoint_ns": ""}}

    saved = saver.put(
        config,
        checkpoint,
        {"source": "input", "step": -1},
        {"messages": "0001"},
    )

    reloaded = SQLiteCheckpointSaver(db_path)
    restored = reloaded.get_tuple(saved)

    assert restored is not None
    assert restored.checkpoint["channel_values"]["messages"][0]["content"] == "hello"


def test_sqlite_checkpoint_saver_allows_daily_audit_models_without_warning(tmp_path, caplog):
    db_path = tmp_path / "controlplane.db"
    selected_unit = AuditUnit(unit_type="function", label="foo", file_path="src/foo.py")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            DailyAuditSelectionResponse(selected_unit=selected_unit),
            DailyAuditAgentResponse(selected_unit=selected_unit, summary_markdown="ok"),
        ]
    }
    checkpoint["channel_versions"] = {"messages": "0001"}
    checkpoint["versions_seen"] = {"agent": {"messages": "0001"}}
    config = {"configurable": {"thread_id": "daily_audit:team/project:primary", "checkpoint_ns": ""}}

    saver = SQLiteCheckpointSaver(db_path)
    saved = saver.put(config, checkpoint, {"source": "input", "step": -1}, {"messages": "0001"})

    with caplog.at_level(logging.WARNING):
        restored = SQLiteCheckpointSaver(db_path).get_tuple(saved)

    assert restored is not None
    assert isinstance(restored.checkpoint["channel_values"]["messages"][0], DailyAuditSelectionResponse)
    assert "Deserializing unregistered type agent.scenes.daily_audit.models" not in caplog.text


def test_sqlite_store_persists_content(tmp_path):
    db_path = tmp_path / "controlplane.db"
    store = SQLiteStore(db_path)

    store.put(("daily_audit", "team__project"), "recall", {"text": "foo hot path"})
    item = store.get(("daily_audit", "team__project"), "recall")
    results = store.search(("daily_audit",), query="foo", limit=5)

    assert item is not None
    assert item.value["text"] == "foo hot path"
    assert results[0].value["text"] == "foo hot path"


def test_archive_daily_audit_run_transcript_persists_run_scoped_history(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    saver = SQLiteCheckpointSaver(db_path)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="Investigated foo() in src/foo.cpp."),
            AIMessage(content="Found a redundant lookup in the hot path."),
            ToolMessage(content="read_file src/foo.cpp", tool_call_id="call-1", name="read_file"),
        ]
    }
    checkpoint["channel_versions"] = {"messages": "0001"}
    checkpoint["versions_seen"] = {"agent": {"messages": "0001"}}
    saver.put(
        {"configurable": {"thread_id": daily_audit_session_id("team/project", "run-1"), "checkpoint_ns": ""}},
        checkpoint,
        {"source": "input", "step": -1},
        {"messages": "0001"},
    )

    archived = archive_daily_audit_run_transcript(
        project_id="team/project",
        runtime_run_id="runtime-1",
        run_id="run-1",
        unit_label="foo()",
        file_path="src/foo.cpp",
        result_json={"summary_markdown": "Found a redundant lookup."},
    )
    run = tracking.list_recent_runs(limit=1)[0]
    records = run["agent_records"]

    assert archived is True
    assert len(records) == 1
    assert records[0]["record_kind"] == "daily_audit.analysis"
    assert records[0]["thread_id"] == "daily_audit:team/project:run-1:primary"
    assert records[0]["metadata_json"]["logical_run_id"] == "run-1"
    assert records[0]["metadata_json"]["unit_label"] == "foo()"
    assert records[0]["messages_json"]


def test_archive_daily_audit_run_transcript_reads_checkpoint_messages_when_history_file_missing(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    saver = SQLiteCheckpointSaver(db_path)
    thread_id = daily_audit_session_id("team/project", "run-1")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            {"role": "user", "content": "Investigated foo() in src/foo.cpp."},
            {"role": "assistant", "content": "Found a redundant lookup in the hot path."},
            {"role": "tool", "content": "read_file src/foo.cpp"},
        ]
    }
    checkpoint["channel_versions"] = {"messages": "0001"}
    checkpoint["versions_seen"] = {"agent": {"messages": "0001"}}
    saver.put(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        checkpoint,
        {"source": "input", "step": -1},
        {"messages": "0001"},
    )

    archived = archive_daily_audit_run_transcript(
        project_id="team/project",
        runtime_run_id="runtime-1",
        run_id="run-1",
        unit_label="foo()",
        file_path="src/foo.cpp",
        result_json={"summary_markdown": "Found a redundant lookup."},
    )
    run = tracking.list_recent_runs(limit=1)[0]
    records = run["agent_records"]

    assert archived is True
    assert len(records) == 1
    assert records[0]["messages_json"][0]["role"] == "user"
    assert records[0]["messages_json"][1]["role"] == "assistant"


def test_archive_daily_audit_run_transcript_does_not_fallback_to_legacy_thread_id(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    from agent.controlplane import get_tracking_service, reset_controlplane_services

    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    saver = SQLiteCheckpointSaver(db_path)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            {"role": "user", "content": "Investigated foo() in src/foo.cpp."},
            {"role": "assistant", "content": "Found a redundant lookup in the hot path."},
        ]
    }
    checkpoint["channel_versions"] = {"messages": "0001"}
    checkpoint["versions_seen"] = {"agent": {"messages": "0001"}}
    saver.put(
        {"configurable": {"thread_id": "daily_audit:team/project:primary", "checkpoint_ns": ""}},
        checkpoint,
        {"source": "input", "step": -1},
        {"messages": "0001"},
    )

    archived = archive_daily_audit_run_transcript(
        project_id="team/project",
        runtime_run_id="runtime-1",
        run_id="run-1",
        unit_label="foo()",
        file_path="src/foo.cpp",
        result_json={"summary_markdown": "Found a redundant lookup."},
    )
    run = tracking.list_recent_runs(limit=1)[0]

    assert archived is False
    assert run["agent_records"] == []
