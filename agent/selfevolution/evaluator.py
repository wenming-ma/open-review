"""Prompt evaluator agent for GEPA self-evolution."""

from __future__ import annotations

import json
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from pydantic import BaseModel, Field

from agent.middleware import ModelRetryMiddleware, StructuredOutputRetryMiddleware, ToolErrorMiddleware
from agent.scenes.daily_audit.runtime.backends import FileToolBackend
from agent.selfevolution.materialization import materialize_task_repository
from agent.utils.model import make_model
from agent.utils.structured_output import make_structured_response_format


class PromptEvaluationVerdict(BaseModel):
    instruction_following: float = Field(ge=0, le=1)
    task_accuracy: float | None = Field(default=None, ge=0, le=1)
    language_quality: float | None = Field(default=None, ge=0, le=1)
    issue_truthfulness: float | None = Field(default=None, ge=0, le=1)
    direction_quality: float | None = Field(default=None, ge=0, le=1)
    feedback_score: float | None = Field(default=None, ge=0, le=1)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _dossier_text(example, rendered_prompt: str, materialized) -> str:
    agent_record = getattr(example, "agent_record", {}) or {}
    return "\n".join(
        [
            f"Agent type: {getattr(example, 'agent_type', '')}",
            f"Prompt target: {getattr(example, 'prompt_target', '')}",
            f"Project: {getattr(example, 'project_id', '')}",
            f"Run: {getattr(example, 'source_run_id', '')}",
            "",
            "## Candidate Rendered Prompt",
            rendered_prompt,
            "",
            "## Historical System Prompt",
            str(getattr(example, "historical_system_prompt", "") or ""),
            "",
            "## Task Input",
            str(getattr(example, "task_input", "") or ""),
            "",
            "## Agent Record",
            _json_text(agent_record),
            "",
            "## Trigger Events",
            _json_text(getattr(example, "trigger_events", [])),
            "",
            "## Published Objects",
            _json_text(getattr(example, "published_objects", [])),
            "",
            "## Feedback Events",
            _json_text(getattr(example, "feedback_events", [])),
            "",
            "## Materialized Repository",
            f"repo_dir={materialized.repo_dir}",
            f"identifier_summary={_json_text(materialized.identifier_summary)}",
            f"notes={_json_text(materialized.notes)}",
            "",
            "## Materialized Diff",
            materialized.diff_text or "(none)",
        ]
    ).strip()


def _evaluate_prompt_with_agent(*, example, rendered_prompt: str) -> dict[str, Any]:
    with materialize_task_repository(example) as materialized:
        backend = FileToolBackend(
            LocalShellBackend(root_dir=materialized.temp_root, virtual_mode=True, inherit_env=False),
            allow_writes=False,
        )
        agent = create_deep_agent(
            model=make_model(temperature=0, max_tokens=6_000),
            system_prompt=(
                "You are a prompt evaluator for Open Review self-evolution.\n"
                "Judge how well the candidate rendered system prompt would guide the main agent for this task.\n"
                "Use repository tools and shell inspection when needed, but do not modify files or rerun the original workflow.\n"
                "Score only the dimensions that are relevant and supported by evidence.\n"
                "Higher is better, 0.0 to 1.0.\n"
            ),
            backend=backend,
            middleware=[
                StructuredOutputRetryMiddleware(),
                ModelRetryMiddleware(),
                ToolErrorMiddleware(),
            ],
            response_format=make_structured_response_format(PromptEvaluationVerdict),
            name="selfevolution-evaluator",
        )
        payload = agent.invoke(
            {"messages": [{"role": "user", "content": _dossier_text(example, rendered_prompt, materialized)}]},
            config={"configurable": {"project_id": getattr(example, "project_id", ""), "repo_dir": materialized.repo_dir, "thread_id": f"selfevolution:{getattr(example, 'source_run_id', '')}:{getattr(example, 'prompt_target', '')}"}},
        )
        verdict = payload.get("structured_response", payload)
        if isinstance(verdict, PromptEvaluationVerdict):
            result = verdict.model_dump(mode="json")
        elif isinstance(verdict, dict):
            result = PromptEvaluationVerdict.model_validate(verdict).model_dump(mode="json")
        else:
            result = PromptEvaluationVerdict.model_validate(verdict).model_dump(mode="json")
        result["materialization_failed"] = bool(materialized.notes and not materialized.repo_dir)
        return result


def evaluate_prompt_candidate(*, example, rendered_prompt: str) -> dict[str, Any]:
    return _evaluate_prompt_with_agent(example=example, rendered_prompt=rendered_prompt)
