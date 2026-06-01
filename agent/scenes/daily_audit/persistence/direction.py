"""Async direction archive persistence for daily audit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from deepagents import create_deep_agent

from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.runtime.queue import get_runtime_store
from agent.scenes.daily_audit.models import DailyAuditSelectionResponse
from agent.scenes.daily_audit.persistence.raw_records import append_daily_audit_agent_record
from agent.scenes.daily_audit.persistence.store import get_daily_audit_persistence_store
from agent.utils.model import make_model


def build_write_direction_archive_tool(
    *,
    project_id: str,
    run_id: str,
    store,
    selection_payload: dict[str, Any],
):
    selection = DailyAuditSelectionResponse.model_validate(selection_payload)
    unit = selection.selected_unit

    def write_direction_archive(archive_brief: str, archive_keywords: list[str]) -> dict[str, object]:
        """Persist the final direction archive for this run."""

        brief = archive_brief.strip()
        keywords = [str(item).strip() for item in archive_keywords if str(item).strip()]
        if not brief:
            return {"success": False, "error": "archive_brief is required"}
        if not keywords:
            return {"success": False, "error": "archive_keywords is required"}
        store.record_direction_archive(
            project_id,
            run_id=run_id,
            unit_type=unit.unit_type,
            unit_label=unit.label,
            file_path=unit.file_path,
            entrypoint_kind=unit.entrypoint_kind,
            entrypoint_symbol=unit.entrypoint_symbol,
            workflow_summary=unit.workflow_summary or unit.label,
            selection_reasoning=selection.selection_reasoning,
            direction_brief=brief,
            keywords=keywords,
            metadata={
                "entry_evidence": list(unit.entry_evidence),
                "used_subagents": list(selection.used_subagents),
            },
        )
        return {"success": True, "run_id": run_id, "unit_label": unit.label}

    write_direction_archive.__name__ = "write_direction_archive"
    return write_direction_archive


def _direction_persistence_prompt(selection: DailyAuditSelectionResponse) -> str:
    unit = selection.selected_unit
    return (
        "You are the Daily Audit direction persistence agent.\n\n"
        "Your only job is to persist one high-density direction archive record for this run.\n\n"
        "Rules:\n"
        "- Do not respond conversationally.\n"
        "- Call `write_direction_archive` exactly once.\n"
        "- `archive_brief` must be concise but information-dense.\n"
        "- `archive_keywords` must be semantic phrases, not raw token dumps.\n"
        "- Do not invent facts beyond the provided selection payload.\n\n"
        f"Selected workflow: {unit.label}\n"
        f"Entrypoint kind: {unit.entrypoint_kind or '(unknown)'}\n"
        f"Entrypoint symbol: {unit.entrypoint_symbol or '(unknown)'}\n"
        f"Workflow summary: {unit.workflow_summary or unit.label}\n"
        f"Selection reasoning: {selection.selection_reasoning or '(none)'}\n"
        f"Entry evidence:\n" + ("\n".join(f"- {item}" for item in unit.entry_evidence) if unit.entry_evidence else "- (none)")
    )


async def run_daily_audit_direction_persistence(
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
    selection_payload = payload.get("selection")
    if not isinstance(selection_payload, dict):
        raise RuntimeError("daily_audit_direction_persistence event missing selection payload")

    selection = DailyAuditSelectionResponse.model_validate(selection_payload)
    store = get_daily_audit_persistence_store()
    tool = build_write_direction_archive_tool(
        project_id=project_id,
        run_id=str(payload.get("run_id") or ""),
        store=store,
        selection_payload=selection_payload,
    )
    writes: list[dict[str, object]] = []

    def _wrapped_write_direction_archive(archive_brief: str, archive_keywords: list[str]) -> dict[str, object]:
        result = tool(archive_brief, archive_keywords)
        writes.append(result)
        return result

    _wrapped_write_direction_archive.__name__ = "write_direction_archive"
    _wrapped_write_direction_archive.__doc__ = tool.__doc__
    agent = create_deep_agent(
        model=make_model(None, temperature=0, max_tokens=4_000),
        tools=[_wrapped_write_direction_archive],
        system_prompt=_direction_persistence_prompt(selection),
        middleware=[StructuredOutputRetryMiddleware(), ModelRetryMiddleware(), ToolErrorMiddleware()],
    )
    input_messages = [
        {
            "role": "user",
            "content": "Persist the direction archive for this run.",
        }
    ]
    thread_id = f"{payload.get('session_id')}:direction-persistence"
    result = await agent.ainvoke(
        {"messages": input_messages},
        config={"configurable": {"project_id": project_id, "thread_id": thread_id}},
    )
    target_runtime_run_id = parent_runtime_run_id or str(runtime_run_id or "")
    if target_runtime_run_id:
        append_daily_audit_agent_record(
            runtime_run_id=target_runtime_run_id,
            logical_run_id=str(payload.get("run_id") or ""),
            record_kind="daily_audit.direction_persistence",
            thread_id=thread_id,
            system_prompt=_direction_persistence_prompt(selection),
            input_messages_json=input_messages,
            messages_json=result.get("messages", input_messages) if isinstance(result, dict) else input_messages,
            result_json=result,
            started_at=None,
            completed_at=None,
            metadata_json={
                "stage": "direction_persistence",
                "unit_label": selection.selected_unit.label,
                "file_path": selection.selected_unit.file_path,
            },
        )
    if not writes:
        return SimpleNamespace(status="failed", reason="direction_archive_not_written", run_id=payload.get("run_id"))
    return SimpleNamespace(
        status="persisted",
        reason="direction_archive_written",
        run_id=payload.get("run_id"),
        unit_label=selection.selected_unit.label,
    )
