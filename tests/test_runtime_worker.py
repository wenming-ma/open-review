"""Tests for runtime worker integration behavior."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.runtime import worker
from agent.runtime.models import EventEnvelope, RunCheckpoint
from agent.runtime.queue import get_runtime_store, reset_runtime_clients
from agent.utils.thread_id import generate_thread_id


@pytest.mark.asyncio
async def test_runtime_auto_review_event_uses_temporary_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-1",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-123",
    )
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")
    called = {}

    async def fake_setup_sandbox(*_args, **_kwargs):
        return sandbox, "/tmp/sandbox/repo"

    async def fake_run_auto_review(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="published")

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
    )
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.sandbox.manager.create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/review-evt-1")
    monkeypatch.setattr("agent.sandbox.manager.cleanup_temporary_worktree", lambda *_args, **_kwargs: called.setdefault("cleanup", True))
    monkeypatch.setattr("agent.scenes.auto_review.orchestrator.run_auto_review", fake_run_auto_review)

    result = await worker._run_auto_review_event(event)

    assert result.status == "published"
    assert called["run"]["repo_dir"] == "/tmp/sandbox/worktrees/review-evt-1"
    assert called["run"]["expected_head_sha"] == "head-123"
    assert called["cleanup"] is True


def test_event_trace_input_summary_formats_auto_review_event():
    event = EventEnvelope(
        event_id="evt-1",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        target_branch="main",
        title="Fix router regression",
        head_sha="head-123",
    )
    summary = worker._event_trace_input_summary(event)

    assert "## Auto Review Request" in summary
    assert "`team/project!42`" in summary
    assert "Fix router regression" in summary
    assert "`head-123`" in summary


def test_event_trace_output_summary_formats_auto_review_result():
    result = SimpleNamespace(
        status="published",
        review_run_id="review-1",
        recommendation="建议重新修改",
        confirmed_findings_count=2,
        suspicious_findings_count=1,
        open_questions_count=3,
        inline_comments_count=4,
        reason=None,
        degraded_reason=None,
    )

    summary = worker._event_trace_output_summary("auto_review", result)

    assert "## Auto Review Result" in summary
    assert "`published`" in summary
    assert "`review-1`" in summary
    assert "Recommendation: `建议重新修改`" in summary
    assert "Confirmed Findings: `2`" in summary
    assert "Failed Lanes" not in summary
    assert "Reason:" not in summary


def test_event_trace_name_formats_auto_review_runtime_span():
    event = EventEnvelope(
        event_id="evt-1",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        head_sha="head1234567890",
    )
    run_record = worker.RunRecord(
        run_id="auto_review:mr:team/project:42:open:head1234567890:runabcd12",
        execution_key="auto_review:deadbeefcafefeed",
        actor_key="team/project!42",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
    )

    assert (
        worker._event_trace_name(event, run_record)
        == "auto_review team/project!42 @head1234 [deadbeef]"
    )


@pytest.mark.asyncio
async def test_runtime_mention_event_passes_batched_events(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-2",
        event_type="mention",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-123",
        note_id=12,
        discussion_id="disc-1",
        note_body="please fix this",
        note_author="developer",
        payload={
            "batched_events": [
                {
                    "event_id": "evt-1",
                    "note_id": 11,
                    "discussion_id": "disc-1",
                    "note_body": "please explain this",
                    "note_author": "developer",
                    "head_sha": "head-123",
                },
                {
                    "event_id": "evt-2",
                    "note_id": 12,
                    "discussion_id": "disc-1",
                    "note_body": "please fix this",
                    "note_author": "developer",
                    "head_sha": "head-123",
                },
            ]
        },
    )
    called = {}

    async def fake_setup_sandbox(*_args, **_kwargs):
        return SimpleNamespace(root_dir="/tmp/sandbox"), "/tmp/sandbox/repo"

    async def fake_run_mention(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="replied")

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
    )
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.scenes.mention.orchestrator.run_mention", fake_run_mention)

    result = await worker._run_mention_event(event)

    assert result.status == "replied"
    assert [item["note_id"] for item in called["run"]["batched_events"]] == [11, 12]


@pytest.mark.asyncio
async def test_runtime_daily_audit_event_prepares_project_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-daily",
        event_type="daily_audit",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit",
    )
    called = {}

    async def fake_setup_sandbox(*_args, **_kwargs):
        return SimpleNamespace(root_dir="/tmp/sandbox"), "/tmp/sandbox/repo"

    async def fake_run_daily_audit(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="reported", unit_type="function", unit_label="foo()", finding_count=1)

    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.scenes.daily_audit.orchestrator.run_daily_audit", fake_run_daily_audit)

    result = await worker._run_daily_audit_event(event)

    assert result.status == "reported"
    assert called["run"]["default_branch"] == "main"
    assert called["run"]["repo_dir"] == "/tmp/sandbox/repo"


@pytest.mark.asyncio
async def test_runtime_agent_self_evolution_event_dispatches_registered_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-agent-evo",
        event_type="agent_self_evolution",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Agent self evolution",
        payload={"agent_type": "daily_audit"},
    )
    called = {}

    async def fake_run_agent_self_evolution(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="reported", output_count=2)

    monkeypatch.setattr(
        "agent.selfevolution.runtime.run_agent_self_evolution_cycle",
        fake_run_agent_self_evolution,
    )

    result = await worker._run_agent_self_evolution_event(event)

    assert result.status == "reported"
    assert called["run"]["project_id"] == "team/project"
    assert called["run"]["default_branch"] == "main"
    assert called["run"]["agent_type"] == "daily_audit"


@pytest.mark.asyncio
async def test_runtime_sandbox_cleanup_event_removes_mr_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="mr:team/project:42:close:cleanup",
        event_type="sandbox_cleanup",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        target_branch="main",
        title="Fix router regression",
    )
    called = {}

    monkeypatch.setattr(
        "agent.sandbox.manager.cleanup_sandbox",
        lambda thread_id: called.setdefault("thread_id", thread_id),
    )

    result = await worker._run_sandbox_cleanup_event(event)

    assert result.status == "cleaned"
    assert result.reason == "sandbox_cleanup_completed"
    assert called["thread_id"] == generate_thread_id("team/project", 42)


@pytest.mark.asyncio
async def test_runtime_agent_self_evolution_event_runs_sync_cycle_off_main_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-agent-evo-sync",
        event_type="agent_self_evolution",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Agent self evolution",
        payload={"agent_type": "mention"},
    )
    main_thread = threading.get_ident()
    called = {}

    def fake_run_agent_self_evolution(**kwargs):
        called["run"] = kwargs
        called["thread_id"] = threading.get_ident()
        return SimpleNamespace(status="reported", output_count=1)

    monkeypatch.setattr(
        "agent.selfevolution.runtime.run_agent_self_evolution_cycle",
        fake_run_agent_self_evolution,
    )

    result = await worker._run_agent_self_evolution_event(event)

    assert result.status == "reported"
    assert called["run"]["agent_type"] == "mention"
    assert called["thread_id"] != main_thread


@pytest.mark.asyncio
async def test_runtime_agent_self_evolution_event_persists_summary_and_asset_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    reset_runtime_clients()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "agent-self-evo-run-1",
            "execution_key": "agent_self_evolution:test",
            "actor_key": "team/project!self_evolution:mention",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "agent_self_evolution",
            "state": "running",
            "started_at": "2026-04-21T10:00:00+08:00",
        }
    )
    event = EventEnvelope(
        event_id="evt-agent-evo-record",
        event_type="agent_self_evolution",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Agent self evolution",
        payload={
            "agent_type": "mention",
            "_runtime": {
                "run_id": "agent-self-evo-run-1",
                "execution_key": "agent_self_evolution:test",
                "batch_event_ids": ["evt-agent-evo-record"],
                "batch_size": 1,
            },
        },
    )

    async def fake_run_agent_self_evolution(**_kwargs):
        return SimpleNamespace(
            status="reported",
            reason=None,
            output_count=1,
            outputs=[str(tmp_path / "review-swarm.md")],
            asset_outcomes=[
                SimpleNamespace(
                    asset_type="skill",
                    target="review-swarm",
                    status="candidate_generated",
                    reason=None,
                    candidate_path=str(tmp_path / "review-swarm.md"),
                    verification_status="passed",
                    baseline_score=0.21,
                    candidate_score=0.89,
                    heldout_examples=4,
                    gate_reason=None,
                    commit_sha=None,
                )
            ],
        )

    monkeypatch.setattr(
        "agent.selfevolution.runtime.run_agent_self_evolution_cycle",
        fake_run_agent_self_evolution,
    )

    result = await worker._run_agent_self_evolution_event(event)

    assert result.status == "reported"
    tracked = tracking.get_run("agent-self-evo-run-1")
    assert tracked is not None
    records = tracked["agent_records"]
    assert [item["record_kind"] for item in records] == [
        "agent_self_evolution.summary",
        "agent_self_evolution.asset",
    ]
    assert records[0]["result_json"]["status"] == "reported"
    assert records[0]["result_json"]["output_count"] == 1
    assert records[0]["metadata_json"]["agent_type"] == "mention"
    assert records[1]["result_json"]["asset_type"] == "skill"
    assert records[1]["result_json"]["target"] == "review-swarm"
    assert records[1]["result_json"]["status"] == "candidate_generated"
    assert records[1]["result_json"]["candidate_path"] == str(tmp_path / "review-swarm.md")


@pytest.mark.asyncio
async def test_runtime_daily_audit_direction_persistence_event_dispatches_separate_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-daily-direction",
        event_type="daily_audit_direction_persistence",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit direction persistence",
        payload={"kind": "direction_archive"},
    )
    called = {}

    async def fake_run_direction_persistence(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="persisted")

    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.direction.run_daily_audit_direction_persistence",
        fake_run_direction_persistence,
    )

    result = await worker._run_daily_audit_direction_persistence_event(event)

    assert result.status == "persisted"
    assert called["run"]["project_id"] == "team/project"
    assert called["run"]["event"] is event


@pytest.mark.asyncio
async def test_runtime_daily_audit_short_term_persistence_event_dispatches_separate_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-daily-short",
        event_type="daily_audit_short_term_persistence",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit short term persistence",
        payload={"kind": "short_term_persistence"},
    )
    called = {}

    async def fake_run_short_term(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="persisted")

    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.short_term.run_daily_audit_short_term_persistence",
        fake_run_short_term,
    )

    result = await worker._run_daily_audit_short_term_persistence_event(event)

    assert result.status == "persisted"
    assert called["run"]["project_id"] == "team/project"
    assert called["run"]["event"] is event


@pytest.mark.asyncio
async def test_runtime_daily_audit_long_term_persistence_event_dispatches_separate_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-daily-long",
        event_type="daily_audit_long_term_persistence",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit long term persistence",
        payload={"kind": "long_term_persistence"},
    )
    called = {}

    async def fake_run_long_term(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="persisted")

    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.long_term.run_daily_audit_long_term_persistence",
        fake_run_long_term,
    )

    result = await worker._run_daily_audit_long_term_persistence_event(event)

    assert result.status == "persisted"
    assert called["run"]["project_id"] == "team/project"
    assert called["run"]["event"] is event


@pytest.mark.asyncio
async def test_runtime_daily_audit_skill_persistence_event_dispatches_separate_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-daily-skill",
        event_type="daily_audit_skill_persistence",
        project_id="team/project",
        mr_iid=None,
        source_branch="main",
        target_branch="main",
        title="Daily audit skill persistence",
        payload={"kind": "skill_persistence"},
    )
    called = {}

    async def fake_run_skill(**kwargs):
        called["run"] = kwargs
        return SimpleNamespace(status="reviewed")

    monkeypatch.setattr(
        "agent.scenes.daily_audit.persistence.skill.run_daily_audit_skill_persistence",
        fake_run_skill,
    )

    result = await worker._run_daily_audit_skill_persistence_event(event)

    assert result.status == "reviewed"
    assert called["run"]["project_id"] == "team/project"
    assert called["run"]["event"] is event


@pytest.mark.asyncio
async def test_runtime_auto_review_event_skips_stale_head_before_setup_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-stale",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-old",
    )

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-new", source_branch="feature/router"),
    )
    monkeypatch.setattr(
        "agent.sandbox.manager.setup_sandbox",
        lambda *_args, **_kwargs: pytest.fail("stale auto-review events should not provision a sandbox"),
    )

    result = await worker._run_auto_review_event(event)

    assert result.status == "skipped"
    assert result.reason == "stale_webhook_head_sha"


@pytest.mark.asyncio
async def test_runtime_mention_event_skips_stale_head_before_setup_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-stale-mention",
        event_type="mention",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-old",
        note_id=12,
        discussion_id="disc-1",
        note_body="please fix this",
        note_author="developer",
    )

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-new", source_branch="feature/router"),
    )
    monkeypatch.setattr(
        "agent.sandbox.manager.setup_sandbox",
        lambda *_args, **_kwargs: pytest.fail("stale mention events should not provision a sandbox"),
    )

    result = await worker._run_mention_event(event)

    assert result.status == "skipped"
    assert result.degraded_reason == "stale_webhook_head_sha"


@pytest.mark.asyncio
async def test_runtime_auto_review_event_records_journal_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-journal",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-123",
        payload={
            "_runtime": {
                "run_id": "run-journal",
                "execution_key": "exec-journal",
                "batch_event_ids": ["evt-journal"],
                "batch_size": 1,
            }
        },
    )
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")

    async def fake_setup_sandbox(*_args, **_kwargs):
        return sandbox, "/tmp/sandbox/repo"

    async def fake_run_auto_review(**_kwargs):
        return SimpleNamespace(status="published")

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
    )
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.sandbox.manager.create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/review-evt-journal")
    monkeypatch.setattr("agent.sandbox.manager.cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.scenes.auto_review.orchestrator.run_auto_review", fake_run_auto_review)

    await worker._run_auto_review_event(event)

    store = await get_runtime_store()
    journal = await store.list_run_journal("exec-journal")
    checkpoint = await store.get_run_checkpoint("exec-journal")

    assert [(item.event_type, item.stage_key) for item in journal] == [
        ("stage_started", "preflight"),
        ("checkpoint_written", "preflight"),
        ("stage_completed", "preflight"),
        ("stage_started", "sandbox_prepare"),
        ("checkpoint_written", "sandbox_prepare"),
        ("stage_completed", "sandbox_prepare"),
        ("stage_started", "scene_execute"),
        ("checkpoint_written", "scene_execute"),
        ("stage_completed", "scene_execute"),
        ("run_completed", None),
    ]
    assert checkpoint is None


@pytest.mark.asyncio
async def test_runtime_auto_review_event_records_restart_marker_from_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    store = await get_runtime_store()
    await store.write_run_checkpoint(
        RunCheckpoint(
            execution_key="exec-restart",
            actor_key="team/project!42",
            scene="auto_review",
            workflow_version=worker._WORKFLOW_VERSIONS["auto_review"],
            stage_key="sandbox_prepare",
        )
    )
    event = EventEnvelope(
        event_id="evt-restart",
        event_type="auto_review",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-123",
        payload={
            "_runtime": {
                "run_id": "run-restart",
                "execution_key": "exec-restart",
                "batch_event_ids": ["evt-restart"],
                "batch_size": 1,
            }
        },
    )

    async def fake_setup_sandbox(*_args, **_kwargs):
        return SimpleNamespace(root_dir="/tmp/sandbox"), "/tmp/sandbox/repo"

    async def fake_run_auto_review(**_kwargs):
        return SimpleNamespace(status="published")

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
    )
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.sandbox.manager.create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/review-evt-restart")
    monkeypatch.setattr("agent.sandbox.manager.cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.scenes.auto_review.orchestrator.run_auto_review", fake_run_auto_review)

    await worker._run_auto_review_event(event)

    journal = await store.list_run_journal("exec-restart")

    assert journal[0].event_type == "run_restarted"
    assert journal[0].stage_key == "sandbox_prepare"


@pytest.mark.asyncio
async def test_runtime_mention_event_records_runtime_observations(tmp_path, monkeypatch):
    from agent.runtime.journal_observer import record_runtime_observation

    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_runtime_clients()
    event = EventEnvelope(
        event_id="evt-mention-observation",
        event_type="mention",
        project_id="team/project",
        mr_iid=42,
        source_branch="feature/router",
        head_sha="head-123",
        note_id=12,
        discussion_id="disc-1",
        note_body="please fix this",
        note_author="developer",
        payload={
            "_runtime": {
                "run_id": "run-mention-observation",
                "execution_key": "exec-mention-observation",
                "batch_event_ids": ["evt-mention-observation"],
                "batch_size": 1,
            }
        },
    )

    async def fake_setup_sandbox(*_args, **_kwargs):
        return SimpleNamespace(root_dir="/tmp/sandbox"), "/tmp/sandbox/repo"

    async def fake_run_mention(**_kwargs):
        await record_runtime_observation(
            "mention author round started",
            details={"mention_role": "author", "mention_round": 1},
        )
        await record_runtime_observation(
            "mention reviewer round completed",
            details={"mention_role": "reviewer", "mention_round": 1, "approved": True},
        )
        return SimpleNamespace(status="replied", intent="analysis")

    monkeypatch.setattr(
        "agent.gitlab.mr_info.get_mr_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
    )
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.scenes.mention.orchestrator.run_mention", fake_run_mention)

    await worker._run_mention_event(event)

    store = await get_runtime_store()
    journal = await store.list_run_journal("exec-mention-observation")

    assert [(item.event_type, item.stage_key) for item in journal] == [
        ("stage_started", "preflight"),
        ("checkpoint_written", "preflight"),
        ("stage_completed", "preflight"),
        ("stage_started", "sandbox_prepare"),
        ("checkpoint_written", "sandbox_prepare"),
        ("stage_completed", "sandbox_prepare"),
        ("stage_started", "scene_execute"),
        ("observation", "scene_execute"),
        ("observation", "scene_execute"),
        ("checkpoint_written", "scene_execute"),
        ("stage_completed", "scene_execute"),
        ("run_completed", None),
    ]
    assert journal[7].summary == "mention author round started"
    assert journal[7].details == {"mention_role": "author", "mention_round": 1}
    assert journal[8].summary == "mention reviewer round completed"
    assert journal[8].details == {"mention_role": "reviewer", "mention_round": 1, "approved": True}
