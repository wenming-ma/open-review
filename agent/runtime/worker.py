"""Worker entry points for durable MR actor execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as time_cls

from agent.config import settings
from agent.controlplane import get_config_service
from agent.gitlab.identity import schedule_bot_identity_prime
from agent.gitlab.project_ops import get_project_default_branch
from agent.observability import build_open_review_trace_name, configure_phoenix_tracing, start_open_review_span
from agent.runtime.journal_observer import RuntimeJournalObserver, bind_runtime_journal_observer
from agent.runtime.models import EventEnvelope, RunCheckpoint, RunJournalEvent, RunRecord
from agent.runtime.queue import (
    MR_ACTOR_JOB_NAME,
    get_job_queue,
    get_runtime_store,
    resume_runtime_processing,
)
from agent.runtime.termination import RunTerminationRequested
from agent.utils.timezone import iso_now, now_in_open_review_tz, to_open_review_tz

logger = logging.getLogger(__name__)

_WORKFLOW_VERSIONS = {
    "auto_review": "auto_review.v1",
    "mention": "mention.v2",
    "sandbox_cleanup": "sandbox_cleanup.v1",
    "daily_audit": "daily_audit.v1",
    "agent_self_evolution": "agent_self_evolution.v1",
    "daily_audit_evolution": "daily_audit_evolution.v1",
    "daily_audit_direction_persistence": "daily_audit_direction_persistence.v1",
    "daily_audit_short_term_persistence": "daily_audit_short_term_persistence.v1",
    "daily_audit_long_term_persistence": "daily_audit_long_term_persistence.v1",
    "daily_audit_skill_persistence": "daily_audit_skill_persistence.v1",
}

_SELF_EVOLUTION_AGENT_TYPES = ("mention", "auto_review", "daily_audit")
_SELF_EVOLUTION_SCHEDULE_AGENT_TYPE = "all"


@dataclass
class RuntimeHandlers:
    run_auto_review: Callable[[EventEnvelope], Awaitable[object] | object]
    run_mention: Callable[[EventEnvelope], Awaitable[object] | object]
    run_sandbox_cleanup: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_agent_self_evolution: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit_evolution: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit_direction_persistence: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit_short_term_persistence: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit_long_term_persistence: Callable[[EventEnvelope], Awaitable[object] | object] | None = None
    run_daily_audit_skill_persistence: Callable[[EventEnvelope], Awaitable[object] | object] | None = None


@dataclass(frozen=True)
class _ExecutionJournalContext:
    run_id: str
    execution_key: str
    actor_key: str
    scene: str
    workflow_version: str


@dataclass(frozen=True)
class SandboxCleanupResult:
    status: str
    reason: str
    thread_id: str


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


def _make_execution_key(actor_key: str, batch: list[EventEnvelope]) -> str:
    basis = "|".join([actor_key, batch[0].event_type, *[item.event_id for item in batch]])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{batch[0].event_type}:{digest}"


def _default_execution_key_for_event(event: EventEnvelope) -> str:
    digest = hashlib.sha256(f"{event.actor_key}|{event.event_type}|{event.event_id}".encode()).hexdigest()[:16]
    return f"{event.event_type}:{digest}"


def _runtime_metadata(event: EventEnvelope) -> dict[str, object]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    runtime = payload.get("_runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    return {
        "run_id": str(runtime.get("run_id") or f"{event.event_type}:{event.event_id}"),
        "execution_key": str(runtime.get("execution_key") or _default_execution_key_for_event(event)),
        "batch_event_ids": list(runtime.get("batch_event_ids") or [event.event_id]),
        "batch_size": int(runtime.get("batch_size") or 1),
    }


def _execution_journal_context(event: EventEnvelope) -> _ExecutionJournalContext:
    runtime = _runtime_metadata(event)
    return _ExecutionJournalContext(
        run_id=str(runtime["run_id"]),
        execution_key=str(runtime["execution_key"]),
        actor_key=event.actor_key,
        scene=event.event_type,
        workflow_version=_WORKFLOW_VERSIONS[event.event_type],
    )


def _journal_summary_from_result(result) -> str | None:
    for value in (
        getattr(result, "reason", None),
        getattr(result, "degraded_reason", None),
        getattr(result, "status", None),
    ):
        if value:
            return str(value)
    return None


def _journal_status_from_result(result) -> str:
    status = str(getattr(result, "status", "") or "").strip().lower()
    if status == "skipped":
        return "skipped"
    if status == "failed":
        return "failed"
    return "completed"


async def _record_run_journal_event(
    store,
    context: _ExecutionJournalContext,
    *,
    stage_key: str | None,
    event_type: str,
    status: str,
    summary: str | None = None,
    artifact_refs: dict[str, str] | None = None,
    details: dict[str, object] | None = None,
) -> None:
    await store.record_run_journal_event(
        RunJournalEvent(
            execution_key=context.execution_key,
            run_id=context.run_id,
            actor_key=context.actor_key,
            scene=context.scene,
            workflow_version=context.workflow_version,
            stage_key=stage_key,
            event_type=event_type,
            status=status,
            summary=summary,
            artifact_refs=artifact_refs or {},
            details=details or {},
        )
    )


async def _write_run_checkpoint(
    store,
    context: _ExecutionJournalContext,
    *,
    stage_key: str,
    summary: str | None = None,
    artifact_refs: dict[str, str] | None = None,
    details: dict[str, object] | None = None,
) -> None:
    checkpoint = RunCheckpoint(
        execution_key=context.execution_key,
        actor_key=context.actor_key,
        scene=context.scene,
        workflow_version=context.workflow_version,
        stage_key=stage_key,
        artifact_refs=artifact_refs or {},
        details=details or {},
    )
    await store.write_run_checkpoint(checkpoint)
    await _record_run_journal_event(
        store,
        context,
        stage_key=stage_key,
        event_type="checkpoint_written",
        status="running",
        summary=summary,
        artifact_refs=artifact_refs,
        details=details,
    )


async def _record_restart_if_needed(store, context: _ExecutionJournalContext) -> None:
    checkpoint = await store.get_run_checkpoint(context.execution_key)
    if checkpoint is None:
        return
    if checkpoint.workflow_version != context.workflow_version:
        await store.clear_run_checkpoint(context.execution_key)
        await _record_run_journal_event(
            store,
            context,
            stage_key=checkpoint.stage_key,
            event_type="run_restarted",
            status="restarted",
            summary="workflow version changed; restarting from preflight",
            details={
                "previous_workflow_version": checkpoint.workflow_version,
                "current_workflow_version": context.workflow_version,
            },
        )
        return
    await _record_run_journal_event(
        store,
        context,
        stage_key=checkpoint.stage_key,
        event_type="run_restarted",
        status="restarted",
        summary=f"resuming from checkpoint at {checkpoint.stage_key}",
        artifact_refs=checkpoint.artifact_refs,
        details=checkpoint.details,
    )


def _event_with_runtime_payload(event: EventEnvelope, run_record: RunRecord) -> EventEnvelope:
    payload = dict(event.payload)
    payload["_runtime"] = {
        "run_id": run_record.run_id,
        "execution_key": run_record.execution_key,
        "batch_event_ids": run_record.event_ids,
        "batch_size": run_record.batch_size,
    }
    return event.model_copy(update={"payload": payload})


def _track_run(
    run_record: RunRecord,
    result,
    *,
    state: str,
    reason: str | None,
    error: str | None = None,
    trace_context=None,
) -> None:
    from agent.controlplane import get_tracking_service

    payload = {
        "run_id": run_record.run_id,
        "execution_key": run_record.execution_key,
        "actor_key": run_record.actor_key,
        "project_id": run_record.project_id,
        "mr_iid": run_record.mr_iid,
        "event_type": run_record.event_type,
        "state": state,
        "reason": reason,
        "error": error,
        "head_sha": run_record.head_sha,
        "note_id": run_record.note_id,
        "discussion_id": run_record.discussion_id,
        "batch_size": run_record.batch_size,
        "started_at": run_record.started_at,
        "completed_at": iso_now(),
        "review_run_id": getattr(result, "review_run_id", None),
        "review_mode": getattr(result, "review_mode", None),
        "compressed_review": getattr(result, "compressed_review", False),
        "published_findings_count": getattr(result, "published_findings_count", 0),
        "suppressed_findings_count": getattr(result, "suppressed_findings_count", 0),
        "confirmed_findings_count": getattr(result, "confirmed_findings_count", 0),
        "suspicious_findings_count": getattr(result, "suspicious_findings_count", 0),
        "open_questions_count": getattr(result, "open_questions_count", 0),
        "inline_comments_count": getattr(result, "inline_comments_count", 0),
        "mention_intent": getattr(result, "intent", None),
        "mention_status": getattr(result, "status", None),
        "mention_degraded_reason": getattr(result, "degraded_reason", None),
        "changed_files_count": len(getattr(result, "changed_files", []) or []),
        "commit_sha": getattr(result, "commit_sha", None),
        "covered_note_ids": list(getattr(result, "covered_note_ids", []) or []),
        "trace_id": getattr(trace_context, "trace_id", None),
        "trace_url": getattr(trace_context, "trace_url", None),
        "session_id": getattr(trace_context, "session_id", None),
    }
    get_tracking_service().record_run(payload)


def _track_run_started(run_record: RunRecord) -> None:
    from agent.controlplane import get_tracking_service

    get_tracking_service().record_run(
        {
            "run_id": run_record.run_id,
            "execution_key": run_record.execution_key,
            "actor_key": run_record.actor_key,
            "project_id": run_record.project_id,
            "mr_iid": run_record.mr_iid,
            "event_type": run_record.event_type,
            "state": "running",
            "reason": None,
            "error": None,
            "head_sha": run_record.head_sha,
            "note_id": run_record.note_id,
            "discussion_id": run_record.discussion_id,
            "batch_size": run_record.batch_size,
            "started_at": run_record.started_at,
            "completed_at": None,
        }
    )


def _append_trigger_event(run_id: str, event: EventEnvelope) -> None:
    from agent.controlplane import get_tracking_service

    payload = dict(event.payload or {})
    payload.pop("_runtime", None)
    get_tracking_service().append_trigger_event(
        run_id,
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "project_id": event.project_id,
            "mr_iid": event.mr_iid,
            "source_branch": event.source_branch or None,
            "target_branch": event.target_branch or None,
            "head_sha": event.head_sha,
            "note_id": event.note_id,
            "discussion_id": event.discussion_id,
            "payload": payload,
        },
    )


def _update_tracked_run_state(
    run_id: str,
    *,
    state: str,
    reason: str | None,
    error: str | None = None,
) -> None:
    from agent.controlplane import get_tracking_service

    tracking = get_tracking_service()
    existing = tracking.get_run(run_id)
    if existing is None:
        return
    tracking.record_run(
        {
            **existing,
            "state": state,
            "reason": reason,
            "error": error,
            "completed_at": iso_now(),
        }
    )


async def _mark_interrupted_prior_runs_stale(store, run_record: RunRecord) -> None:
    interrupted_reason = "interrupted_run_restarted"
    prior_runs = await store.list_runs(run_record.actor_key, limit=50)
    for prior in prior_runs:
        if prior.run_id == run_record.run_id:
            continue
        if prior.execution_key != run_record.execution_key:
            continue
        if prior.state != "running":
            continue
        await store.write_run(
            prior.model_copy(
                update={
                    "state": "stale",
                    "completed_at": iso_now(),
                    "reason": interrupted_reason,
                }
            )
        )
        _update_tracked_run_state(
            prior.run_id,
            state="stale",
            reason=interrupted_reason,
        )


def _run_terminal_state(result) -> tuple[str, str | None]:
    status = getattr(result, "status", None)
    reason = getattr(result, "reason", None) or getattr(result, "degraded_reason", None)
    if status == "terminated":
        return "terminated", reason or "user_terminated"
    if isinstance(reason, str) and "stale" in reason:
        return "stale", reason
    if status == "failed":
        return "failed", reason
    if status == "skipped" and reason:
        if "stale" in reason:
            return "stale", reason
        return "skipped", reason
    return "succeeded", reason


def _event_user_id(event: EventEnvelope) -> str | None:
    if event.event_type == "mention":
        return event.note_author or None
    return None


def _event_trace_attributes(event: EventEnvelope, run_record: RunRecord) -> dict[str, object]:
    return {
        "open_review.actor_key": event.actor_key,
        "open_review.project_id": event.project_id,
        "open_review.mr_iid": event.mr_iid,
        "open_review.event_type": event.event_type,
        "open_review.run_id": run_record.run_id,
        "open_review.head_sha": event.head_sha or "",
        "open_review.note_id": event.note_id or 0,
        "open_review.discussion_id": event.discussion_id or "",
        "open_review.batch_size": run_record.batch_size,
    }


def _event_trace_input_summary(event: EventEnvelope) -> str:
    title = event.title.strip() or "(no title)"
    request_label = {
        "auto_review": "Auto Review",
        "mention": "Mention",
        "sandbox_cleanup": "Sandbox Cleanup",
        "daily_audit": "Daily Audit",
        "agent_self_evolution": "Agent Self Evolution",
        "daily_audit_evolution": "Daily Audit Evolution",
        "daily_audit_direction_persistence": "Daily Audit Direction Persistence",
        "daily_audit_short_term_persistence": "Daily Audit Short Term Persistence",
        "daily_audit_long_term_persistence": "Daily Audit Long Term Persistence",
        "daily_audit_skill_persistence": "Daily Audit Skill Persistence",
    }[event.event_type]
    lines = [
        f"## {request_label} Request",
        f"- Actor: `{event.actor_key}`",
        f"- Event Type: `{event.event_type}`",
        f"- Title: {title}",
        f"- Source Branch: `{event.source_branch or '(unknown)'}`",
        f"- Target Branch: `{event.target_branch or '(unknown)'}`",
        f"- Head SHA: `{event.head_sha or '(unknown)'}`",
    ]
    if event.event_type == "agent_self_evolution":
        lines.append(f"- Agent Type: `{str(event.payload.get('agent_type') or '(unknown)')}`")
    if event.event_type == "mention":
        lines.append(f"- Note ID: `{event.note_id or 0}`")
        if event.note_body:
            lines.append(f"- Note: {event.note_body.strip()}")
    return "\n".join(lines)


def _event_trace_output_summary(event_type: str, result: object) -> str:
    if event_type == "auto_review":
        lines = [
            "## Auto Review Result",
            f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
            f"- Review Run ID: `{getattr(result, 'review_run_id', None) or '(none)'}`",
            f"- Recommendation: `{getattr(result, 'recommendation', None) or '(none)'}`",
            f"- Confirmed Findings: `{getattr(result, 'confirmed_findings_count', 0)}`",
            f"- Suspicious Findings: `{getattr(result, 'suspicious_findings_count', 0)}`",
            f"- Open Questions: `{getattr(result, 'open_questions_count', 0)}`",
            f"- Inline Comments: `{getattr(result, 'inline_comments_count', 0)}`",
        ]
        reason = getattr(result, "reason", None) or getattr(result, "degraded_reason", None)
        if reason:
            lines.append(f"- Reason: {reason}")
        return "\n".join(lines)

    if event_type == "daily_audit":
        return "\n".join(
            [
                "## Daily Audit Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Unit Type: `{getattr(result, 'unit_type', None) or '(unknown)'}`",
                f"- Unit Label: `{getattr(result, 'unit_label', None) or '(unknown)'}`",
                f"- Findings: `{getattr(result, 'finding_count', 0)}`",
                f"- Reason: {getattr(result, 'reason', None) or getattr(result, 'degraded_reason', None) or '(none)'}",
            ]
        )

    if event_type == "sandbox_cleanup":
        return "\n".join(
            [
                "## Sandbox Cleanup Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Thread ID: `{getattr(result, 'thread_id', None) or '(unknown)'}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )

    if event_type == "daily_audit_direction_persistence":
        return "\n".join(
            [
                "## Daily Audit Direction Persistence Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Run ID: `{getattr(result, 'run_id', None) or '(unknown)'}`",
                f"- Archived Unit: `{getattr(result, 'unit_label', None) or '(unknown)'}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )

    if event_type == "daily_audit_short_term_persistence":
        return "\n".join(
            [
                "## Daily Audit Short Term Persistence Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Run ID: `{getattr(result, 'run_id', None) or '(unknown)'}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )
    if event_type == "daily_audit_long_term_persistence":
        return "\n".join(
            [
                "## Daily Audit Long Term Persistence Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Run ID: `{getattr(result, 'run_id', None) or '(unknown)'}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )
    if event_type == "daily_audit_skill_persistence":
        return "\n".join(
            [
                "## Daily Audit Skill Persistence Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Run ID: `{getattr(result, 'run_id', None) or '(unknown)'}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )
    if event_type == "agent_self_evolution":
        asset_outcomes = list(getattr(result, "asset_outcomes", []) or [])
        skipped_count = sum(1 for item in asset_outcomes if getattr(item, "status", None) == "skipped")
        failed_count = sum(1 for item in asset_outcomes if getattr(item, "status", None) in {"failed", "rejected"})
        return "\n".join(
            [
                "## Agent Self Evolution Result",
                f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
                f"- Output Count: `{getattr(result, 'output_count', 0)}`",
                f"- Skipped Assets: `{skipped_count}`",
                f"- Failed Assets: `{failed_count}`",
                f"- Reason: {getattr(result, 'reason', None) or '(none)'}",
            ]
        )

    return "\n".join(
        [
            "## Mention Result",
            f"- Status: `{getattr(result, 'status', 'unknown') or 'unknown'}`",
            f"- Intent: `{getattr(result, 'intent', None) or '(unknown)'}`",
            f"- Commit SHA: `{getattr(result, 'commit_sha', None) or '(none)'}`",
            f"- Reason: {getattr(result, 'reason', None) or getattr(result, 'degraded_reason', None) or '(none)'}",
        ]
    )


def _event_trace_name(event: EventEnvelope, run_record: RunRecord) -> str:
    return build_open_review_trace_name(
        event.event_type,
        event.actor_key,
        head_sha=event.head_sha,
        run_key=run_record.execution_key or run_record.run_id,
        note_id=event.note_id if event.event_type == "mention" else None,
    )


async def _run_auto_review_event(event: EventEnvelope):
    from agent.gitlab.mr_info import get_mr_metadata
    from agent.runtime.publish import GitLabPublishService
    from agent.sandbox.manager import (
        cleanup_temporary_worktree,
        create_temporary_worktree,
        setup_sandbox,
    )
    from agent.scenes.auto_review.models import AutoReviewRunResult
    from agent.scenes.auto_review.orchestrator import run_auto_review
    from agent.utils.thread_id import generate_thread_id

    store = await get_runtime_store()
    journal_context = _execution_journal_context(event)
    runtime = _runtime_metadata(event)
    await _record_restart_if_needed(store, journal_context)
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_started",
        status="running",
        summary="load latest merge request metadata",
    )
    agent_config = _project_agent_config(event.project_id)
    metadata = get_mr_metadata(event.project_id, event.mr_iid)
    latest_head_sha = getattr(metadata, "head_sha", None)
    source_branch = getattr(metadata, "source_branch", None) or event.source_branch
    await _write_run_checkpoint(
        store,
        journal_context,
        stage_key="preflight",
        summary="latest merge request metadata loaded",
        details={
            "expected_head_sha": event.head_sha or "",
            "latest_head_sha": latest_head_sha or "",
            "source_branch": source_branch,
        },
    )
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_completed",
        status="completed",
        summary="preflight checks completed",
    )
    if event.head_sha and latest_head_sha and latest_head_sha != event.head_sha:
        result = AutoReviewRunResult(status="skipped", reason="stale_webhook_head_sha")
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=None,
            event_type="run_completed",
            status="skipped",
            summary="stale_webhook_head_sha",
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        return result

    thread_id = generate_thread_id(event.project_id, event.mr_iid)
    sandbox = None
    repo_dir = ""
    worktree_dir = None
    try:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_started",
            status="running",
            summary="prepare sandbox checkout and temporary worktree",
        )
        sandbox, repo_dir = await setup_sandbox(thread_id, event.project_id, source_branch)
        publish_service = GitLabPublishService(
            store=store,
            actor_key=event.actor_key,
            tracking_run_id=str(runtime["run_id"]),
        )
        worktree_dir = create_temporary_worktree(
            sandbox,
            repo_dir=repo_dir,
            head_sha="HEAD",
            run_id=f"review-{event.event_id}",
        )
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            summary="sandbox checkout prepared",
            artifact_refs={"repo_dir": repo_dir, "worktree_dir": worktree_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_completed",
            status="completed",
            summary="sandbox and worktree ready",
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_started",
            status="running",
            summary="run auto-review orchestrator",
        )
        result = await run_auto_review(
            project_id=event.project_id,
            mr_iid=event.mr_iid,
            repo_dir=worktree_dir,
            sandbox=sandbox,
            runtime_run_id=str(runtime["run_id"]),
            expected_head_sha=event.head_sha,
            publish_service=publish_service,
            agent_config=agent_config,
        )
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="scene_execute",
            summary=_journal_summary_from_result(result),
            artifact_refs={"worktree_dir": worktree_dir},
            details={"result_status": getattr(result, "status", None) or ""},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
            artifact_refs={"worktree_dir": worktree_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=None,
            event_type="run_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        return result
    except RunTerminationRequested as exc:
        failed_stage = "scene_execute" if worktree_dir else "sandbox_prepare"
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=failed_stage,
            event_type="run_terminated",
            status="terminated",
            summary=exc.reason,
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        raise
    except Exception as exc:
        failed_stage = "scene_execute" if worktree_dir else "sandbox_prepare"
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=failed_stage,
            event_type="run_failed",
            status="failed",
            summary=str(exc),
        )
        raise
    finally:
        if sandbox is not None and worktree_dir is not None:
            cleanup_temporary_worktree(sandbox, repo_dir=repo_dir, worktree_dir=worktree_dir)


async def _run_mention_event(event: EventEnvelope):
    from agent.gitlab.mr_info import get_mr_metadata
    from agent.runtime.publish import GitLabPublishService
    from agent.sandbox.manager import setup_sandbox
    from agent.scenes.mention.models import MentionExecutionResult
    from agent.scenes.mention.orchestrator import run_mention
    from agent.utils.thread_id import generate_thread_id

    store = await get_runtime_store()
    journal_context = _execution_journal_context(event)
    runtime = _runtime_metadata(event)
    await _record_restart_if_needed(store, journal_context)
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_started",
        status="running",
        summary="load latest merge request metadata",
    )
    agent_config = _project_agent_config(event.project_id)
    metadata = get_mr_metadata(event.project_id, event.mr_iid)
    latest_head_sha = getattr(metadata, "head_sha", None)
    source_branch = event.source_branch or getattr(metadata, "source_branch", None)
    await _write_run_checkpoint(
        store,
        journal_context,
        stage_key="preflight",
        summary="latest merge request metadata loaded",
        details={
            "expected_head_sha": event.head_sha or "",
            "latest_head_sha": latest_head_sha or "",
            "source_branch": source_branch or "",
        },
    )
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_completed",
        status="completed",
        summary="preflight checks completed",
    )
    if event.head_sha and latest_head_sha and latest_head_sha != event.head_sha:
        result = MentionExecutionResult(
            intent="reply",
            status="skipped",
            reply_markdown="执行开始前合并请求的 head 已变化，本次 mention 已跳过。",
            degraded_reason="stale_webhook_head_sha",
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=None,
            event_type="run_completed",
            status="skipped",
            summary="stale_webhook_head_sha",
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        return result

    thread_id = generate_thread_id(event.project_id, event.mr_iid)
    try:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_started",
            status="running",
            summary="prepare sandbox checkout",
        )
        sandbox, repo_dir = await setup_sandbox(thread_id, event.project_id, source_branch)
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            summary="sandbox checkout prepared",
            artifact_refs={"repo_dir": repo_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_completed",
            status="completed",
            summary="sandbox ready",
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_started",
            status="running",
            summary="run mention orchestrator",
        )
        publish_service = GitLabPublishService(
            store=store,
            actor_key=event.actor_key,
            tracking_run_id=str(runtime["run_id"]),
        )
        observer = RuntimeJournalObserver(
            record=lambda stage_key, event_type, status, summary, details: _record_run_journal_event(
                store,
                journal_context,
                stage_key=stage_key,
                event_type=event_type,
                status=status,
                summary=summary,
                details=details,
            )
        )
        with bind_runtime_journal_observer(observer):
            result = await run_mention(
                project_id=event.project_id,
                mr_iid=event.mr_iid,
                repo_dir=repo_dir,
                sandbox=sandbox,
                runtime_run_id=str(runtime["run_id"]),
                note_id=event.note_id or 0,
                discussion_id=event.discussion_id,
                note_body=event.note_body or "",
                note_author=event.note_author or "unknown",
                expected_head_sha=event.head_sha,
                batched_events=event.payload.get("batched_events"),
                publish_service=publish_service,
                agent_config=agent_config,
            )
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="scene_execute",
            summary=_journal_summary_from_result(result),
            artifact_refs={"repo_dir": repo_dir},
            details={"result_status": getattr(result, "status", None) or ""},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
            artifact_refs={"repo_dir": repo_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=None,
            event_type="run_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        return result
    except RunTerminationRequested as exc:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="run_terminated",
            status="terminated",
            summary=exc.reason,
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        raise
    except Exception as exc:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="run_failed",
            status="failed",
            summary=str(exc),
        )
        raise


async def _run_daily_audit_event(event: EventEnvelope):
    from agent.runtime.publish import GitLabPublishService
    from agent.sandbox.manager import setup_sandbox
    from agent.scenes.daily_audit.orchestrator import run_daily_audit

    store = await get_runtime_store()
    journal_context = _execution_journal_context(event)
    runtime = _runtime_metadata(event)
    await _record_restart_if_needed(store, journal_context)
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_started",
        status="running",
        summary="prepare project-level daily audit context",
    )
    agent_config = _project_agent_config(event.project_id)
    source_branch = event.source_branch or event.target_branch or "main"
    await _write_run_checkpoint(
        store,
        journal_context,
        stage_key="preflight",
        summary="daily audit preflight completed",
        details={
            "source_branch": source_branch,
            "default_branch": event.target_branch or source_branch,
        },
    )
    await _record_run_journal_event(
        store,
        journal_context,
        stage_key="preflight",
        event_type="stage_completed",
        status="completed",
        summary="daily audit preflight completed",
    )

    sandbox = None
    repo_dir = ""
    try:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_started",
            status="running",
            summary="prepare sandbox checkout for daily audit",
        )
        sandbox, repo_dir = await setup_sandbox(event.actor_key, event.project_id, source_branch)
        publish_service = GitLabPublishService(
            store=store,
            actor_key=event.actor_key,
            tracking_run_id=str(runtime["run_id"]),
        )
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            summary="sandbox checkout prepared",
            artifact_refs={"repo_dir": repo_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="sandbox_prepare",
            event_type="stage_completed",
            status="completed",
            summary="sandbox ready for daily audit",
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_started",
            status="running",
            summary="run daily audit orchestrator",
        )
        result = await run_daily_audit(
            project_id=event.project_id,
            repo_dir=repo_dir,
            sandbox=sandbox,
            default_branch=source_branch,
            publish_service=publish_service,
            event=event,
            runtime_run_id=str(runtime["run_id"]),
            agent_config=agent_config,
        )
        await _write_run_checkpoint(
            store,
            journal_context,
            stage_key="scene_execute",
            summary=_journal_summary_from_result(result),
            artifact_refs={"repo_dir": repo_dir},
            details={"result_status": getattr(result, "status", None) or ""},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute",
            event_type="stage_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
            artifact_refs={"repo_dir": repo_dir},
        )
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key=None,
            event_type="run_completed",
            status=_journal_status_from_result(result),
            summary=_journal_summary_from_result(result),
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        return result
    except RunTerminationRequested as exc:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute" if repo_dir else "sandbox_prepare",
            event_type="run_terminated",
            status="terminated",
            summary=exc.reason,
        )
        await store.clear_run_checkpoint(journal_context.execution_key)
        raise
    except Exception as exc:
        await _record_run_journal_event(
            store,
            journal_context,
            stage_key="scene_execute" if repo_dir else "sandbox_prepare",
            event_type="run_failed",
            status="failed",
            summary=str(exc),
        )
        raise


async def _run_agent_self_evolution_event(event: EventEnvelope):
    from agent.controlplane import get_tracking_service
    from agent.selfevolution.runtime import run_agent_self_evolution_cycle

    default_branch = event.source_branch or event.target_branch or "main"
    agent_type = str(event.payload.get("agent_type") or "all").strip() or "all"
    kwargs = {
        "agent_type": agent_type,
        "project_id": event.project_id,
        "default_branch": default_branch,
        "event": event,
    }
    if inspect.iscoroutinefunction(run_agent_self_evolution_cycle):
        result = await run_agent_self_evolution_cycle(**kwargs)
    else:
        result = await asyncio.to_thread(
            run_agent_self_evolution_cycle,
            **kwargs,
        )
    runtime = _runtime_metadata(event)
    run_id = str(runtime.get("run_id") or "").strip()
    if run_id:
        tracking = get_tracking_service()
        tracked = tracking.get_run(run_id)
        if tracked is not None:
            asset_outcomes = list(getattr(result, "asset_outcomes", []) or [])
            tracking.append_agent_record(
                run_id,
                {
                    "record_kind": "agent_self_evolution.summary",
                    "input_messages_json": [],
                    "result_json": {
                        "status": getattr(result, "status", None),
                        "reason": getattr(result, "reason", None),
                        "output_count": getattr(result, "output_count", 0),
                        "output_paths": [str(item) for item in (getattr(result, "outputs", []) or [])],
                        "applied_count": sum(1 for item in asset_outcomes if getattr(item, "status", None) == "applied"),
                        "rejected_count": sum(1 for item in asset_outcomes if getattr(item, "status", None) == "rejected"),
                        "skipped_count": sum(1 for item in asset_outcomes if getattr(item, "status", None) == "skipped"),
                        "failed_count": sum(1 for item in asset_outcomes if getattr(item, "status", None) == "failed"),
                    },
                    "metadata_json": {
                        "agent_type": agent_type,
                        "agent_types": event.payload.get("agent_types") or None,
                        "default_branch": default_branch,
                        "event_id": event.event_id,
                        "trigger_source": str(
                            event.payload.get("trigger_source") or event.payload.get("trigger") or ""
                        ).strip()
                        or None,
                    },
                },
            )
            for outcome in asset_outcomes:
                tracking.append_agent_record(
                    run_id,
                    {
                        "record_kind": "agent_self_evolution.asset",
                        "input_messages_json": [],
                        "result_json": {
                            "asset_type": getattr(outcome, "asset_type", None),
                            "target": getattr(outcome, "target", None),
                            "status": getattr(outcome, "status", None),
                            "reason": getattr(outcome, "reason", None),
                            "candidate_path": getattr(outcome, "candidate_path", None),
                            "verification_status": getattr(outcome, "verification_status", None),
                            "baseline_score": getattr(outcome, "baseline_score", None),
                            "candidate_score": getattr(outcome, "candidate_score", None),
                            "heldout_examples": getattr(outcome, "heldout_examples", None),
                            "gate_reason": getattr(outcome, "gate_reason", None),
                            "commit_sha": getattr(outcome, "commit_sha", None),
                            "train_count": getattr(outcome, "train_count", None),
                            "val_count": getattr(outcome, "val_count", None),
                            "heldout_count": getattr(outcome, "heldout_count", None),
                            "dimension_scores_summary": getattr(outcome, "dimension_scores_summary", None),
                            "feedback_coverage": getattr(outcome, "feedback_coverage", None),
                            "materialization_failures": getattr(outcome, "materialization_failures", None),
                        },
                        "metadata_json": {
                            "agent_type": agent_type,
                            "event_id": event.event_id,
                        },
                    },
                )
    return result


async def _run_daily_audit_evolution_event(event: EventEnvelope):
    payload = dict(event.payload or {})
    payload.setdefault("agent_type", "daily_audit")
    wrapped = event.model_copy(update={"payload": payload})
    return await _run_agent_self_evolution_event(wrapped)


async def _run_daily_audit_direction_persistence_event(event: EventEnvelope):
    from agent.scenes.daily_audit.persistence.direction import run_daily_audit_direction_persistence

    default_branch = event.source_branch or event.target_branch or "main"
    runtime = _runtime_metadata(event)
    return await _maybe_await(
        run_daily_audit_direction_persistence(
            project_id=event.project_id,
            default_branch=default_branch,
            event=event,
            runtime_run_id=str(runtime["run_id"]),
        )
    )


async def _run_daily_audit_short_term_persistence_event(event: EventEnvelope):
    from agent.scenes.daily_audit.persistence.short_term import (
        run_daily_audit_short_term_persistence,
    )

    default_branch = event.source_branch or event.target_branch or "main"
    runtime = _runtime_metadata(event)
    return await _maybe_await(
        run_daily_audit_short_term_persistence(
            project_id=event.project_id,
            default_branch=default_branch,
            event=event,
            runtime_run_id=str(runtime["run_id"]),
        )
    )


async def _run_daily_audit_long_term_persistence_event(event: EventEnvelope):
    from agent.scenes.daily_audit.persistence.long_term import run_daily_audit_long_term_persistence

    default_branch = event.source_branch or event.target_branch or "main"
    runtime = _runtime_metadata(event)
    return await _maybe_await(
        run_daily_audit_long_term_persistence(
            project_id=event.project_id,
            default_branch=default_branch,
            event=event,
            runtime_run_id=str(runtime["run_id"]),
        )
    )


async def _run_daily_audit_skill_persistence_event(event: EventEnvelope):
    from agent.scenes.daily_audit.persistence.skill import run_daily_audit_skill_persistence

    default_branch = event.source_branch or event.target_branch or "main"
    runtime = _runtime_metadata(event)
    return await _maybe_await(
        run_daily_audit_skill_persistence(
            project_id=event.project_id,
            default_branch=default_branch,
            event=event,
            runtime_run_id=str(runtime["run_id"]),
        )
    )


async def _run_sandbox_cleanup_event(event: EventEnvelope) -> SandboxCleanupResult:
    from agent.sandbox.manager import cleanup_sandbox
    from agent.utils.thread_id import generate_thread_id

    if event.mr_iid is None:
        raise ValueError("sandbox_cleanup events require mr_iid")
    thread_id = generate_thread_id(event.project_id, event.mr_iid)
    cleanup_sandbox(thread_id)
    return SandboxCleanupResult(
        status="cleaned",
        reason="sandbox_cleanup_completed",
        thread_id=thread_id,
    )


def default_runtime_handlers() -> RuntimeHandlers:
    return RuntimeHandlers(
        run_auto_review=_run_auto_review_event,
        run_mention=_run_mention_event,
        run_sandbox_cleanup=_run_sandbox_cleanup_event,
        run_daily_audit=_run_daily_audit_event,
        run_agent_self_evolution=_run_agent_self_evolution_event,
        run_daily_audit_evolution=_run_daily_audit_evolution_event,
        run_daily_audit_direction_persistence=_run_daily_audit_direction_persistence_event,
        run_daily_audit_short_term_persistence=_run_daily_audit_short_term_persistence_event,
        run_daily_audit_long_term_persistence=_run_daily_audit_long_term_persistence_event,
        run_daily_audit_skill_persistence=_run_daily_audit_skill_persistence_event,
    )


def _make_run_record(actor_key: str, batch: list[EventEnvelope]) -> RunRecord:
    event = batch[-1] if batch and batch[0].event_type == "auto_review" else batch[0]
    return RunRecord(
        run_id=f"{event.event_type}:{event.event_id}:{uuid.uuid4().hex[:8]}",
        execution_key=_make_execution_key(actor_key, batch),
        actor_key=actor_key,
        event_type=event.event_type,
        project_id=event.project_id,
        mr_iid=event.mr_iid,
        event_ids=[item.event_id for item in batch],
        batch_size=len(batch),
        head_sha=event.head_sha,
        note_id=event.note_id,
        discussion_id=event.discussion_id,
    )


async def _lease_heartbeat_loop(
    store,
    *,
    actor_key: str,
    worker_id: str,
) -> None:
    interval = max(float(settings.RUN_HEARTBEAT_SECONDS), 0.01)
    while True:
        await asyncio.sleep(interval)
        ok = await store.heartbeat_lease(actor_key, worker_id, settings.MR_ACTOR_LEASE_SECONDS)
        if not ok:
            logger.warning("Lost lease heartbeat for actor %s worker=%s", actor_key, worker_id)
            return


async def drain_mr_actor(
    actor_key: str,
    *,
    store=None,
    queue=None,
    handlers: RuntimeHandlers | None = None,
    worker_id: str | None = None,
) -> bool:
    store = store or await get_runtime_store()
    queue = queue or await get_job_queue()
    handlers = handlers or default_runtime_handlers()
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    acquired = await store.acquire_lease(actor_key, worker_id, settings.MR_ACTOR_LEASE_SECONDS)
    if not acquired:
        logger.info("Actor %s is already leased; leaving pending events queued", actor_key)
        return False

    try:
        restored = await store.restore_inflight(actor_key)
        if restored:
            logger.warning("Restored %d inflight events for actor %s after an interrupted run", restored, actor_key)
        while True:
            batch = await store.pop_next_batch(actor_key)
            if not batch:
                break
            event = batch[-1] if batch[0].event_type == "auto_review" else batch[0]
            if batch[0].event_type == "mention" and len(batch) > 1:
                event = batch[-1].model_copy(
                    update={
                        "payload": {
                            **batch[-1].payload,
                            "batched_events": [item.model_dump(mode="json") for item in batch],
                        }
                    }
                )
            run_record = _make_run_record(actor_key, batch)
            event = _event_with_runtime_payload(event, run_record)
            journal_context = _execution_journal_context(event)
            await _mark_interrupted_prior_runs_stale(store, run_record)
            await store.write_run(run_record)
            _track_run_started(run_record)
            _append_trigger_event(run_record.run_id, event)
            logger.info(
                "Draining actor %s event=%s type=%s batch=%d",
                actor_key,
                event.event_id,
                event.event_type,
                len(batch),
            )
            termination = await store.get_run_termination(run_record.run_id)
            if termination is not None:
                await store.ack_batch(actor_key, batch)
                await store.write_run(
                    run_record.model_copy(
                        update={
                            "state": "terminated",
                            "completed_at": iso_now(),
                            "reason": "user_terminated",
                        }
                    )
                )
                await _record_run_journal_event(
                    store,
                    journal_context,
                    stage_key=None,
                    event_type="run_terminated",
                    status="terminated",
                    summary="user_terminated",
                    details={
                        "requested_by": termination.requested_by,
                        "requested_at": termination.requested_at,
                    },
                )
                await store.clear_run_checkpoint(journal_context.execution_key)
                _track_run(
                    run_record,
                    None,
                    state="terminated",
                    reason="user_terminated",
                )
                continue
            # Renew once before the handler starts so long-running work begins with a fresh lease.
            await store.heartbeat_lease(actor_key, worker_id, settings.MR_ACTOR_LEASE_SECONDS)
            heartbeat_task = asyncio.create_task(
                _lease_heartbeat_loop(store, actor_key=actor_key, worker_id=worker_id)
            )
            try:
                with start_open_review_span(
                    _event_trace_name(event, run_record),
                    session_id=actor_key,
                    user_id=_event_user_id(event),
                    attributes=_event_trace_attributes(event, run_record),
                    metadata={
                        "event_ids": run_record.event_ids,
                        "title": event.title,
                    },
                    tags=[event.event_type, "runtime"],
                    span_kind="chain",
                ) as trace_context:
                    trace_context.set_input(_event_trace_input_summary(event))
                    if event.event_type == "auto_review":
                        result = await _maybe_await(handlers.run_auto_review(event))
                    elif event.event_type == "mention":
                        result = await _maybe_await(handlers.run_mention(event))
                    elif event.event_type == "sandbox_cleanup":
                        if handlers.run_sandbox_cleanup is None:
                            raise RuntimeError("sandbox_cleanup handler is not configured")
                        result = await _maybe_await(handlers.run_sandbox_cleanup(event))
                    elif event.event_type == "agent_self_evolution":
                        if handlers.run_agent_self_evolution is None:
                            raise RuntimeError("agent_self_evolution handler is not configured")
                        result = await _maybe_await(handlers.run_agent_self_evolution(event))
                    elif event.event_type == "daily_audit_evolution":
                        if handlers.run_daily_audit_evolution is None:
                            raise RuntimeError("daily_audit_evolution handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit_evolution(event))
                    elif event.event_type == "daily_audit_direction_persistence":
                        if handlers.run_daily_audit_direction_persistence is None:
                            raise RuntimeError("daily_audit_direction_persistence handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit_direction_persistence(event))
                    elif event.event_type == "daily_audit_short_term_persistence":
                        if handlers.run_daily_audit_short_term_persistence is None:
                            raise RuntimeError("daily_audit_short_term_persistence handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit_short_term_persistence(event))
                    elif event.event_type == "daily_audit_long_term_persistence":
                        if handlers.run_daily_audit_long_term_persistence is None:
                            raise RuntimeError("daily_audit_long_term_persistence handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit_long_term_persistence(event))
                    elif event.event_type == "daily_audit_skill_persistence":
                        if handlers.run_daily_audit_skill_persistence is None:
                            raise RuntimeError("daily_audit_skill_persistence handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit_skill_persistence(event))
                    else:
                        if handlers.run_daily_audit is None:
                            raise RuntimeError("daily_audit handler is not configured")
                        result = await _maybe_await(handlers.run_daily_audit(event))
                    trace_context.set_output(_event_trace_output_summary(event.event_type, result))
            except RunTerminationRequested as exc:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                await store.ack_batch(actor_key, batch)
                await store.write_run(
                    run_record.model_copy(
                        update={
                            "state": "terminated",
                            "completed_at": iso_now(),
                            "reason": exc.reason,
                        }
                    )
                )
                await _record_run_journal_event(
                    store,
                    journal_context,
                    stage_key=None,
                    event_type="run_terminated",
                    status="terminated",
                    summary=exc.reason,
                    details={
                        "requested_by": exc.requested_by,
                        "requested_at": exc.requested_at,
                    },
                )
                await store.clear_run_checkpoint(journal_context.execution_key)
                _track_run(
                    run_record,
                    None,
                    state="terminated",
                    reason=exc.reason,
                    trace_context=locals().get("trace_context"),
                )
                continue
            except Exception as exc:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                retryable = True
                if hasattr(store, "mark_inflight_failed"):
                    retryable = await store.mark_inflight_failed(
                        actor_key,
                        batch,
                        error=str(exc),
                        max_attempts=int(getattr(settings, "RUNTIME_MAX_EVENT_ATTEMPTS", 3) or 3),
                    )
                terminal_reason = None if retryable else "retry_budget_exhausted"
                await store.write_run(
                    run_record.model_copy(
                        update={
                            "state": "failed",
                            "completed_at": iso_now(),
                            "reason": terminal_reason,
                            "error": str(exc),
                        }
                    )
                )
                _track_run(
                    run_record,
                    None,
                    state="failed",
                    reason=terminal_reason,
                    error=str(exc),
                    trace_context=locals().get("trace_context"),
                )
                raise
            lease_still_owned = await store.heartbeat_lease(actor_key, worker_id, settings.MR_ACTOR_LEASE_SECONDS)
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            if not lease_still_owned:
                error = "lost actor lease before ack"
                await store.write_run(
                    run_record.model_copy(
                        update={
                            "state": "failed",
                            "completed_at": iso_now(),
                            "error": error,
                        }
                    )
                )
                _track_run(
                    run_record,
                    None,
                    state="failed",
                    reason=None,
                    error=error,
                    trace_context=trace_context,
                )
                raise RuntimeError(error)
            await store.ack_batch(actor_key, batch)
            terminal_state, terminal_reason = _run_terminal_state(result)
            await store.write_run(
                run_record.model_copy(
                    update={
                        "state": terminal_state,
                        "completed_at": iso_now(),
                        "reason": terminal_reason,
                    }
                )
            )
            _track_run(
                run_record,
                result,
                state=terminal_state,
                reason=terminal_reason,
                trace_context=trace_context,
            )
            await store.heartbeat_lease(actor_key, worker_id, settings.MR_ACTOR_LEASE_SECONDS)
        return True
    finally:
        await store.release_lease(actor_key, worker_id)
        await store.clear_actor_scheduled(actor_key)
        if await store.has_actor_events(actor_key):
            if await store.mark_actor_scheduled(actor_key):
                await queue.enqueue_job(MR_ACTOR_JOB_NAME, actor_key)


async def drain_mr_actor_job(ctx, actor_key: str):  # pragma: no cover - exercised through queue integration
    del ctx
    return await drain_mr_actor(actor_key)


async def _worker_startup(ctx):  # pragma: no cover - exercised by runtime bootstrap
    del ctx
    configure_phoenix_tracing()
    from agent.sandbox.manager import configure_runtime_sandbox_config

    configure_runtime_sandbox_config(settings.current_snapshot().model_dump())
    schedule_bot_identity_prime(logger=logger, context="worker startup")
    resumed = await resume_runtime_processing()
    if resumed:
        logger.info("Resumed %d queued runtime actor(s) from SQLite", resumed)


def _parse_daily_start_time(value: str) -> time_cls:
    try:
        hour_raw, minute_raw = (value or "02:00").split(":", 1)
        return time_cls(hour=int(hour_raw), minute=int(minute_raw))
    except Exception:
        logger.warning("Invalid DAILY_AUDIT_START_TIME_LOCAL=%r; falling back to 02:00", value)
        return time_cls(hour=2, minute=0)


def _project_agent_config(project_id: str) -> dict:
    return get_config_service().get_project_agent_config(project_id)


def _configured_target_projects() -> list[str]:
    snapshot = get_config_service().get_snapshot()
    return [str(item).strip() for item in snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]


def _self_evolution_interval_days() -> int:
    try:
        return max(int(settings.SELF_EVOLUTION_INTERVAL_DAYS or 1), 1)
    except Exception:
        return 1


def _self_evolution_time() -> time_cls:
    return _parse_daily_start_time(str(settings.SELF_EVOLUTION_TIME_LOCAL or "02:00"))


def _self_evolution_enabled() -> bool:
    return bool(settings.SELF_EVOLUTION_ENABLED)


async def maybe_enqueue_daily_audit_events(*, now: datetime | None = None, store=None, queue=None) -> int:
    target_projects = _configured_target_projects()
    if not target_projects:
        return 0

    current = now or now_in_open_review_tz()
    local_now = to_open_review_tz(current)

    store = store or await get_runtime_store()
    queue = queue or await get_job_queue()
    scheduled = 0
    event_date = local_now.date().isoformat()

    for project_id in target_projects:
        project_config = _project_agent_config(project_id)
        if not project_config.get("DAILY_AUDIT_ENABLED"):
            continue
        target_time = _parse_daily_start_time(str(project_config.get("DAILY_AUDIT_START_TIME_LOCAL") or "02:00"))
        if local_now.time().replace(second=0, microsecond=0) < target_time:
            continue
        try:
            default_branch = get_project_default_branch(project_id)
        except Exception:
            logger.warning(
                "Could not resolve default branch for %s during daily audit scheduling; using main",
                project_id,
                exc_info=True,
            )
            default_branch = "main"

        event = EventEnvelope(
            event_id=f"daily_audit:{project_id}:{event_date}",
            event_type="daily_audit",
            project_id=project_id,
            mr_iid=None,
            source_branch=default_branch,
            target_branch=default_branch,
            title=f"Daily audit {event_date}",
            received_at=to_open_review_tz(current).isoformat(),
            payload={
                "kind": "daily_audit",
                "default_branch": default_branch,
                "scheduled_date": event_date,
            },
        )
        appended = await store.append_event(event)
        if not appended:
            continue
        if await store.mark_actor_scheduled(event.actor_key):
            await queue.enqueue_job(MR_ACTOR_JOB_NAME, event.actor_key)
        scheduled += 1

    return scheduled


async def maybe_enqueue_agent_self_evolution_events(*, now: datetime | None = None, store=None, queue=None) -> int:
    target_projects = _configured_target_projects()
    if not target_projects:
        return 0

    current = now or now_in_open_review_tz()
    local_now = to_open_review_tz(current)
    if not _self_evolution_enabled():
        return 0
    if local_now.time().replace(second=0, microsecond=0) < _self_evolution_time():
        return 0

    store = store or await get_runtime_store()
    queue = queue or await get_job_queue()
    scheduled = 0
    event_date = local_now.date().isoformat()
    config_service = get_config_service()

    for project_id in target_projects:
        try:
            default_branch = get_project_default_branch(project_id)
        except Exception:
            logger.warning(
                "Could not resolve default branch for %s during self-evolution scheduling; using main",
                project_id,
                exc_info=True,
            )
            default_branch = "main"

        state = config_service.get_self_evolution_schedule_state(_SELF_EVOLUTION_SCHEDULE_AGENT_TYPE, project_id) or {}
        last_scheduled_date = str(state.get("last_scheduled_date") or "").strip()
        if last_scheduled_date:
            delta_days = (local_now.date() - datetime.fromisoformat(last_scheduled_date).date()).days
            if delta_days < _self_evolution_interval_days():
                continue

        event = EventEnvelope(
            event_id=f"agent_self_evolution:{project_id}:{event_date}",
            event_type="agent_self_evolution",
            project_id=project_id,
            mr_iid=None,
            source_branch=default_branch,
            target_branch=default_branch,
            title=f"Agent self evolution {event_date}",
            received_at=to_open_review_tz(current).isoformat(),
            payload={
                "kind": "agent_self_evolution",
                "agent_type": _SELF_EVOLUTION_SCHEDULE_AGENT_TYPE,
                "agent_types": list(_SELF_EVOLUTION_AGENT_TYPES),
                "default_branch": default_branch,
                "scheduled_date": event_date,
                "trigger_source": "scheduled",
            },
        )
        appended = await store.append_event(event)
        if not appended:
            continue
        config_service.record_self_evolution_schedule(
            agent_type=_SELF_EVOLUTION_SCHEDULE_AGENT_TYPE,
            project_id=project_id,
            scheduled_date=event_date,
        )
        if await store.mark_actor_scheduled(event.actor_key):
            await queue.enqueue_job(MR_ACTOR_JOB_NAME, event.actor_key)
        scheduled += 1

    return scheduled


async def run_sqlite_worker_forever(poll_interval_seconds: float = 1.0) -> None:
    """Standalone single-host worker loop for SQLite-backed runtime mode."""
    await _worker_startup(None)
    logger.info("Starting standalone SQLite worker loop interval=%ss", poll_interval_seconds)
    while True:
        try:
            await resume_runtime_processing()
            await maybe_enqueue_daily_audit_events()
            await maybe_enqueue_agent_self_evolution_events()
        except Exception:
            logger.exception("SQLite worker loop iteration failed")
        await asyncio.sleep(poll_interval_seconds)
def main() -> None:  # pragma: no cover - exercised by container/runtime startup
    os.environ.setdefault("OPEN_REVIEW_RUNTIME_ROLE", "worker")
    asyncio.run(run_sqlite_worker_forever())


if __name__ == "__main__":  # pragma: no cover - exercised by runtime startup
    main()
