"""Agent builders for the daily audit workflow."""

from __future__ import annotations

import inspect
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol

from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.observability import start_open_review_span
from agent.rlm import build_repo_analyst_subagent
from agent.runtime.termination import RunTerminationMiddleware
from agent.sandbox.manager import sandbox_file_tool_path, sandbox_shell_path
from agent.scenes.daily_audit.middleware import DailyAuditSessionMiddleware
from agent.scenes.daily_audit.models import (
    DailyAuditAgentResponse,
    DailyAuditContext,
    DailyAuditSubagentType,
)
from agent.scenes.daily_audit.runtime.backends import DailyAuditBackend, FileToolBackend
from agent.scenes.daily_audit.runtime.deepagents import (
    get_daily_audit_checkpointer,
    get_daily_audit_store,
)
from agent.scenes.daily_audit.selfevolution.prompts import (
    build_daily_audit_agent_prompt,
    build_daily_audit_auxiliary_prompt,
)
from agent.scenes.daily_audit.selfevolution.repo import daily_audit_state_root
from agent.scenes.daily_audit.selfevolution.tools import (
    _skill_source_roots,
    build_direction_history_tool,
    build_exploration_memory_tool,
    build_session_search_tool,
    build_skill_tools,
    describe_daily_subagent,
)
from agent.utils.model import make_model
from agent.utils.structured_output import (
    SimpleStructuredSubagentRunnable,
    SimpleSubagentResult,
    make_structured_response_format,
)


def _safe_skill_source_name(source_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_name or "").strip("._-")
    return value or "skills"


def _visible_skill_source_path(sandbox: SandboxBackendProtocol, source_name: str, root: Path) -> str:
    candidate = root.resolve()
    if getattr(sandbox, "host_root_dir", None) is None:
        return str(candidate)

    state_root = daily_audit_state_root().resolve()
    if candidate == state_root or state_root in candidate.parents:
        return str(candidate)

    mirror_root = state_root / "runtime" / "daily_audit" / "bundled-skills" / _safe_skill_source_name(source_name)
    if mirror_root.exists():
        shutil.rmtree(mirror_root)
    shutil.copytree(candidate, mirror_root)
    return str(mirror_root)


def _skill_sources(sandbox: SandboxBackendProtocol, repo_dir: str, default_branch: str) -> list[str]:
    sources: list[str] = []
    for source_name, root in _skill_source_roots(repo_dir, default_branch=default_branch):
        if not root.is_dir():
            continue
        sources.append(_visible_skill_source_path(sandbox, source_name, root))
    return sources


@dataclass
class _ObservedDailyAuditRunnable:
    runnable: Any
    span_name: str
    tags: list[str]
    static_attributes: dict[str, Any]

    @staticmethod
    def _configurable(config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            return {}
        configurable = config.get("configurable")
        return configurable if isinstance(configurable, dict) else {}

    def _attributes(self, config: Any) -> dict[str, Any]:
        configurable = self._configurable(config)
        attributes = dict(self.static_attributes)
        for source_key, attr_key in (
            ("project_id", "open_review.project_id"),
            ("thread_id", "open_review.session_id"),
        ):
            value = configurable.get(source_key)
            if value is not None:
                attributes[attr_key] = value
        return attributes

    @staticmethod
    def _payload_keys(payload: Any) -> list[str] | None:
        if isinstance(payload, dict):
            return sorted(payload.keys())
        return None

    async def ainvoke(self, payload: Any, config: Any | None = None, **kwargs: Any) -> Any:
        with start_open_review_span(
            self.span_name,
            attributes=self._attributes(config),
            metadata=None,
            tags=self.tags,
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input(payload)
            try:
                result = await self.runnable.ainvoke(payload, config=config, **kwargs)
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "payload_keys": self._payload_keys(payload),
                    },
                )
                raise
            trace_ctx.add_event(
                "invoke_completed",
                {
                    "payload_keys": self._payload_keys(result),
                    "structured_response_present": isinstance(result, dict)
                    and result.get("structured_response") is not None,
                },
            )
            trace_ctx.set_output(result)
            return result

    def invoke(self, payload: Any, config: Any | None = None, **kwargs: Any) -> Any:
        with start_open_review_span(
            self.span_name,
            attributes=self._attributes(config),
            metadata=None,
            tags=self.tags,
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input(payload)
            try:
                invoke = getattr(self.runnable, "invoke", None)
                if callable(invoke):
                    result = invoke(payload, config=config, **kwargs)
                else:
                    ainvoke = getattr(self.runnable, "ainvoke", None)
                    if callable(ainvoke):
                        result = ainvoke(payload, config=config, **kwargs)
                        if inspect.isawaitable(result):
                            raise RuntimeError("sync invoke is not supported for async-only observed subagents")
                    else:
                        raise AttributeError("wrapped runnable does not implement invoke or ainvoke")
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "payload_keys": self._payload_keys(payload),
                    },
                )
                raise
            trace_ctx.add_event(
                "invoke_completed",
                {
                    "payload_keys": self._payload_keys(result),
                    "structured_response_present": isinstance(result, dict)
                    and result.get("structured_response") is not None,
                },
            )
            trace_ctx.set_output(result)
            return result


def _stage_subagent_types(stage: Literal["direction", "analysis"]) -> tuple[DailyAuditSubagentType, ...]:
    if stage == "direction":
        return ()
    return (
        "correctness_reviewer",
        "performance_reviewer",
        "optimization_reviewer",
        "verification_agent",
        "evolution_curator",
    )


def _termination_middleware(context: DailyAuditContext, runtime_run_id: str | None) -> list[object]:
    if not runtime_run_id:
        return []
    return [
        RunTerminationMiddleware(
            run_id=runtime_run_id,
            actor_key=context.actor_key,
        )
    ]


def build_daily_audit_auxiliary_subagent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    context: DailyAuditContext,
    subagent_type: DailyAuditSubagentType,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    """Build a read-only daily-audit auxiliary subagent."""
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    backend = FileToolBackend(sandbox, allow_writes=False)
    runnable = create_deep_agent(
        model=model,
        system_prompt=build_daily_audit_auxiliary_prompt(
            repo_dir=repo_dir,
            file_tool_repo_dir=file_tool_repo_dir,
            context=context,
            subagent_type=subagent_type,
        ),
        backend=backend,
        middleware=[
            *_termination_middleware(context, runtime_run_id),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(sandbox, repo_dir, context.default_branch),
        response_format=make_structured_response_format(SimpleSubagentResult),
    )
    return {
        "name": subagent_type,
        "description": describe_daily_subagent(subagent_type),
        "runnable": _ObservedDailyAuditRunnable(
            runnable=SimpleStructuredSubagentRunnable(runnable=runnable, name=subagent_type),
            span_name=f"open_review.daily_audit.subagent.{subagent_type}",
            tags=["daily_audit", "subagent"],
            static_attributes={
                "open_review.parent_role": "daily_audit",
                "open_review.daily_subagent": subagent_type,
                "open_review.run_id": context.run_id,
            },
        ),
    }


def build_daily_audit_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    context: DailyAuditContext,
    model_id: str | None = None,
    response_format: type[Any] | dict[str, Any] | None = None,
    stage: Literal["direction", "analysis"] = "direction",
    runtime_run_id: str | None = None,
):
    """Build the writable primary daily-audit agent and its service subagents."""
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    shell_repo_dir = sandbox_shell_path(sandbox, repo_dir)
    from agent.scenes.daily_audit.persistence.store import get_daily_audit_persistence_store

    store = get_daily_audit_persistence_store()
    system_prompt = build_daily_audit_agent_prompt(
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        context=context,
        stage=stage,
    )
    session_middleware = DailyAuditSessionMiddleware(
        context=context,
        stage=stage,
        runtime_run_id=runtime_run_id,
        store=store,
        repo_dir=repo_dir,
        sandbox=sandbox,
        system_prompt=system_prompt,
    )
    session_search_tool = build_session_search_tool(
        project_id=context.project_id,
        session_id=context.session_id,
        store=store,
    )
    direction_history_tool = build_direction_history_tool(
        project_id=context.project_id,
        run_id=context.run_id,
        store=store,
    )
    exploration_memory_tool = build_exploration_memory_tool(
        project_id=context.project_id,
        store=store,
    )
    skills_list_tool, skill_view_tool, skill_manage_tool = build_skill_tools(
        repo_dir=repo_dir,
        default_branch=context.default_branch,
    )
    tools = [session_search_tool, skills_list_tool, skill_view_tool, skill_manage_tool]
    if stage == "direction":
        tools.append(direction_history_tool)
        tools.append(exploration_memory_tool)
    subagents = [
        build_daily_audit_auxiliary_subagent(
            sandbox=sandbox,
            repo_dir=repo_dir,
            context=context,
            subagent_type=subagent_type,
            model_id=model_id,
            runtime_run_id=runtime_run_id,
        )
        for subagent_type in _stage_subagent_types(stage)
    ]
    extra_repo_tools: dict[str, Any] = {
        "session_search": session_search_tool,
        "skills_list": skills_list_tool,
        "skill_view": skill_view_tool,
        "skill_manage": skill_manage_tool,
    }
    if stage == "direction":
        extra_repo_tools["direction_history"] = direction_history_tool
        extra_repo_tools["exploration_memory"] = exploration_memory_tool
    subagents.append(
        build_repo_analyst_subagent(
            scene="daily_audit",
            backend=DailyAuditBackend(sandbox, allow_writes=False),
            repo_dir=repo_dir,
            file_tool_repo_dir=file_tool_repo_dir,
            shell_repo_dir=shell_repo_dir,
            model_id=model_id,
            context_payload={
                "project_id": context.project_id,
                "run_id": context.run_id,
                "stage": stage,
                "selected_unit": context.selected_unit.model_dump() if context.selected_unit is not None else None,
            },
            extra_tools=extra_repo_tools,
        )
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        backend=DailyAuditBackend(sandbox, allow_writes=True),
        middleware=[
            *_termination_middleware(context, runtime_run_id),
            session_middleware,
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(sandbox, repo_dir, context.default_branch),
        response_format=make_structured_response_format(response_format or DailyAuditAgentResponse),
        subagents=subagents,
        checkpointer=get_daily_audit_checkpointer(),
        store=get_daily_audit_store(),
    )
