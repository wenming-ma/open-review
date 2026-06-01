"""Background review and flush helpers for daily audit skill persistence."""

from __future__ import annotations

from types import SimpleNamespace

from deepagents import create_deep_agent

from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.observability import start_open_review_span
from agent.runtime.queue import get_runtime_store
from agent.scenes.daily_audit.persistence.raw_records import append_daily_audit_agent_record
from agent.scenes.daily_audit.persistence.store import get_daily_audit_persistence_store
from agent.scenes.daily_audit.runtime.deepagents import daily_audit_session_id
from agent.scenes.daily_audit.selfevolution.tools import build_skill_tools
from agent.utils.model import make_model


def _review_prompt(*, flush: bool) -> str:
    purpose = (
        "This session is ending. Review the transcript and save reusable skills before context is lost."
        if flush
        else "Review the transcript for reusable skills worth saving or patching."
    )
    return (
        "You are the Daily Audit skill persistence reviewer.\n\n"
        f"{purpose}\n\n"
        "Your job is to preserve only generic, high-value workflow knowledge and avoid adding noise.\n\n"
        "Rules:\n"
        "- Do not respond conversationally.\n"
        "- Only use skills_list, skill_view, and skill_manage when a reusable procedure is genuinely worth saving.\n"
        "- A saved skill must capture a generic, high-value, cross-project or cross-session workflow that will likely help future audits.\n"
        "- Prefer durable methods, decision rules, investigation playbooks, and verification workflows.\n"
        "- Do not save one-off project facts, repo-specific paths, temporary bug details, issue-specific conclusions, or low-signal notes.\n"
        "- Do not create noisy or redundant skills; patch an existing skill instead when the lesson fits there.\n"
        "- When unsure, skip saving.\n"
        "- Do not try to write memory.\n"
        "- If nothing should be saved, do nothing.\n"
    )


def _review_message(transcript_text: str) -> str:
    return (
        "Review this daily-audit session transcript.\n\n"
        f"SESSION TRANSCRIPT:\n{transcript_text}\n\n"
        "Only save a skill when it captures a generic, reusable workflow with clear future value. "
        "Do not save one-off project facts, repo-specific bug details, or low-signal notes. "
        "When unsure, skip saving. "
        "Do not answer the user. Use tools only if a reusable skill should be saved."
    )


async def run_daily_audit_skill_review(
    *,
    project_id: str,
    run_id: str,
    repo_dir: str,
    transcript_text: str,
    flush: bool = False,
) -> object | None:
    if not transcript_text.strip():
        return
    model = make_model(None, temperature=0, max_tokens=8_000)
    skills_list_tool, skill_view_tool, skill_manage_tool = build_skill_tools(repo_dir=repo_dir)
    session_id = daily_audit_session_id(project_id, run_id, role="skill-persistence")
    agent = create_deep_agent(
        model=model,
        tools=[skills_list_tool, skill_view_tool, skill_manage_tool],
        system_prompt=_review_prompt(flush=flush),
        middleware=[StructuredOutputRetryMiddleware(), ModelRetryMiddleware(), ToolErrorMiddleware()],
    )
    review_message = _review_message(transcript_text)
    with start_open_review_span(
        "open_review.daily_audit.skill_persistence",
        session_id=session_id,
        attributes={
            "open_review.project_id": project_id,
            "open_review.session_id": session_id,
            "open_review.flush": flush,
        },
        metadata={"transcript_chars": len(transcript_text)},
        tags=["daily_audit", "skill-persistence"],
        span_kind="agent",
    ) as trace_ctx:
        trace_ctx.set_input({"messages": [{"role": "user", "content": review_message}]})
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": review_message}]},
            config={"configurable": {"project_id": project_id, "repo_dir": repo_dir, "thread_id": session_id}},
        )
        trace_ctx.add_event(
            "invoke_completed",
            {
                "payload_keys": sorted(result.keys()) if isinstance(result, dict) else None,
                "structured_response_present": isinstance(result, dict)
                and result.get("structured_response") is not None,
            },
        )
        trace_ctx.set_output(result)
    return result


async def run_daily_audit_skill_persistence(
    *,
    project_id: str,
    default_branch: str,
    event,
    runtime_run_id: str | None = None,
) -> object:
    del default_branch
    payload = event.payload if isinstance(event.payload, dict) else {}
    parent_runtime_run_id = str(payload.get("parent_runtime_run_id") or "")
    if parent_runtime_run_id:
        request = await (await get_runtime_store()).get_run_termination(parent_runtime_run_id)
        if request is not None:
            return SimpleNamespace(
                status="terminated",
                reason="parent_run_terminated",
                run_id=str(payload.get("run_id") or ""),
            )
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise RuntimeError("daily_audit_skill_persistence event missing run_id")
    repo_dir = str(payload.get("repo_dir") or "")
    if not repo_dir:
        raise RuntimeError("daily_audit_skill_persistence event missing repo_dir")

    store = get_daily_audit_persistence_store()
    transcript = store.get_run_transcript(project_id, run_id)
    transcript_text = str((transcript or {}).get("content") or "").strip()
    if not transcript_text:
        return SimpleNamespace(status="failed", reason="transcript_not_found", run_id=run_id)

    flush = bool(payload.get("flush", True))
    session_id = daily_audit_session_id(project_id, run_id, role="skill-persistence")
    review_message = _review_message(transcript_text)
    review_result = await run_daily_audit_skill_review(
        project_id=project_id,
        run_id=run_id,
        repo_dir=repo_dir,
        transcript_text=transcript_text,
        flush=flush,
    )
    target_runtime_run_id = parent_runtime_run_id or str(runtime_run_id or "")
    if target_runtime_run_id:
        append_daily_audit_agent_record(
            runtime_run_id=target_runtime_run_id,
            logical_run_id=run_id,
            record_kind="daily_audit.skill_persistence",
            thread_id=session_id,
            system_prompt=_review_prompt(flush=flush),
            input_messages_json=[{"role": "user", "content": review_message}],
            messages_json=[{"role": "user", "content": review_message}],
            result_json=review_result or {"status": "reviewed"},
            started_at=None,
            completed_at=None,
            metadata_json={"stage": "skill_persistence", "repo_dir": repo_dir},
        )
    return SimpleNamespace(
        status="reviewed",
        reason="skill_persistence_completed",
        run_id=run_id,
    )
