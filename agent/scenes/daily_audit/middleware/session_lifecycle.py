"""DeepAgents middleware for daily audit session lifecycle and recall injection."""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.scenes.daily_audit.models import (
    DailyAuditAgentResponse,
    DailyAuditContext,
    DailyAuditSelectionResponse,
)
from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore
from agent.scenes.daily_audit.runtime.deepagents import archive_daily_audit_run_transcript

logger = logging.getLogger(__name__)


def _coerce_agent_response(value: object) -> DailyAuditAgentResponse | None:
    if isinstance(value, DailyAuditAgentResponse):
        return value
    if isinstance(value, dict):
        return DailyAuditAgentResponse.model_validate(value)
    return None


def _coerce_selection_response(value: object) -> DailyAuditSelectionResponse | None:
    if isinstance(value, DailyAuditSelectionResponse):
        return value
    if isinstance(value, dict):
        return DailyAuditSelectionResponse.model_validate(value)
    return None


class DailyAuditSessionMiddleware(AgentMiddleware):
    """Own the session/run lifecycle through DeepAgents middleware hooks."""

    def __init__(
        self,
        *,
        context: DailyAuditContext,
        stage: str,
        runtime_run_id: str | None,
        store: DailyAuditPersistenceStore,
        repo_dir: str,
        sandbox,
        system_prompt: str = "",
    ) -> None:
        self.context = context
        self.stage = stage
        self.runtime_run_id = runtime_run_id
        self.store = store
        self.repo_dir = repo_dir
        self.sandbox = sandbox
        self.system_prompt = system_prompt

    def _repo_head_sha(self) -> str:
        if not self.repo_dir:
            return ""
        command = f"git -C {shlex.quote(self.repo_dir)} rev-parse HEAD"
        if self.sandbox is not None and hasattr(self.sandbox, "execute"):
            try:
                result = self.sandbox.execute(command, timeout=30)
                if getattr(result, "exit_code", 1) == 0:
                    return str(getattr(result, "output", "") or "").strip()
            except Exception:
                return ""
            return ""
        result = subprocess.run(
            ["git", "-C", self.repo_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    @staticmethod
    def _normalize_tool_request(request: Any) -> Any:
        tool_call = getattr(request, "tool_call", None)
        if not isinstance(tool_call, dict):
            return request
        if tool_call.get("name") != "glob":
            return request
        args = tool_call.get("args")
        if not isinstance(args, dict) or args.get("path") is not None:
            return request
        normalized_tool_call = dict(tool_call)
        normalized_args = dict(args)
        normalized_args["path"] = "/"
        normalized_tool_call["args"] = normalized_args
        if hasattr(request, "model_copy"):
            return request.model_copy(update={"tool_call": normalized_tool_call})
        request.tool_call = normalized_tool_call
        return request

    def before_agent(self, state, runtime, config) -> None:  # type: ignore[override]
        del state, runtime, config
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request)

    def after_model(self, state, runtime) -> None:
        del state, runtime
        return None

    async def aafter_agent(self, state, runtime) -> None:
        del runtime
        if self.stage == "direction":
            response = _coerce_selection_response(state.get("structured_response"))
            if response is None:
                return None
            self._persist_direction_response(response)
            await self._enqueue_direction_persistence(self._build_direction_persistence_event(response))
            return None
        if self.stage != "analysis":
            return None
        response = _coerce_agent_response(state.get("structured_response"))
        if response is None:
            return None
        archived = self._persist_analysis_response(response)
        if not archived:
            logger.warning(
                "daily audit transcript archive failed; skipping async persistence fan-out for run %s",
                self.context.run_id,
            )
            return None
        await self._enqueue_short_term_persistence(self._build_short_term_persistence_event())
        await self._enqueue_long_term_persistence(self._build_long_term_persistence_event())
        await self._enqueue_skill_persistence(self._build_skill_persistence_event())
        return None

    def _build_direction_persistence_event(self, response: DailyAuditSelectionResponse):
        from agent.runtime.models import EventEnvelope

        return EventEnvelope(
            event_id=f"daily_audit_direction_persistence:{self.context.project_id}:{self.context.run_id}",
            event_type="daily_audit_direction_persistence",
            project_id=self.context.project_id,
            mr_iid=None,
            source_branch=self.context.default_branch,
            target_branch=self.context.default_branch,
            title=f"Daily audit direction persistence {self.context.run_id}",
            payload={
                "kind": "direction_archive",
                "run_id": self.context.run_id,
                "parent_runtime_run_id": self.runtime_run_id,
                "session_id": self.context.session_id,
                "default_branch": self.context.default_branch,
                "selection": response.model_dump(mode="json"),
            },
        )

    async def _enqueue_direction_persistence(self, event) -> None:
        from agent.runtime.queue import enqueue_gitlab_event

        await enqueue_gitlab_event(event)

    def _build_short_term_persistence_event(self):
        from agent.runtime.models import EventEnvelope

        return EventEnvelope(
            event_id=f"daily_audit_short_term_persistence:{self.context.project_id}:{self.context.run_id}",
            event_type="daily_audit_short_term_persistence",
            project_id=self.context.project_id,
            mr_iid=None,
            source_branch=self.context.default_branch,
            target_branch=self.context.default_branch,
            title=f"Daily audit short term persistence {self.context.run_id}",
            payload={
                "kind": "short_term_persistence",
                "run_id": self.context.run_id,
                "parent_runtime_run_id": self.runtime_run_id,
                "session_id": self.context.session_id,
                "default_branch": self.context.default_branch,
            },
        )

    async def _enqueue_short_term_persistence(self, event) -> None:
        from agent.runtime.queue import enqueue_gitlab_event

        await enqueue_gitlab_event(event)

    def _build_long_term_persistence_event(self):
        from agent.runtime.models import EventEnvelope

        return EventEnvelope(
            event_id=f"daily_audit_long_term_persistence:{self.context.project_id}:{self.context.run_id}",
            event_type="daily_audit_long_term_persistence",
            project_id=self.context.project_id,
            mr_iid=None,
            source_branch=self.context.default_branch,
            target_branch=self.context.default_branch,
            title=f"Daily audit long term persistence {self.context.run_id}",
            payload={
                "kind": "long_term_persistence",
                "run_id": self.context.run_id,
                "parent_runtime_run_id": self.runtime_run_id,
                "session_id": self.context.session_id,
                "default_branch": self.context.default_branch,
            },
        )

    async def _enqueue_long_term_persistence(self, event) -> None:
        from agent.runtime.queue import enqueue_gitlab_event

        await enqueue_gitlab_event(event)

    def _build_skill_persistence_event(self):
        from agent.runtime.models import EventEnvelope

        return EventEnvelope(
            event_id=f"daily_audit_skill_persistence:{self.context.project_id}:{self.context.run_id}",
            event_type="daily_audit_skill_persistence",
            project_id=self.context.project_id,
            mr_iid=None,
            source_branch=self.context.default_branch,
            target_branch=self.context.default_branch,
            title=f"Daily audit skill persistence {self.context.run_id}",
            payload={
                "kind": "skill_persistence",
                "run_id": self.context.run_id,
                "parent_runtime_run_id": self.runtime_run_id,
                "session_id": self.context.session_id,
                "default_branch": self.context.default_branch,
                "repo_dir": self.repo_dir,
                "flush": True,
            },
        )

    async def _enqueue_skill_persistence(self, event) -> None:
        from agent.runtime.queue import enqueue_gitlab_event

        await enqueue_gitlab_event(event)

    def _persist_analysis_response(self, response: DailyAuditAgentResponse) -> bool:
        repo_head_sha = self._repo_head_sha()
        return archive_daily_audit_run_transcript(
            project_id=self.context.project_id,
            runtime_run_id=self.runtime_run_id,
            run_id=self.context.run_id,
            unit_label=response.selected_unit.label,
            file_path=response.selected_unit.file_path,
            record_kind="daily_audit.analysis",
            system_prompt=self.system_prompt,
            result_json=response.model_dump(mode="json"),
            metadata_json={
                "stage": "analysis",
                "default_branch": self.context.default_branch,
                "repo_head_sha": repo_head_sha,
                "session_id": self.context.session_id,
                "experiment_root": self.context.experiment_root,
                "repo_dir": self.repo_dir,
                "selected_unit_label": response.selected_unit.label,
                "entrypoint_kind": response.selected_unit.entrypoint_kind,
                "entrypoint_symbol": response.selected_unit.entrypoint_symbol,
            },
        )

    def _persist_direction_response(self, response: DailyAuditSelectionResponse) -> bool:
        repo_head_sha = self._repo_head_sha()
        return archive_daily_audit_run_transcript(
            project_id=self.context.project_id,
            runtime_run_id=self.runtime_run_id,
            run_id=self.context.run_id,
            unit_label=response.selected_unit.label,
            file_path=response.selected_unit.file_path,
            role="direction",
            record_kind="daily_audit.direction",
            system_prompt=self.system_prompt,
            result_json=response.model_dump(mode="json"),
            metadata_json={
                "stage": "direction",
                "default_branch": self.context.default_branch,
                "repo_head_sha": repo_head_sha,
                "session_id": self.context.session_id,
                "experiment_root": self.context.experiment_root,
                "repo_dir": self.repo_dir,
                "selected_unit_label": response.selected_unit.label,
                "entrypoint_kind": response.selected_unit.entrypoint_kind,
                "entrypoint_symbol": response.selected_unit.entrypoint_symbol,
            },
        )

    def wrap_tool_call(
        self,
        request,
        handler: Callable[[Any], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        request = self._normalize_tool_request(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        request = self._normalize_tool_request(request)
        return await handler(request)
