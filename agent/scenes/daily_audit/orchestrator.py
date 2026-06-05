"""Agent-driven daily audit workflow."""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
from contextvars import ContextVar
from typing import Any

from agent.config import settings
from agent.controlplane import get_tracking_service
from agent.gitlab.project_ops import (
    create_project_issue,
    create_project_merge_request,
)
from agent.observability import start_open_review_span
from agent.runtime.termination import raise_if_run_termination_requested
from agent.sandbox.manager import (
    cleanup_temporary_worktree,
    commit_all_and_get_sha,
    create_temporary_worktree,
    push_branch_head,
)
from agent.scenes.auto_review.orchestrator import _git
from agent.scenes.daily_audit.graph import build_daily_audit_agent
from agent.scenes.daily_audit.models import (
    DailyAuditAgentResponse,
    DailyAuditContext,
    DailyAuditRunResult,
    DailyAuditSelectionResponse,
)
from agent.scenes.daily_audit.runtime.deepagents import (
    daily_audit_session_id,
)
from agent.utils.gitlab_project_targets import build_gitlab_merge_request_url
from agent.utils.timezone import compact_timestamp

logger = logging.getLogger(__name__)
_DAILY_AUDIT_AGENT_CONFIG: ContextVar[dict[str, Any] | None] = ContextVar(
    "open_review_daily_audit_agent_config",
    default=None,
)


def _load_daily_audit_agent_config(project_id: str) -> dict[str, Any]:
    try:
        from agent.controlplane import get_config_service

        return get_config_service().get_project_agent_config(project_id)
    except Exception:
        logger.warning("Could not load daily-audit project config for %s", project_id, exc_info=True)
        return {}


def _daily_audit_setting(key: str) -> Any:
    config = _DAILY_AUDIT_AGENT_CONFIG.get()
    if config is not None and key in config:
        return config.get(key)
    return getattr(settings, key)


def _build_run_id(event_id: str) -> str:
    timestamp = compact_timestamp()
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:8]
    return f"{timestamp}-{digest}"


def _sandbox_root_dir(sandbox) -> str:
    root_dir = getattr(sandbox, "root_dir", None)
    if root_dir:
        return str(root_dir)
    cwd = getattr(sandbox, "cwd", None)
    if cwd:
        return str(cwd)
    return "/workspace"


def _experiment_root(sandbox, run_id: str) -> str:
    return f"{_sandbox_root_dir(sandbox).rstrip('/')}/.open-review-daily-audit/{run_id}"


def _prepare_experiment_root(sandbox, run_id: str) -> str:
    experiment_root = _experiment_root(sandbox, run_id)
    if hasattr(sandbox, "execute"):
        sandbox.execute(f"mkdir -p {shlex.quote(experiment_root)}", timeout=30)
    return experiment_root


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "daily-audit"


def _scoped_autofix_branch_name(*, requested_branch: str | None, selected_label: str, run_id: str) -> str:
    base_branch = (requested_branch or f"open-review/daily-audit/{_slugify(selected_label)}").strip().strip("/")
    base_branch = base_branch or "open-review/daily-audit/daily-audit"
    suffix = f"/{run_id}"
    if base_branch.endswith(suffix):
        return base_branch
    return f"{base_branch}{suffix}"


def _coerce_agent_response(payload) -> DailyAuditAgentResponse:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected daily audit payload type: {type(payload).__name__}")
    structured = payload.get("structured_response", payload)
    if isinstance(structured, DailyAuditSelectionResponse):
        raise RuntimeError("daily audit analysis received direction-stage structured response")
    if (
        isinstance(structured, dict)
        and "selected_unit" in structured
        and "selection_reasoning" in structured
        and "summary_markdown" not in structured
        and "report_markdown" not in structured
    ):
        raise RuntimeError("daily audit analysis received direction-stage structured response")
    return DailyAuditAgentResponse.model_validate(structured)


def _extract_used_subagents(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []
    structured = payload.get("structured_response", payload)
    try:
        response = DailyAuditAgentResponse.model_validate(structured)
    except Exception:
        return []
    return list(response.used_subagents)


def _coerce_selection_response(payload) -> DailyAuditSelectionResponse:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected daily audit selection payload type: {type(payload).__name__}")
    structured = payload.get("structured_response", payload)
    return DailyAuditSelectionResponse.model_validate(structured)


def _stage_thread_id(context: DailyAuditContext, stage_tag: str) -> str:
    if stage_tag == "direction":
        return daily_audit_session_id(context.project_id, context.run_id, role="direction")
    return context.session_id


def build_daily_audit_context(
    *,
    project_id: str,
    repo_dir: str,
    default_branch: str,
    event,
    sandbox=None,
) -> DailyAuditContext:
    run_id = _build_run_id(getattr(event, "event_id", "daily-audit"))
    return DailyAuditContext(
        project_id=project_id,
        actor_key=f"{project_id}!daily_audit",
        repo_dir=repo_dir,
        default_branch=default_branch,
        run_id=run_id,
        session_id=daily_audit_session_id(project_id, run_id),
        experiment_root=_prepare_experiment_root(sandbox, run_id) if sandbox is not None else "",
        candidates=[],
    )


def _working_tree_has_changes(sandbox, repo_dir: str) -> bool:
    result = _git(repo_dir, "status", "--porcelain", sandbox=sandbox)
    return result.returncode == 0 and bool(result.stdout.strip())


def _collect_changed_paths(sandbox, repo_dir: str) -> list[str]:
    result = _git(repo_dir, "diff", "--name-only", sandbox=sandbox)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _count_changed_lines(sandbox, repo_dir: str) -> int:
    result = _git(repo_dir, "diff", "--numstat", sandbox=sandbox)
    if result.returncode != 0:
        return 0
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        try:
            added = 0 if parts[0] == "-" else int(parts[0])
            removed = 0 if parts[1] == "-" else int(parts[1])
        except ValueError:
            continue
        total += added + removed
    return total


def _is_safe_autofix(*, used_subagents: list[str], changed_files: list[str], changed_line_count: int) -> tuple[bool, str | None]:
    del used_subagents
    if not _daily_audit_setting("DAILY_AUDIT_ENABLE_AUTOFIX"):
        return False, "autofix_disabled"
    if not changed_files:
        return False, "no_file_changes"
    max_changed_files = int(_daily_audit_setting("DAILY_AUDIT_MAX_CHANGED_FILES") or 0)
    if max_changed_files > 0 and len(changed_files) > max_changed_files:
        return False, "changed_file_limit_exceeded"
    max_changed_lines = int(_daily_audit_setting("DAILY_AUDIT_MAX_CHANGED_LINES") or 0)
    if max_changed_lines > 0 and changed_line_count > max_changed_lines:
        return False, "changed_line_limit_exceeded"
    return True, None


def _issue_title_prefix() -> str:
    configured = str(_daily_audit_setting("DAILY_AUDIT_ROLLING_ISSUE_TITLE") or "").strip()
    if not configured or configured in {"Open Review Daily Audit Findings", "Open Review 日常审计问题汇总"}:
        return "Open Review 日常审计"
    return configured


def _issue_title(workflow_label: str) -> str:
    prefix = _issue_title_prefix().rstrip("：: ")
    label = workflow_label.strip() or "未命名工作流"
    return f"{prefix}：{label}"


def _issue_description(context: DailyAuditContext, response: DailyAuditAgentResponse) -> str:
    lines = [
        f"## 日常审计运行 `{context.run_id}`",
        f"- 默认分支：`{context.default_branch}`",
        f"- 选定工作流：`{response.selected_unit.unit_type}` `{response.selected_unit.label}`",
        "",
        response.report_markdown.strip() or response.summary_markdown.strip() or "(无报告正文)",
    ]
    return "\n".join(lines).strip()


def _record_daily_audit_published_issue(
    *,
    runtime_run_id: str | None,
    context: DailyAuditContext,
    issue_iid: int,
    title: str,
    description: str,
) -> None:
    if not runtime_run_id:
        return
    tracking = get_tracking_service()
    tracking.set_published_issue_iid(runtime_run_id, issue_iid)
    tracking.append_published_object(
        runtime_run_id,
        {
            "channel": "issue",
            "object_kind": "issue",
            "issue_iid": issue_iid,
            "external_id": str(issue_iid),
            "body_snapshot": description,
            "title": title,
            "marker_map": {"open-review-daily-audit-run": context.run_id},
        },
    )


def _record_daily_audit_published_merge_request(
    *,
    runtime_run_id: str | None,
    context: DailyAuditContext,
    merge_request_iid: int,
    title: str,
    description: str,
) -> None:
    if not runtime_run_id:
        return
    tracking = get_tracking_service()
    tracking.set_published_merge_request_iid(runtime_run_id, merge_request_iid)
    tracking.append_published_object(
        runtime_run_id,
        {
            "channel": "merge_request",
            "object_kind": "merge_request",
            "merge_request_iid": merge_request_iid,
            "external_id": str(merge_request_iid),
            "body_snapshot": description,
            "title": title,
            "marker_map": {"open-review-daily-audit-run": context.run_id},
        },
    )


async def _invoke_daily_audit_stage(
    *,
    span_name: str,
    context: DailyAuditContext,
    project_id: str,
    repo_dir: str,
    message: str,
    agent,
    stage_tag: str,
    thread_id: str,
) -> dict:
    with start_open_review_span(
        span_name,
        session_id=context.session_id,
        attributes={
            "open_review.project_id": project_id,
            "open_review.run_id": context.run_id,
            "open_review.session_id": context.session_id,
            "open_review.model_id": settings.LLM_MODEL_ID,
        },
        tags=["daily_audit", stage_tag],
        span_kind="agent",
    ) as trace_ctx:
        trace_ctx.set_input({"messages": [{"role": "user", "content": message}]})
        try:
            payload = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": message,
                        }
                    ]
                },
                config={
                    "configurable": {
                        "project_id": project_id,
                        "repo_dir": repo_dir,
                        "thread_id": thread_id,
                    }
                },
            )
        except Exception as exc:
            trace_ctx.record_exception(exc)
            trace_ctx.set_error_status(str(exc))
            trace_ctx.add_event("invoke_failed", {"error_type": exc.__class__.__name__})
            raise
        trace_ctx.add_event(
            "invoke_completed",
            {
                "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
                "structured_response_present": isinstance(payload, dict)
                and payload.get("structured_response") is not None,
            },
        )
        trace_ctx.set_output(payload)
        return payload


async def run_daily_audit(
    *,
    project_id: str,
    repo_dir: str,
    sandbox,
    default_branch: str,
    publish_service=None,
    event,
    runtime_run_id: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> DailyAuditRunResult:
    config = dict(agent_config or _load_daily_audit_agent_config(project_id))
    token = _DAILY_AUDIT_AGENT_CONFIG.set(config)
    try:
        return await _run_daily_audit_inner(
            project_id=project_id,
            repo_dir=repo_dir,
            sandbox=sandbox,
            default_branch=default_branch,
            publish_service=publish_service,
            event=event,
            runtime_run_id=runtime_run_id,
        )
    finally:
        _DAILY_AUDIT_AGENT_CONFIG.reset(token)


async def _run_daily_audit_inner(
    *,
    project_id: str,
    repo_dir: str,
    sandbox,
    default_branch: str,
    publish_service=None,
    event,
    runtime_run_id: str | None = None,
) -> DailyAuditRunResult:
    del publish_service
    await raise_if_run_termination_requested(
        run_id=runtime_run_id,
        actor_key=f"{project_id}!daily_audit",
    )
    context = build_daily_audit_context(
        project_id=project_id,
        repo_dir=repo_dir,
        default_branch=default_branch,
        event=event,
        sandbox=sandbox,
    )
    worktree_dir: str | None = None
    try:
        worktree_dir = create_temporary_worktree(
            sandbox,
            repo_dir=repo_dir,
            head_sha=f"origin/{default_branch}",
            run_id=context.run_id,
        )
        context = context.model_copy(update={"repo_dir": worktree_dir})
        with start_open_review_span(
            "open_review.daily_audit.execute.main",
            attributes={
                "open_review.project_id": context.project_id,
                "open_review.run_id": context.run_id,
                "open_review.actor_key": context.actor_key,
            },
            tags=["daily_audit", "main-agent"],
        ):
            selection_context = context
            selection_message = (
                "Discover one user-triggered action workflow for today's audit. "
                "Explore the repository yourself and return only the selected workflow plus the reasoning and entry evidence for that choice."
            )
            selector_agent = build_daily_audit_agent(
                sandbox=sandbox,
                repo_dir=worktree_dir,
                context=selection_context,
                response_format=DailyAuditSelectionResponse,
                stage="direction",
                runtime_run_id=runtime_run_id,
            )
            selection_payload = await _invoke_daily_audit_stage(
                span_name="open_review.daily_audit.direction",
                context=context,
                project_id=project_id,
                repo_dir=worktree_dir,
                message=selection_message,
                agent=selector_agent,
                stage_tag="direction",
                thread_id=_stage_thread_id(context, "direction"),
            )
            selection = _coerce_selection_response(selection_payload)
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=context.actor_key,
            )
            analysis_context = context.model_copy(
                update={
                    "selected_unit": selection.selected_unit,
                }
            )
            analysis_message = "Analyze the selected user-triggered workflow using the targeted recall. Return a bounded report or autofix recommendation for that workflow only."
            agent = build_daily_audit_agent(
                sandbox=sandbox,
                repo_dir=worktree_dir,
                context=analysis_context,
                stage="analysis",
                runtime_run_id=runtime_run_id,
            )
            payload = await _invoke_daily_audit_stage(
                span_name="open_review.daily_audit.analysis",
                context=context,
                project_id=project_id,
                repo_dir=worktree_dir,
                message=analysis_message,
                agent=agent,
                stage_tag="analysis",
                thread_id=_stage_thread_id(context, "analysis"),
            )
            response = _coerce_agent_response(payload)
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=context.actor_key,
            )
            used_subagents = sorted(set(selection.used_subagents) | set(_extract_used_subagents(payload)))
            result = DailyAuditRunResult(
                status="reported",
                reason="reported",
                unit_type=response.selected_unit.unit_type,
                unit_label=response.selected_unit.label,
                experiment_root=context.experiment_root,
                finding_count=len(response.findings),
                used_subagents=used_subagents,
            )
            if response.recommended_action != "autofix":
                await raise_if_run_termination_requested(
                    run_id=runtime_run_id,
                    actor_key=context.actor_key,
                )
                issue_title = _issue_title(response.selected_unit.label)
                issue_description = _issue_description(context, response)
                issue_iid = create_project_issue(
                    project_id,
                    title=issue_title,
                    description=issue_description,
                )
                _record_daily_audit_published_issue(
                    runtime_run_id=runtime_run_id,
                    context=context,
                    issue_iid=issue_iid,
                    title=issue_title,
                    description=issue_description,
                )
                result.issue_iid = issue_iid
                return result

            if not _working_tree_has_changes(sandbox, worktree_dir):
                result.degraded_reason = "no_file_changes"
                await raise_if_run_termination_requested(
                    run_id=runtime_run_id,
                    actor_key=context.actor_key,
                )
                issue_title = _issue_title(response.selected_unit.label)
                issue_description = _issue_description(context, response)
                issue_iid = create_project_issue(
                    project_id,
                    title=issue_title,
                    description=issue_description,
                )
                _record_daily_audit_published_issue(
                    runtime_run_id=runtime_run_id,
                    context=context,
                    issue_iid=issue_iid,
                    title=issue_title,
                    description=issue_description,
                )
                result.issue_iid = issue_iid
                return result

            changed_files = _collect_changed_paths(sandbox, worktree_dir)
            changed_line_count = _count_changed_lines(sandbox, worktree_dir)
            result.changed_files = changed_files
            result.changed_line_count = changed_line_count

            safe, degraded_reason = _is_safe_autofix(
                used_subagents=used_subagents,
                changed_files=changed_files,
                changed_line_count=changed_line_count,
            )
            if not safe:
                result.degraded_reason = degraded_reason
                await raise_if_run_termination_requested(
                    run_id=runtime_run_id,
                    actor_key=context.actor_key,
                )
                issue_title = _issue_title(response.selected_unit.label)
                issue_description = _issue_description(context, response)
                issue_iid = create_project_issue(
                    project_id,
                    title=issue_title,
                    description=issue_description,
                )
                _record_daily_audit_published_issue(
                    runtime_run_id=runtime_run_id,
                    context=context,
                    issue_iid=issue_iid,
                    title=issue_title,
                    description=issue_description,
                )
                result.issue_iid = issue_iid
                return result

            branch_name = _scoped_autofix_branch_name(
                requested_branch=response.branch_name,
                selected_label=response.selected_unit.label,
                run_id=context.run_id,
            )
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=context.actor_key,
            )
            commit_sha = commit_all_and_get_sha(
                worktree_dir=worktree_dir,
                message=response.commit_message or f"fix: daily audit {response.selected_unit.label}",
                sandbox=sandbox,
            )
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=context.actor_key,
            )
            push_branch_head(
                project_id=project_id,
                worktree_dir=worktree_dir,
                source_branch=branch_name,
                sandbox=sandbox,
            )
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=context.actor_key,
            )
            merge_request_title = response.merge_request_title or f"日常审计：{response.selected_unit.label}"
            merge_request_description = (
                response.merge_request_description
                or response.report_markdown
                or response.summary_markdown
            )
            merge_request = create_project_merge_request(
                project_id,
                source_branch=branch_name,
                target_branch=default_branch,
                title=merge_request_title,
                description=merge_request_description,
                draft=True,
            )
            result.status = "merge_request_opened"
            result.reason = "merge_request_opened"
            result.commit_sha = commit_sha
            result.merge_request_iid = getattr(merge_request, "iid", None)
            result.merge_request_url = (
                build_gitlab_merge_request_url(
                    project_id,
                    result.merge_request_iid,
                    external_url=settings.GITLAB_EXTERNAL_URL,
                )
                if result.merge_request_iid is not None
                else None
            ) or getattr(merge_request, "web_url", None)
            if result.merge_request_iid is not None:
                _record_daily_audit_published_merge_request(
                    runtime_run_id=runtime_run_id,
                    context=context,
                    merge_request_iid=result.merge_request_iid,
                    title=merge_request_title,
                    description=merge_request_description,
                )
            return result
    finally:
        if worktree_dir:
            cleanup_temporary_worktree(sandbox=sandbox, repo_dir=repo_dir, worktree_dir=worktree_dir)
