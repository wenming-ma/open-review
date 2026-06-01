"""Self-evolution plumbing for the Auto Review agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.controlplane import get_tracking_service
from agent.config import settings
from agent.prompt import EDA_STANDARDS
from agent.selfevolution.apply import (
    apply_code_candidate_direct_merge,
    apply_prompt_candidate_direct_merge,
    apply_skill_candidate_direct_merge,
    apply_tool_description_candidate_direct_merge,
)
from agent.selfevolution.assets import (
    active_selfevolution_root,
    scene_code_targets_path,
    scene_prompt_root,
    scene_skill_root,
    scene_tool_metadata_path,
)
from agent.selfevolution.common import (
    AgentSelfEvolutionSpec,
    render_prompt_template,
)
from agent.selfevolution.gepa import PromptTaskExample, run_gepa_self_evolution_for_spec


def _scene_root(default_branch: str | None = None):
    return active_selfevolution_root("auto_review", default_branch=default_branch).parents[0]


def _skill_root(default_branch: str | None = None) -> Path:
    return scene_skill_root("auto_review", default_branch=default_branch)


def _prompt_root(default_branch: str | None = None) -> Path:
    return scene_prompt_root("auto_review", default_branch=default_branch)


def _tool_metadata_path(default_branch: str | None = None) -> Path:
    return scene_tool_metadata_path("auto_review", default_branch=default_branch)


def _code_targets_path(default_branch: str | None = None) -> Path:
    return scene_code_targets_path("auto_review", default_branch=default_branch)


def _raw_runs(project_id: str, *, limit: int) -> list[dict[str, Any]]:
    return get_tracking_service().list_runs(project_id=project_id, event_type="auto_review", limit=max(limit * 5, 50))


def _find_agent_record(run: dict[str, Any], record_kind: str) -> dict[str, Any] | None:
    for item in run.get("agent_records", []):
        if isinstance(item, dict) and str(item.get("record_kind") or "") == record_kind:
            return item
    return None


def _feedback_suffix(run: dict[str, Any]) -> str:
    items = []
    for event in run.get("feedback_events", []):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("feedback_kind") or "").strip()
        author = str(event.get("author") or "").strip()
        if not kind:
            continue
        items.append(f"- {kind}" + (f" by {author}" if author else ""))
    if not items:
        return ""
    return "\n\nExternal feedback:\n" + "\n".join(items)


def _director_expected_behavior(result: dict[str, Any]) -> str:
    parts = []
    summary = str(result.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    for finding in result.get("confirmed_findings") or []:
        if isinstance(finding, dict):
            parts.append(str(finding.get("summary") or "").strip())
    for finding in result.get("suspicious_findings") or []:
        if isinstance(finding, dict):
            parts.append(str(finding.get("summary") or "").strip())
    for question in result.get("open_questions") or []:
        if isinstance(question, dict):
            parts.append(str(question.get("summary") or "").strip())
    return "\n".join(item for item in parts if item)


def build_auto_review_skill_eval_examples(project_id: str, *, limit: int = 20):
    from agent.scenes.daily_audit.selfevolution.evaluation import DailyAuditEvalExample

    examples: list[DailyAuditEvalExample] = []
    for run in reversed(_raw_runs(project_id, limit=limit)):
        record = _find_agent_record(run, "auto_review.director")
        if record is None:
            continue
        result = record.get("result_json") if isinstance(record, dict) else {}
        result = result if isinstance(result, dict) else {}
        expected = _director_expected_behavior(result).strip()
        if not expected:
            continue
        input_messages = record.get("input_messages_json") if isinstance(record, dict) else []
        task_input = ""
        if isinstance(input_messages, list) and input_messages:
            first = input_messages[0]
            if isinstance(first, dict):
                task_input = str(first.get("content") or "").strip()
        metadata = record.get("metadata_json") if isinstance(record, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        examples.append(
            DailyAuditEvalExample(
                task_input=task_input or "Review the merge request and return a grounded structured decision.",
                expected_behavior=expected + _feedback_suffix(run),
                source_run_id=str(metadata.get("logical_run_id") or run.get("run_id") or ""),
                recommended_action=str(result.get("recommendation") or ""),
                used_subagents=tuple(
                    str(item.get("lane") or "")
                    for item in (result.get("specialist_reports") or [])
                    if isinstance(item, dict) and str(item.get("lane") or "").strip()
                ),
            )
        )
        if len(examples) >= limit:
            break
    return examples


def build_auto_review_prompt_eval_examples(project_id: str, target_name: str, limit: int = 20) -> list[PromptTaskExample]:
    del target_name
    examples: list[PromptTaskExample] = []
    for run in reversed(_raw_runs(project_id, limit=limit)):
        record = _find_agent_record(run, "auto_review.director")
        if record is None:
            continue
        input_messages = record.get("input_messages_json") if isinstance(record, dict) else []
        task_input = ""
        if isinstance(input_messages, list) and input_messages:
            first = input_messages[0]
            if isinstance(first, dict):
                task_input = str(first.get("content") or "").strip()
        metadata = record.get("metadata_json") if isinstance(record, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        examples.append(
            PromptTaskExample(
                agent_type="auto_review",
                prompt_target="director-prompt",
                source_run_id=str(metadata.get("logical_run_id") or run.get("run_id") or ""),
                runtime_run_id=str(run.get("run_id") or ""),
                project_id=project_id,
                task_input=task_input or "Review the merge request and return a grounded structured decision.",
                historical_system_prompt=str(record.get("system_prompt") or ""),
                agent_record=record,
                trigger_events=[item for item in (run.get("trigger_events") or []) if isinstance(item, dict)],
                feedback_events=[item for item in (run.get("feedback_events") or []) if isinstance(item, dict)],
                published_objects=[item for item in (run.get("published_objects") or []) if isinstance(item, dict)],
                metadata=metadata,
            )
        )
        if len(examples) >= limit:
            break
    return examples


def _render_director_prompt_candidate(target_name: str, candidate_text: str, example: PromptTaskExample) -> str:
    del target_name, example
    return render_prompt_template(candidate_text, {"eda_standards": EDA_STANDARDS})


def _examples_for_prompt(project_id: str, target_name: str):
    del target_name
    return build_auto_review_skill_eval_examples(project_id)


def _examples_for_tool(project_id: str, target_name: str):
    del target_name
    return build_auto_review_skill_eval_examples(project_id)


_SPEC = AgentSelfEvolutionSpec(
    agent_type="auto_review",
    skill_root=_skill_root,
    prompt_root=_prompt_root,
    tool_metadata_path=_tool_metadata_path,
    code_targets_path=_code_targets_path,
    build_skill_examples=build_auto_review_skill_eval_examples,
    build_prompt_examples=_examples_for_prompt,
    build_tool_examples=_examples_for_tool,
    prompt_allowlist=("director-prompt",),
    build_prompt_eval_examples=build_auto_review_prompt_eval_examples,
    render_prompt_candidate=_render_director_prompt_candidate,
    apply_skill_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_skill_candidate_direct_merge(
        agent_type="auto_review",
        skill_name=target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve auto-review skill {target}",
    ),
    apply_prompt_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_prompt_candidate_direct_merge(
        agent_type="auto_review",
        target_name=target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve auto-review prompt {target}",
    ),
    apply_tool_description_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_tool_description_candidate_direct_merge(
        agent_type="auto_review",
        target_name=target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve auto-review tool {target}",
    ),
    apply_code_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_code_candidate_direct_merge(
        agent_type="auto_review",
        target_path=target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve auto-review code {target.split('/')[-1]}",
    ),
    evaluation_profile=lambda _target_name: {
        "instruction_following": 0.20,
        "task_accuracy": 0.50,
        "language_quality": 0.10,
        "feedback_score": 0.20,
    },
)


def maybe_run_auto_review_self_evolution(project_id: str, *, default_branch: str | None = None):
    return run_gepa_self_evolution_for_spec(
        spec=_SPEC,
        project_id=project_id,
        default_branch=default_branch,
        enabled=settings.AUTO_REVIEW_SELF_EVOLUTION_ENABLED,
    )


def run_auto_review_evolution_cycle(*, project_id: str, default_branch: str, event=None) -> object:
    del event
    return maybe_run_auto_review_self_evolution(project_id, default_branch=default_branch)
