"""Shared GEPA prompt-only self-evolution runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import litellm
from gepa.optimize_anything import EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything

from agent.config import settings
from agent.selfevolution import common as selfevolution_common
from agent.selfevolution.common import (
    AgentSelfEvolutionResult,
    AgentSelfEvolutionSpec,
    SelfEvolutionAssetOutcome,
    SelfEvolutionRejected,
    SelfEvolutionSkipped,
    _apply_status_and_commit,
    failed_asset_outcome,
    finalize_agent_self_evolution_result,
    successful_asset_outcome,
)
from agent.selfevolution.evaluator import evaluate_prompt_candidate
from agent.utils.model import resolve_llm_config


@dataclass(frozen=True)
class PromptTaskExample:
    agent_type: str
    prompt_target: str
    source_run_id: str
    runtime_run_id: str
    project_id: str
    task_input: str
    historical_system_prompt: str
    agent_record: dict[str, Any]
    trigger_events: list[dict[str, Any]]
    feedback_events: list[dict[str, Any]]
    published_objects: list[dict[str, Any]]
    metadata: dict[str, Any]


def _normalize_weight_map(weights: dict[str, float], *, feedback_available: bool) -> dict[str, float]:
    base = {str(key): max(float(value), 0.0) for key, value in weights.items() if float(value) > 0}
    if not feedback_available:
        base.pop("feedback_score", None)
    total = sum(base.values())
    if total <= 0:
        raise RuntimeError("invalid_prompt_evaluation_profile")
    return {key: value / total for key, value in base.items()}


def _default_example_limit() -> int:
    return 20


def split_prompt_examples(
    examples: list[PromptTaskExample],
) -> tuple[list[PromptTaskExample], list[PromptTaskExample], list[PromptTaskExample]]:
    if len(examples) < 3:
        return examples, examples[:1], []
    train_end = max(1, int(len(examples) * 0.6))
    val_end = max(train_end + 1, int(len(examples) * 0.8))
    if val_end >= len(examples):
        val_end = len(examples) - 1
    train = examples[:train_end]
    val = examples[train_end:val_end] or examples[:1]
    heldout = examples[val_end:]
    return train, val, heldout


def _make_reflection_lm():
    resolved = resolve_llm_config(settings.current_snapshot().model_dump())
    model_name = f"{resolved.provider}/{resolved.model}"

    def _call(prompt: str) -> str:
        response = litellm.completion(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            api_base=resolved.base_url,
            api_key=resolved.api_key,
            temperature=0,
        )
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", "")
        if isinstance(content, list):
            parts = [item.get("text", "") if isinstance(item, dict) else str(item) for item in content]
            return "\n".join(part for part in parts if part).strip()
        return str(content or "").strip()

    return _call


def _candidate_text(value: Any) -> str:
    def _strip_code_fence(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return text
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return text
        return "\n".join(lines[1:-1]).strip()

    def _extract(value: Any) -> str:
        if isinstance(value, str):
            stripped = _strip_code_fence(value).strip()
            if stripped[:1] in {"{", "["}:
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    return value
                return _extract(parsed)
            return value
        if isinstance(value, dict):
            for key in ("current_candidate", "content", "prompt", "candidate_text", "text"):
                if key not in value:
                    continue
                extracted = _extract(value.get(key))
                if str(extracted or "").strip():
                    return extracted
            if len(value) == 1:
                return _extract(next(iter(value.values())))
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            if len(value) == 1:
                return _extract(value[0])
            return json.dumps(value, ensure_ascii=False)
        return str(value or "")

    normalized = _extract(value)
    if isinstance(normalized, str):
        return normalized
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "current_candidate" in value:
            return str(value.get("current_candidate") or "")
        if len(value) == 1:
            return str(next(iter(value.values())) or "")
    return str(value or "")


def _fallback_prompt_evaluator(
    *,
    spec: AgentSelfEvolutionSpec,
    target_name: str,
    candidate_text: str,
    example: PromptTaskExample,
) -> tuple[float, dict[str, Any]]:
    rendered = (
        spec.render_prompt_candidate(target_name, candidate_text, example)
        if callable(spec.render_prompt_candidate)
        else candidate_text
    )
    weights = spec.evaluation_profile(target_name) if callable(spec.evaluation_profile) else {}
    feedback_available = bool(example.feedback_events)
    normalized_weights = _normalize_weight_map(
        weights or {"instruction_following": 0.5, "task_accuracy": 0.5},
        feedback_available=feedback_available,
    )
    try:
        verdict = evaluate_prompt_candidate(example=example, rendered_prompt=rendered)
        dimensions = {
            key: float(value)
            for key, value in verdict.items()
            if key in normalized_weights and isinstance(value, (int, float))
        }
        side_info: dict[str, Any] = {
            "scores": dimensions,
            "rendered_prompt": rendered,
            "rationale": verdict.get("rationale", ""),
            "evidence": verdict.get("evidence", []),
        }
        if verdict.get("materialization_failed"):
            side_info["materialization_failed"] = True
    except Exception:
        dimensions = {}
        prompt_lower = rendered.lower()
        task_input_lower = example.task_input.lower()
        dimensions["instruction_following"] = 1.0 if task_input_lower and task_input_lower[:24] in prompt_lower else 0.6
        dimensions["task_accuracy"] = 0.6 if example.agent_record else 0.0
        if "language_quality" in normalized_weights:
            dimensions["language_quality"] = 1.0 if len(rendered.strip()) >= 40 else 0.4
        if "feedback_score" in normalized_weights:
            dimensions["feedback_score"] = 0.8 if example.feedback_events else 0.0
        if "issue_truthfulness" in normalized_weights:
            dimensions["issue_truthfulness"] = 0.6 if example.agent_record.get("result_json") else 0.0
        if "direction_quality" in normalized_weights:
            dimensions["direction_quality"] = 0.6 if example.metadata.get("selected_unit_label") or example.metadata.get("unit_label") else 0.0
        side_info = {"scores": dimensions, "rendered_prompt": rendered}
    total_score = sum(normalized_weights[key] * dimensions.get(key, 0.0) for key in normalized_weights)
    return total_score, side_info


def _evaluate_prompt_candidate(
    *,
    spec: AgentSelfEvolutionSpec,
    target_name: str,
    candidate_text: str,
    examples: list[PromptTaskExample],
) -> tuple[float | None, dict[str, float], int]:
    if not examples:
        return None, {}, 0
    per_dimension: dict[str, list[float]] = {}
    totals: list[float] = []
    materialization_failures = 0
    for example in examples:
        score, side_info = _fallback_prompt_evaluator(
            spec=spec,
            target_name=target_name,
            candidate_text=candidate_text,
            example=example,
        )
        totals.append(float(score))
        scores = side_info.get("scores", {}) if isinstance(side_info, dict) else {}
        if not isinstance(scores, dict):
            scores = {}
        for name, value in scores.items():
            try:
                per_dimension.setdefault(str(name), []).append(float(value))
            except Exception:
                continue
        if side_info.get("materialization_failed"):
            materialization_failures += 1
    averages = {key: (sum(values) / len(values)) for key, values in per_dimension.items() if values}
    return sum(totals) / len(totals), averages, materialization_failures


def run_prompt_target_gepa_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    target_name: str,
    default_branch: str | None = None,
    limit: int = _default_example_limit(),
    candidate_dir: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    if not callable(spec.build_prompt_eval_examples):
        raise RuntimeError(f"{spec.agent_type}_prompt_eval_examples_not_configured")
    examples = list(spec.build_prompt_eval_examples(project_id, target_name, limit))
    if not examples:
        raise SelfEvolutionSkipped("no_recorded_raw_runs")
    baseline_path = spec.prompt_root(default_branch) / f"{target_name}.md"
    if not baseline_path.exists():
        raise RuntimeError(f"missing_prompt_asset:{target_name}")
    baseline_text = baseline_path.read_text(encoding="utf-8")
    train_examples, val_examples, heldout_examples = split_prompt_examples(examples)
    if not train_examples:
        raise SelfEvolutionSkipped("no_recorded_raw_runs")
    reflection_lm = _make_reflection_lm()
    run_dir = (
        selfevolution_common.evolution_root(spec.agent_type, project_id)
        / "runs"
        / "prompts"
        / target_name
        / uuid4().hex
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    result = optimize_anything(
        seed_candidate=baseline_text,
        evaluator=lambda candidate, example: _fallback_prompt_evaluator(
            spec=spec,
            target_name=target_name,
            candidate_text=_candidate_text(candidate),
            example=example,
        ),
        dataset=train_examples,
        valset=val_examples or train_examples,
        objective=(
            f"Improve the {spec.agent_type} prompt target '{target_name}' so it better guides the "
            "agent on real recorded tasks while respecting feedback and repository facts."
        ),
        background=(
            "Optimize only the system prompt text. Evaluate each candidate against recorded task dossiers, "
            "dynamic GitLab materialization, and external feedback. Do not optimize tools, skills, or code."
        ),
        config=GEPAConfig(
            engine=EngineConfig(
                run_dir=str(run_dir),
                max_metric_calls=24,
                parallel=False,
                cache_evaluation=True,
                cache_evaluation_storage="disk",
                candidate_selection_strategy="pareto",
            ),
            reflection=ReflectionConfig(
                reflection_lm=reflection_lm,
                reflection_minibatch_size=3,
            ),
        ),
    )
    candidate_text = _candidate_text(getattr(result, "best_candidate", baseline_text)).strip()
    if not candidate_text:
        raise RuntimeError("empty_gepa_candidate")
    baseline_score, _, baseline_failures = _evaluate_prompt_candidate(
        spec=spec,
        target_name=target_name,
        candidate_text=baseline_text,
        examples=heldout_examples,
    )
    candidate_score, dimension_summary, candidate_failures = _evaluate_prompt_candidate(
        spec=spec,
        target_name=target_name,
        candidate_text=candidate_text,
        examples=heldout_examples,
    )
    gate_reason = None
    if (
        heldout_examples
        and baseline_score is not None
        and candidate_score is not None
        and candidate_score + 1e-9 < baseline_score
    ):
        gate_reason = (
            f"held-out regression for {target_name}: baseline={baseline_score:.3f} "
            f"candidate={candidate_score:.3f}"
        )
    if gate_reason:
        raise SelfEvolutionRejected(gate_reason)
    output_dir = candidate_dir or (
        selfevolution_common.evolution_root(spec.agent_type, project_id) / "candidates" / "prompts" / target_name
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / f"{target_name}.md"
    candidate_path.write_text(candidate_text + ("\n" if not candidate_text.endswith("\n") else ""), encoding="utf-8")
    return candidate_path, {
        "baseline_score": baseline_score,
        "candidate_score": candidate_score,
        "heldout_examples": len(heldout_examples),
        "train_examples": len(train_examples),
        "val_examples": len(val_examples),
        "dimension_scores_summary": dimension_summary,
        "feedback_coverage": sum(1 for item in examples if item.feedback_events),
        "materialization_failures": baseline_failures + candidate_failures,
        "gate_reason": gate_reason,
    }


def _skipped_outcome(asset_type: str, target: str, reason: str) -> SelfEvolutionAssetOutcome:
    return SelfEvolutionAssetOutcome(asset_type=asset_type, target=target, status="skipped", reason=reason)


def _prompt_outputs_for_spec(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    default_branch: str | None = None,
) -> tuple[list[Path | str], list[SelfEvolutionAssetOutcome]]:
    outputs: list[Path | str] = []
    asset_outcomes: list[SelfEvolutionAssetOutcome] = []
    prompt_allowlist = set(spec.prompt_allowlist or ())
    list_prompt_targets_fn = spec.list_prompt_targets_override or (
        lambda branch: selfevolution_common.list_prompt_targets(spec, default_branch=branch)
    )
    prompt_targets = list(list_prompt_targets_fn(default_branch))

    for target_name in prompt_targets:
        if prompt_allowlist and target_name not in prompt_allowlist:
            asset_outcomes.append(_skipped_outcome("prompt", target_name, "not_in_v1_scope"))
            continue
        try:
            candidate_path, evaluation = run_prompt_target_gepa_evolution(
                spec=spec,
                project_id=project_id,
                target_name=target_name,
                default_branch=default_branch,
                candidate_dir=(
                    selfevolution_common.evolution_root(spec.agent_type, project_id)
                    / "candidates"
                    / "prompts"
                    / target_name
                ),
            )
            outputs.append(candidate_path)
            status, reason, commit_sha = _apply_status_and_commit(
                spec=spec,
                asset_type="prompt",
                project_id=project_id,
                target=target_name,
                candidate_path=Path(candidate_path),
                default_branch=default_branch,
            )
            asset_outcomes.append(
                SelfEvolutionAssetOutcome(
                    asset_type="prompt",
                    target=target_name,
                    status=status,
                    reason=reason,
                    candidate_path=str(candidate_path),
                    verification_status="passed",
                    commit_sha=commit_sha,
                    baseline_score=evaluation.get("baseline_score"),
                    candidate_score=evaluation.get("candidate_score"),
                    heldout_examples=evaluation.get("heldout_examples"),
                    train_count=evaluation.get("train_examples"),
                    val_count=evaluation.get("val_examples"),
                    heldout_count=evaluation.get("heldout_examples"),
                    dimension_scores_summary=evaluation.get("dimension_scores_summary"),
                    feedback_coverage=evaluation.get("feedback_coverage"),
                    materialization_failures=evaluation.get("materialization_failures"),
                )
            )
        except Exception as exc:
            reason = getattr(exc, "reason", None) or str(exc).strip() or exc.__class__.__name__
            status = (
                "rejected"
                if isinstance(exc, SelfEvolutionRejected)
                else "skipped"
                if isinstance(exc, SelfEvolutionSkipped)
                else "failed"
            )
            asset_outcomes.append(
                SelfEvolutionAssetOutcome(
                    asset_type="prompt",
                    target=target_name,
                    status=status,
                    reason=reason,
                    gate_reason=reason if status == "rejected" else None,
                )
            )

    return outputs, asset_outcomes


def run_gepa_prompt_self_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    default_branch: str | None = None,
    enabled: bool,
) -> AgentSelfEvolutionResult:
    if not enabled:
        return AgentSelfEvolutionResult(
            agent_type=spec.agent_type,
            status="skipped",
            reason="self_evolution_disabled",
            outputs=[],
            asset_outcomes=[],
        )

    outputs: list[Path | str] = []
    asset_outcomes: list[SelfEvolutionAssetOutcome] = []
    prompt_allowlist = set(spec.prompt_allowlist or ())
    list_skills_fn = spec.list_skills_override or (
        lambda branch: selfevolution_common.list_skills(spec, default_branch=branch)
    )
    list_prompt_targets_fn = spec.list_prompt_targets_override or (
        lambda branch: selfevolution_common.list_prompt_targets(spec, default_branch=branch)
    )
    list_tool_targets_fn = spec.list_tool_description_targets_override or (
        lambda branch: selfevolution_common.list_tool_description_targets(spec, default_branch=branch)
    )
    list_code_targets_fn = spec.list_code_targets_override or (
        lambda branch: selfevolution_common.list_code_targets(spec, default_branch=branch)
    )
    prompt_targets = list(list_prompt_targets_fn(default_branch))
    in_scope_prompt_targets = [item for item in prompt_targets if not prompt_allowlist or item in prompt_allowlist]

    for skill_name in list_skills_fn(default_branch):
        asset_outcomes.append(_skipped_outcome("skill", skill_name, "not_in_v1_scope"))

    prompt_outputs, prompt_outcomes = _prompt_outputs_for_spec(
        spec=spec,
        project_id=project_id,
        default_branch=default_branch,
    )
    outputs.extend(prompt_outputs)
    asset_outcomes.extend(prompt_outcomes)

    for target_name in list_tool_targets_fn(default_branch):
        asset_outcomes.append(_skipped_outcome("tool_description", target_name, "not_in_v1_scope"))

    for target_path in list_code_targets_fn(default_branch):
        asset_outcomes.append(_skipped_outcome("code", target_path, "not_in_v1_scope"))

    if outputs:
        return AgentSelfEvolutionResult(
            agent_type=spec.agent_type,
            status="reported",
            outputs=outputs,
            asset_outcomes=asset_outcomes,
        )
    if not asset_outcomes:
        return AgentSelfEvolutionResult(
            agent_type=spec.agent_type,
            status="skipped",
            reason="no_targets_configured",
            outputs=[],
            asset_outcomes=[],
        )
    if not in_scope_prompt_targets:
        return AgentSelfEvolutionResult(
            agent_type=spec.agent_type,
            status="skipped",
            reason="no_targets_configured",
            outputs=[],
            asset_outcomes=asset_outcomes,
        )
    return finalize_agent_self_evolution_result(
        agent_type=spec.agent_type,
        outputs=outputs,
        asset_outcomes=asset_outcomes,
    )


def run_gepa_self_evolution_for_spec(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    default_branch: str | None = None,
    enabled: bool,
) -> AgentSelfEvolutionResult:
    if not enabled:
        return AgentSelfEvolutionResult(
            agent_type=spec.agent_type,
            status="skipped",
            reason="self_evolution_disabled",
            outputs=[],
            asset_outcomes=[],
        )

    outputs: list[Path | str] = []
    asset_outcomes: list[SelfEvolutionAssetOutcome] = []

    list_skills_fn = spec.list_skills_override or (
        lambda branch: selfevolution_common.list_skills(spec, default_branch=branch)
    )
    list_tool_targets_fn = spec.list_tool_description_targets_override or (
        lambda branch: selfevolution_common.list_tool_description_targets(spec, default_branch=branch)
    )
    list_code_targets_fn = spec.list_code_targets_override or (
        lambda branch: selfevolution_common.list_code_targets(spec, default_branch=branch)
    )

    for skill_name in list_skills_fn(default_branch):
        try:
            candidate_path, evaluation = selfevolution_common.run_skill_evolution(
                spec=spec,
                project_id=project_id,
                skill_name=skill_name,
                default_branch=default_branch,
            )
            outputs.append(candidate_path)
            status, reason, commit_sha = _apply_status_and_commit(
                spec=spec,
                asset_type="skill",
                project_id=project_id,
                target=skill_name,
                candidate_path=Path(candidate_path),
                default_branch=default_branch,
            )
            asset_outcomes.append(
                successful_asset_outcome(
                    asset_type="skill",
                    target=skill_name,
                    status=status,
                    candidate_path=candidate_path,
                    reason=reason,
                    verification_status="passed",
                    evaluation=evaluation,
                    commit_sha=commit_sha,
                )
            )
        except Exception as exc:
            asset_outcomes.append(failed_asset_outcome(asset_type="skill", target=skill_name, exc=exc))

    prompt_outputs, prompt_outcomes = _prompt_outputs_for_spec(
        spec=spec,
        project_id=project_id,
        default_branch=default_branch,
    )
    outputs.extend(prompt_outputs)
    asset_outcomes.extend(prompt_outcomes)

    for target_name in list_tool_targets_fn(default_branch):
        try:
            candidate_path, evaluation = selfevolution_common.run_tool_description_evolution(
                spec=spec,
                project_id=project_id,
                target_name=target_name,
                default_branch=default_branch,
            )
            outputs.append(candidate_path)
            status, reason, commit_sha = _apply_status_and_commit(
                spec=spec,
                asset_type="tool_description",
                project_id=project_id,
                target=target_name,
                candidate_path=Path(candidate_path),
                default_branch=default_branch,
            )
            asset_outcomes.append(
                successful_asset_outcome(
                    asset_type="tool_description",
                    target=target_name,
                    status=status,
                    candidate_path=candidate_path,
                    reason=reason,
                    verification_status="passed",
                    evaluation=evaluation,
                    commit_sha=commit_sha,
                )
            )
        except Exception as exc:
            asset_outcomes.append(
                failed_asset_outcome(asset_type="tool_description", target=target_name, exc=exc)
            )

    for target_path in list_code_targets_fn(default_branch):
        asset_outcomes.append(
            _skipped_outcome("code", target_path, "code_self_evolution_placeholder")
        )

    return finalize_agent_self_evolution_result(
        agent_type=spec.agent_type,
        outputs=outputs,
        asset_outcomes=asset_outcomes,
    )
