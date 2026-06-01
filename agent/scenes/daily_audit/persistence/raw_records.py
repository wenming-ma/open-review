"""Raw daily-audit record helpers backed by tracked_runs."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.config import settings
from agent.controlplane import get_tracking_service


def _db_path() -> Path:
    return Path(settings.OPEN_REVIEW_DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _normalize_role(value: str | None) -> str:
    role = str(value or "message").strip().lower()
    if role in {"human", "user"}:
        return "user"
    if role in {"assistant", "ai"}:
        return "assistant"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"
    return role or "message"


def serialize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _jsonable(message.content)}
    if isinstance(message, AIMessage):
        payload = {"role": "assistant", "content": _jsonable(message.content)}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            payload["tool_calls"] = _jsonable(tool_calls)
        return payload
    if isinstance(message, ToolMessage):
        payload = {
            "role": "tool",
            "content": _jsonable(message.content),
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        if getattr(message, "name", None):
            payload["name"] = message.name
        return payload
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _jsonable(message.content)}
    if isinstance(message, dict):
        payload = dict(_jsonable(message))
        payload["role"] = _normalize_role(payload.get("role") or payload.get("type"))
        if "content" not in payload:
            payload["content"] = ""
        return payload
    return {"role": _normalize_role(type(message).__name__), "content": str(message)}


def serialize_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    return [serialize_message(message) for message in messages]


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [render_message_content(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return render_message_content(content.get("text"))
        if content.get("type") == "tool_use":
            name = str(content.get("name") or "tool").strip()
            args = _json_text(content.get("input") or {})
            return f"[tool_use] {name} {args}".strip()
        if content.get("type") == "tool_result":
            name = str(content.get("name") or "tool").strip()
            rendered = render_message_content(content.get("content"))
            return f"[tool_result] {name}\n{rendered}".strip()
        for key in ("text", "content", "thinking"):
            if key in content:
                rendered = render_message_content(content.get(key))
                if rendered:
                    return rendered
        return _json_text(content)
    return str(content).strip()


def render_messages_transcript(messages: Iterable[dict[str, Any]]) -> str:
    sections: list[str] = []
    for message in messages:
        role = _normalize_role(message.get("role"))
        header = {
            "user": "User",
            "assistant": "Assistant",
            "tool": f"Tool Result ({message.get('name')})".strip().rstrip("() "),
            "system": "System",
        }.get(role, role.title() or "Message")
        content = render_message_content(message.get("content"))
        if not content:
            continue
        sections.append(f"## {header}\n{content}")
    return "\n\n".join(sections).strip()


def append_daily_audit_agent_record(
    *,
    runtime_run_id: str,
    logical_run_id: str,
    record_kind: str,
    thread_id: str,
    system_prompt: str,
    input_messages_json: list[dict[str, Any]],
    messages_json: list[dict[str, Any]],
    result_json: Any,
    started_at: str | None,
    completed_at: str | None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    metadata = dict(metadata_json or {})
    metadata.setdefault("logical_run_id", logical_run_id)
    get_tracking_service().append_agent_record(
        runtime_run_id,
        {
            "record_kind": record_kind,
            "thread_id": thread_id,
            "system_prompt": system_prompt,
            "input_messages_json": _jsonable(input_messages_json),
            "messages_json": _jsonable(messages_json),
            "result_json": _jsonable(result_json),
            "started_at": started_at,
            "completed_at": completed_at,
            "metadata_json": _jsonable(metadata),
        },
    )


def iter_daily_audit_agent_records(
    project_id: str,
    *,
    record_kind: str | None = None,
) -> list[dict[str, Any]]:
    with _connect() as conn:
        try:
            rows = conn.execute(
                """
                SELECT run_id, started_at, completed_at, agent_records_json
                FROM tracked_runs
                WHERE project_id = ? AND event_type = 'daily_audit'
                ORDER BY started_at DESC, run_id DESC
                """,
                (project_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            records = json.loads(str(row["agent_records_json"]) or "[]")
        except json.JSONDecodeError:
            records = []
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if record_kind and str(record.get("record_kind") or "") != record_kind:
                continue
            normalized = dict(record)
            normalized["_runtime_run_id"] = str(row["run_id"])
            normalized.setdefault("started_at", row["started_at"])
            normalized.setdefault("completed_at", row["completed_at"])
            metadata = normalized.get("metadata_json")
            normalized["metadata_json"] = metadata if isinstance(metadata, dict) else {}
            results.append(normalized)
    return results


def find_daily_audit_record(
    project_id: str,
    *,
    logical_run_id: str,
    record_kind: str,
) -> dict[str, Any] | None:
    for record in iter_daily_audit_agent_records(project_id, record_kind=record_kind):
        metadata = record.get("metadata_json") or {}
        if str(metadata.get("logical_run_id") or "") == logical_run_id:
            return record
    return None


def transcript_search_matches(text: str, query: str) -> bool:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)]
    haystack = text.lower()
    return bool(text.strip()) and all(term in haystack for term in terms)
