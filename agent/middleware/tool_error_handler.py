"""Tool error handling middleware.

Wraps all tool calls in try/except so that unhandled exceptions are
returned as error ToolMessages instead of crashing the agent run.

Adapted from langchain-open-swe.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


def _get_tool_call_id(request: ToolCallRequest) -> str | None:
    if isinstance(request.tool_call, dict):
        return request.tool_call.get("id")
    return None


def _to_error_payload(e: Exception, request: ToolCallRequest | None = None) -> dict:
    data = {"error": str(e), "error_type": e.__class__.__name__, "status": "error"}
    if request:
        tc = getattr(request, "tool_call", None)
        if isinstance(tc, dict) and tc.get("name"):
            data["name"] = tc["name"]
    return data


class ToolErrorMiddleware(AgentMiddleware):
    """Catch tool exceptions and return structured error messages."""

    state_schema = AgentState

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            return handler(request)
        except Exception as e:
            logger.exception("Tool call error: %r", request)
            return ToolMessage(
                content=json.dumps(_to_error_payload(e, request)),
                tool_call_id=_get_tool_call_id(request),
                status="error",
            )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            return await handler(request)
        except Exception as e:
            logger.exception("Tool call error: %r", request)
            return ToolMessage(
                content=json.dumps(_to_error_payload(e, request)),
                tool_call_id=_get_tool_call_id(request),
                status="error",
            )
