"""Tests for daily audit memory layers."""

from __future__ import annotations

from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.config import settings
from agent.scenes.daily_audit.orchestrator import build_daily_audit_context
from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore
from agent.scenes.daily_audit.selfevolution.tools import build_exploration_memory_tool


def test_daily_audit_memory_store_persists_short_term_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    store = DailyAuditPersistenceStore(str(db_path))

    store.upsert_short_term_summary("team/project", "primary", "continue reviewing the parser hotspot")

    assert store.get_short_term_summary("team/project", "primary") == "continue reviewing the parser hotspot"


def test_daily_audit_memory_store_uses_only_four_business_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    store = DailyAuditPersistenceStore(str(db_path))
    del store

    import sqlite3

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    assert "daily_audit_short_term" in tables
    assert "daily_audit_long_term" in tables
    assert "daily_audit_direction_archives" in tables
    assert "daily_audit_run_transcripts" not in tables
    assert "daily_audit_run_transcript_chunks" not in tables
    assert "daily_audit_history" not in tables
    assert "daily_audit_recall_documents" not in tables
    assert "daily_audit_precompact_insights" not in tables
    assert "daily_audit_builtin_memory" not in tables
    assert "daily_audit_runtime_skills" not in tables
    assert "daily_audit_evolution_lineage" not in tables


def test_daily_audit_memory_store_records_long_term_memory_and_searchable_transcripts(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    store = DailyAuditPersistenceStore(str(db_path))

    store.add_long_term_memory(
        "team/project",
        memory_type="successful_pattern",
        content="Prefer bounded loop-local optimizations over cross-module rewrites.",
        source_run_id="run-1",
    )
    tracking.append_agent_record(
        "runtime-1",
        {
            "record_kind": "daily_audit.analysis",
            "thread_id": "daily_audit:team/project:run-1:primary",
            "system_prompt": "analysis prompt",
            "input_messages_json": [{"role": "user", "content": "Investigate foo()"}],
            "messages_json": [
                {"role": "user", "content": "Investigated foo() in src/foo.cpp."},
                {"role": "assistant", "content": "Found a redundant lookup inside foo() loop and reported it."},
            ],
            "result_json": {"summary_markdown": "Found a redundant lookup."},
            "started_at": "2026-04-20T10:00:00+08:00",
            "completed_at": "2026-04-20T10:05:00+08:00",
            "metadata_json": {
                "logical_run_id": "run-1",
                "unit_label": "foo()",
                "file_path": "src/foo.cpp",
            },
        },
    )

    memories = store.list_long_term_memory("team/project")
    hits = store.search_run_transcripts("team/project", "redundant lookup")

    assert memories[0]["content"] == "Prefer bounded loop-local optimizations over cross-module rewrites."
    assert hits[0]["run_id"] == "run-1"


def test_daily_audit_memory_store_reads_raw_analysis_records_from_tracked_runs(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-1",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    tracking.append_agent_record(
        "runtime-1",
        {
            "record_kind": "daily_audit.analysis",
            "thread_id": "daily_audit:team/project:run-1:primary",
            "system_prompt": "analysis prompt",
            "input_messages_json": [{"role": "user", "content": "analyze foo"}],
            "messages_json": [
                {"role": "user", "content": "Investigated foo() in src/foo.cpp."},
                {"role": "assistant", "content": "Found a redundant lookup in the hot path."},
            ],
            "result_json": {"summary_markdown": "Found a redundant lookup."},
            "started_at": "2026-04-20T10:00:00+08:00",
            "completed_at": "2026-04-20T10:05:00+08:00",
            "metadata_json": {
                "logical_run_id": "run-1",
                "unit_label": "foo()",
                "file_path": "src/foo.cpp",
            },
        },
    )
    store = DailyAuditPersistenceStore(str(db_path))

    transcript = store.get_run_transcript("team/project", "run-1")
    hits = store.search_run_transcripts("team/project", "redundant lookup")

    assert transcript is not None
    assert transcript["run_id"] == "run-1"
    assert transcript["thread_id"] == "daily_audit:team/project:run-1:primary"
    assert "redundant lookup" in transcript["content"]
    assert hits[0]["run_id"] == "run-1"
    assert hits[0]["unit_label"] == "foo()"


def test_daily_audit_memory_store_records_and_searches_direction_archives(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    store = DailyAuditPersistenceStore(str(db_path))

    store.record_direction_archive(
        "team/project",
        run_id="run-1",
        unit_type="action_workflow",
        unit_label="Refresh All Orders",
        file_path="services/orders/refresh_jobs.py",
        entrypoint_kind="toolbar_action",
        entrypoint_symbol="OrderActions.refreshAll",
        workflow_summary="Refresh all orders from the toolbar action and trace refresh scheduling.",
        selection_reasoning="This is user-facing, bounded, and has a known performance profile.",
        direction_brief="Refresh All Orders toolbar workflow touching refresh scheduling and order cache recomputation.",
        keywords=["orders", "refresh", "toolbar", "cache", "scheduler"],
        metadata={"entry_evidence": ["toolbar appends OrderActions.refreshAll"]},
    )
    store.record_direction_archive(
        "team/project",
        run_id="run-2",
        unit_type="action_workflow",
        unit_label="Dashboard Viewer",
        file_path="services/dashboard/view_actions.py",
        entrypoint_kind="menu_action",
        entrypoint_symbol="DashboardActions.showPreview",
        workflow_summary="Open the 3D viewer from the menu and trace scene initialization.",
        selection_reasoning="Another bounded user entrypoint.",
        direction_brief="Dashboard Viewer menu workflow touching scene bootstrap.",
        keywords=["viewer", "3d", "menu"],
        metadata={},
    )

    recent = store.list_recent_direction_archives("team/project", limit=5)
    matches = store.search_direction_archives("team/project", "toolbar refresh", limit=5)

    assert recent[0]["run_id"] == "run-2"
    assert matches[0]["run_id"] == "run-1"
    assert "Refresh All Orders toolbar workflow" in matches[0]["direction_brief"]


def test_build_daily_audit_context_no_longer_embeds_memory_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    store = DailyAuditPersistenceStore(str(db_path))
    store.upsert_short_term_summary("team/project", "primary", "resume the parser review thread")
    store.add_long_term_memory(
        "team/project",
        memory_type="project_fact",
        content="The parser module is latency-sensitive and widely reused.",
        source_run_id="run-1",
    )
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "parser.cpp").write_text("int parse() { for (int i = 0; i < 10; ++i) {} return 0; }\n")

    context = build_daily_audit_context(
        project_id="team/project",
        repo_dir=str(repo_dir),
        default_branch="main",
        event=type("Event", (), {"event_id": "evt-memory"})(),
    )

    assert not hasattr(context, "memory")


def test_exploration_memory_tool_returns_short_term_and_matching_long_term(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    store = DailyAuditPersistenceStore(str(db_path))

    store.upsert_short_term_summary("team/project", "primary", "Resume the parser hotspot audit.")
    store.add_long_term_memory(
        "team/project",
        memory_type="project_fact",
        content="foo() is latency-sensitive and prior broad refactors regressed parser startup.",
        source_run_id="run-1",
    )
    tool = build_exploration_memory_tool(project_id="team/project", store=store)

    result = tool(query="parser startup", limit=5)

    assert result["success"] is True
    assert result["short_term_summary"] == "Resume the parser hotspot audit."
    assert result["count"] == 1
    assert "regressed parser startup" in result["results"][0]["content"]
