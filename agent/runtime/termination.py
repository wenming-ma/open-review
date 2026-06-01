"""Runtime termination controls and cooperative cancellation helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class RunTerminationRequested(RuntimeError):
    """Raised when an operator requested cooperative termination for a running task."""

    def __init__(
        self,
        *,
        run_id: str,
        actor_key: str,
        reason: str = "user_terminated",
        requested_by: str | None = None,
        requested_at: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.actor_key = actor_key
        self.reason = reason
        self.requested_by = requested_by
        self.requested_at = requested_at
        super().__init__(reason)


async def get_run_termination_request(run_id: str):
    from agent.runtime.queue import get_runtime_store

    store = await get_runtime_store()
    return await store.get_run_termination(run_id)


async def raise_if_run_termination_requested(
    *,
    run_id: str | None,
    actor_key: str,
    reason: str = "user_terminated",
) -> None:
    if not run_id:
        return
    request = await get_run_termination_request(run_id)
    if request is None:
        return
    raise RunTerminationRequested(
        run_id=run_id,
        actor_key=actor_key,
        reason=reason,
        requested_by=request.requested_by,
        requested_at=request.requested_at,
    )


class RunTerminationMiddleware(AgentMiddleware[Any, Any, Any]):
    """Check cooperative termination requests at agent/model/tool boundaries."""

    def __init__(self, *, run_id: str | None, actor_key: str, reason: str = "user_terminated") -> None:
        self.run_id = run_id
        self.actor_key = actor_key
        self.reason = reason

    async def abefore_agent(self, state, runtime, config) -> None:  # type: ignore[override]
        del state, runtime, config
        await raise_if_run_termination_requested(
            run_id=self.run_id,
            actor_key=self.actor_key,
            reason=self.reason,
        )
        return None

    async def abefore_model(self, state, runtime) -> None:  # type: ignore[override]
        del state, runtime
        await raise_if_run_termination_requested(
            run_id=self.run_id,
            actor_key=self.actor_key,
            reason=self.reason,
        )
        return None

    async def awrap_model_call(self, request, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        await raise_if_run_termination_requested(
            run_id=self.run_id,
            actor_key=self.actor_key,
            reason=self.reason,
        )
        return await handler(request)

    async def awrap_tool_call(
        self,
        request,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        await raise_if_run_termination_requested(
            run_id=self.run_id,
            actor_key=self.actor_key,
            reason=self.reason,
        )
        return await handler(request)
