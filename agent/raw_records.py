"""Shared raw-record helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {str(key): jsonable(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
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
        return {"role": "user", "content": jsonable(message.content)}
    if isinstance(message, AIMessage):
        payload = {"role": "assistant", "content": jsonable(message.content)}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            payload["tool_calls"] = jsonable(tool_calls)
        return payload
    if isinstance(message, ToolMessage):
        payload = {
            "role": "tool",
            "content": jsonable(message.content),
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        if getattr(message, "name", None):
            payload["name"] = message.name
        return payload
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": jsonable(message.content)}
    if isinstance(message, dict):
        payload = dict(jsonable(message))
        payload["role"] = _normalize_role(payload.get("role") or payload.get("type"))
        if "content" not in payload:
            payload["content"] = ""
        return payload
    return {"role": _normalize_role(type(message).__name__), "content": str(message)}


def serialize_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    return [serialize_message(message) for message in messages]
