"""Async short-term summary persistence for daily audit."""

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
from agent.utils.model import make_model


def build_write_short_term_summary_tool(*, project_id: str, run_id: str, store):
    def write_short_term_summary(summary: str) -> dict[str, object]:
        """Persist the short-term continuity summary for this run."""

        text = summary.strip()
        if not text:
            return {"success": False, "error": "summary is required"}
        store.upsert_short_term_summary(project_id, "primary", text)
        return {"success": True, "run_id": run_id, "summary": text}

    write_short_term_summary.__name__ = "write_short_term_summary"
    return write_short_term_summary


def _prompt(*, transcript_text: str) -> str:
    return (
        "You are the Daily Audit short-term persistence agent.\n\n"
        "Your only job is to write one short continuity baton for the next run.\n\n"
        "Rules:\n"
        "- Do not respond conversationally.\n"
        "- Call `write_short_term_summary` exactly once.\n"
        "- The summary must be concise, action-oriented, and describe what should be resumed next.\n"
        "- Focus on the current workflow and main issue, not general background.\n"
        "- Do not invent facts beyond the transcript.\n\n"
        f"SESSION TRANSCRIPT:\n{transcript_text}\n"
    )


async def run_daily_audit_short_term_persistence(
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
                run_id=payload.get("run_id"),
            )

    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise RuntimeError("daily_audit_short_term_persistence event missing run_id")

    store = get_daily_audit_persistence_store()
    transcript = store.get_run_transcript(project_id, run_id)
    transcript_text = str((transcript or {}).get("content") or "").strip()
    if not transcript_text:
        return SimpleNamespace(status="failed", reason="transcript_not_found", run_id=run_id)

    tool = build_write_short_term_summary_tool(project_id=project_id, run_id=run_id, store=store)
    writes: list[dict[str, object]] = []

    def _wrapped_write_short_term_summary(summary: str) -> dict[str, object]:
        result = tool(summary)
        writes.append(result)
        return result

    _wrapped_write_short_term_summary.__name__ = "write_short_term_summary"
    _wrapped_write_short_term_summary.__doc__ = tool.__doc__
    session_id = daily_audit_session_id(project_id, run_id, role="short-term-persistence")
    system_prompt = _prompt(transcript_text=transcript_text)
    input_messages = [{"role": "user", "content": "Persist the short-term summary for this run."}]
    agent = create_deep_agent(
        model=make_model(None, temperature=0, max_tokens=2_000),
        tools=[_wrapped_write_short_term_summary],
        system_prompt=system_prompt,
        middleware=[StructuredOutputRetryMiddleware(), ModelRetryMiddleware(), ToolErrorMiddleware()],
    )
    with start_open_review_span(
        "open_review.daily_audit.short_term_persistence",
        session_id=session_id,
        attributes={
            "open_review.project_id": project_id,
            "open_review.session_id": session_id,
        },
        metadata={"transcript_chars": len(transcript_text)},
        tags=["daily_audit", "short-term-persistence"],
        span_kind="agent",
    ) as trace_ctx:
        trace_ctx.set_input({"messages": input_messages})
        result = await agent.ainvoke(
            {"messages": input_messages},
            config={"configurable": {"project_id": project_id, "thread_id": session_id}},
        )
        trace_ctx.set_output(result)
    target_runtime_run_id = parent_runtime_run_id or str(runtime_run_id or "")
    if target_runtime_run_id:
        append_daily_audit_agent_record(
            runtime_run_id=target_runtime_run_id,
            logical_run_id=run_id,
            record_kind="daily_audit.short_term_persistence",
            thread_id=session_id,
            system_prompt=system_prompt,
            input_messages_json=input_messages,
            messages_json=result.get("messages", input_messages) if isinstance(result, dict) else input_messages,
            result_json=result,
            started_at=None,
            completed_at=None,
            metadata_json={"stage": "short_term_persistence"},
        )
    if not writes:
        return SimpleNamespace(status="failed", reason="short_term_not_written", run_id=run_id)
    return SimpleNamespace(status="persisted", reason="short_term_written", run_id=run_id)
