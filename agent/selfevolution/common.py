"""Shared helpers for agent-scoped self-evolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import litellm
from gepa.optimize_anything import EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything
from gepa.logging import logger as gepa_logger

from agent.config import settings
from agent.selfevolution.repo import self_repo_enabled
from agent.scenes.daily_audit.selfevolution.evaluation import (
    DailyAuditEvalExample,
    HeldoutEvaluationResult,
    evaluate_text_candidate,
    metric_for_asset_type,
    split_eval_examples,
)
from agent.utils.model import resolve_llm_config

_PROMPT_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _patch_gepa_logger_tee_stream_safety() -> None:
    if getattr(gepa_logger.Tee, "_open_review_stream_safe_patch", False):
        return

    def _safe_write(self, obj):
        alive = []
        for index, handle in enumerate(getattr(self, "files", ())):
            try:
                handle.write(obj)
                alive.append(handle)
            except ValueError as exc:
                if "closed file" not in str(exc).lower():
                    raise
                if index == 0:
                    raise
            except Exception:
                if index == 0:
                    raise
        self.files = tuple(alive)

    def _safe_flush(self):
        alive = []
        for index, handle in enumerate(getattr(self, "files", ())):
            if not hasattr(handle, "flush"):
                alive.append(handle)
                continue
            try:
                handle.flush()
                alive.append(handle)
            except ValueError as exc:
                if "closed file" not in str(exc).lower():
                    raise
                if index == 0:
                    raise
            except Exception:
                if index == 0:
                    raise
        self.files = tuple(alive)

    def _safe_close(self):
        remaining = []
        for index, handle in enumerate(getattr(self, "files", ())):
            if index == 0:
                remaining.append(handle)
                continue
            if hasattr(handle, "close"):
                try:
                    handle.close()
                except Exception:
                    pass
        self.files = tuple(remaining)

    gepa_logger.Tee.write = _safe_write
    gepa_logger.Tee.flush = _safe_flush
    gepa_logger.Tee.close = _safe_close
    gepa_logger.Tee._open_review_stream_safe_patch = True


_patch_gepa_logger_tee_stream_safety()


@dataclass(frozen=True)
class AgentSelfEvolutionSpec:
    agent_type: str
    skill_root: Callable[[str | None], Path]
    prompt_root: Callable[[str | None], Path]
    tool_metadata_path: Callable[[str | None], Path]
    code_targets_path: Callable[[str | None], Path]
    build_skill_examples: Callable[[str, int], list[DailyAuditEvalExample]]
    build_prompt_examples: Callable[[str, str], list[DailyAuditEvalExample]]
    build_tool_examples: Callable[[str, str], list[DailyAuditEvalExample]]
    prompt_allowlist: tuple[str, ...] = ()
    list_skills_override: Callable[[str | None], list[str]] | None = None
    list_prompt_targets_override: Callable[[str | None], list[str]] | None = None
    list_tool_description_targets_override: Callable[[str | None], list[str]] | None = None
    list_code_targets_override: Callable[[str | None], list[str]] | None = None
    build_prompt_eval_examples: Callable[[str, str, int], list[Any]] | None = None
    render_prompt_candidate: Callable[[str, str, Any], str] | None = None
    evaluation_profile: Callable[[str], dict[str, float]] | None = None
    apply_skill_candidate: Callable[[str, str, Path, str | None], Any] | None = None
    apply_prompt_candidate: Callable[[str, str, Path, str | None], Any] | None = None
    apply_tool_description_candidate: Callable[[str, str, Path, str | None], Any] | None = None
    apply_code_candidate: Callable[[str, str, Path, str | None], Any] | None = None


@dataclass(frozen=True)
class SelfEvolutionAssetOutcome:
    asset_type: str
    target: str
    status: str
    reason: str | None = None
    candidate_path: str | None = None
    verification_status: str | None = None
    baseline_score: float | None = None
    candidate_score: float | None = None
    heldout_examples: int | None = None
    gate_reason: str | None = None
    commit_sha: str | None = None
    train_count: int | None = None
    val_count: int | None = None
    heldout_count: int | None = None
    dimension_scores_summary: dict[str, float] | None = None
    feedback_coverage: int | None = None
    materialization_failures: int | None = None


@dataclass
class AgentSelfEvolutionResult:
    agent_type: str
    status: str
    reason: str | None = None
    outputs: list[Path | str] = field(default_factory=list)
    asset_outcomes: list[SelfEvolutionAssetOutcome] = field(default_factory=list)

    @property
    def output_count(self) -> int:
        return len(self.outputs)


class SelfEvolutionSkipped(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = str(reason or "self_evolution_skipped")


class SelfEvolutionRejected(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = str(reason or "self_evolution_rejected")


def _is_productive_asset_outcome(outcome: SelfEvolutionAssetOutcome) -> bool:
    if outcome.status in {"applied", "candidate_generated"}:
        return True
    return outcome.status == "skipped" and str(outcome.reason or "").strip() == "already_applied"


def _preferred_skip_reason(asset_outcomes: list[SelfEvolutionAssetOutcome]) -> str:
    first_rejected = next((item for item in asset_outcomes if item.status == "rejected"), None)
    if first_rejected is not None:
        return first_rejected.reason or first_rejected.gate_reason or "self_evolution_rejected"
    for preferred_reason in ("no_targets_configured", "no_recorded_raw_runs", "self_evolution_disabled"):
        if any((item.reason or "") == preferred_reason for item in asset_outcomes):
            return preferred_reason
    first_reason = next((item.reason for item in asset_outcomes if item.reason), None)
    return first_reason or "no_candidates_generated"


def evolution_root(agent_type: str, project_id: str) -> Path:
    return Path(settings.OPEN_REVIEW_RUNTIME_ROOT) / agent_type / "evolution" / project_id.replace("/", "__")


def _parse_skill_document(raw: str) -> tuple[str, str]:
    if raw.strip().startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[1].strip(), parts[2].strip()
    return "", raw.strip()


def _reassemble_skill(frontmatter: str, body: str) -> str:
    if frontmatter:
        return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"
    return f"{body.strip()}\n"


def list_skills(spec: AgentSelfEvolutionSpec, *, default_branch: str | None = None) -> list[str]:
    root = spec.skill_root(default_branch)
    return sorted({path.parent.name for path in root.rglob("SKILL.md")})


def find_skill(spec: AgentSelfEvolutionSpec, skill_name: str, *, default_branch: str | None = None) -> Path:
    root = spec.skill_root(default_branch)
    for skill_md in root.rglob("SKILL.md"):
        if skill_md.parent.name == skill_name:
            return skill_md
    raise FileNotFoundError(f"Could not find {spec.agent_type} skill '{skill_name}'")


def list_prompt_targets(spec: AgentSelfEvolutionSpec, *, default_branch: str | None = None) -> list[str]:
    root = spec.prompt_root(default_branch)
    return sorted(path.stem for path in root.glob("*.md"))


def load_prompt_asset_text(spec: AgentSelfEvolutionSpec, target_name: str, *, default_branch: str | None = None) -> str:
    path = spec.prompt_root(default_branch) / f"{target_name}.md"
    return path.read_text(encoding="utf-8")


def load_tool_descriptions(spec: AgentSelfEvolutionSpec, *, default_branch: str | None = None) -> dict[str, str]:
    return json.loads(spec.tool_metadata_path(default_branch).read_text(encoding="utf-8"))


def list_tool_description_targets(spec: AgentSelfEvolutionSpec, *, default_branch: str | None = None) -> list[str]:
    return sorted(load_tool_descriptions(spec, default_branch=default_branch))


def list_code_targets(spec: AgentSelfEvolutionSpec, *, default_branch: str | None = None) -> list[str]:
    return json.loads(spec.code_targets_path(default_branch).read_text(encoding="utf-8"))


class TextAssetModule:
    def __init__(self, asset_text: str):
        self.asset_text = asset_text


class SkillModule:
    def __init__(self, skill_text: str):
        self.skill_text = skill_text


def render_prompt_template(candidate_text: str, values: dict[str, Any]) -> str:
    """Replace simple ``{placeholder}`` tokens without interpreting unrelated braces."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return str(values[key])

    return _PROMPT_PLACEHOLDER_RE.sub(_replace, candidate_text)


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


def _extract_candidate_text(value: Any) -> str:
    def _strip_code_fence(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return text
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return text
        return "\n".join(lines[1:-1]).strip()

    def _extract(item: Any) -> str:
        if isinstance(item, str):
            stripped = _strip_code_fence(item).strip()
            if stripped[:1] in {"{", "["}:
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    return item
                return _extract(parsed)
            return item
        if isinstance(item, dict):
            for key in ("current_candidate", "content", "prompt", "candidate_text", "text"):
                if key in item:
                    extracted = _extract(item.get(key))
                    if str(extracted or "").strip():
                        return extracted
            if len(item) == 1:
                return _extract(next(iter(item.values())))
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, list):
            if len(item) == 1:
                return _extract(item[0])
            return json.dumps(item, ensure_ascii=False)
        return str(item or "")

    normalized = _extract(value)
    return normalized if isinstance(normalized, str) else str(normalized or "")


def _candidate_dir(agent_type: str, project_id: str, category: str, target_name: str) -> Path:
    return evolution_root(agent_type, project_id) / "candidates" / category / target_name


def write_skill_candidate(*, spec: AgentSelfEvolutionSpec, project_id: str, skill_name: str, content: str) -> Path:
    root = _candidate_dir(spec.agent_type, project_id, "skills", skill_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_prompt_candidate(*, spec: AgentSelfEvolutionSpec, project_id: str, target_name: str, content: str) -> Path:
    root = _candidate_dir(spec.agent_type, project_id, "prompts", target_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{target_name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_tool_description_candidate(*, spec: AgentSelfEvolutionSpec, project_id: str, target_name: str, content: str) -> Path:
    root = _candidate_dir(spec.agent_type, project_id, "tool_descriptions", target_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "tool_description.txt"
    path.write_text(content, encoding="utf-8")
    return path


def write_code_candidate(*, spec: AgentSelfEvolutionSpec, project_id: str, target_path: str, content: str) -> Path:
    root = _candidate_dir(spec.agent_type, project_id, "code", Path(target_path).stem.replace(".", "-"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / Path(target_path).name
    path.write_text(content, encoding="utf-8")
    return path


def _evaluation_fields(evaluation: HeldoutEvaluationResult) -> dict[str, float | int | str | None]:
    return {
        "baseline_score": evaluation.baseline_score,
        "candidate_score": evaluation.candidate_score,
        "heldout_examples": evaluation.heldout_examples,
        "gate_reason": evaluation.gate_reason,
    }


def _evaluate_text_asset_example(
    *,
    asset_type: str,
    candidate_text: str,
    example: DailyAuditEvalExample,
) -> tuple[float, dict[str, Any]]:
    metric = metric_for_asset_type(asset_type)
    prediction = SimpleNamespace(output=str(candidate_text or "").strip())
    score = float(metric(example, prediction, None))
    return score, {
        "task_input": example.task_input,
        "expected_behavior": example.expected_behavior,
        "candidate_text": candidate_text,
        "source_run_id": example.source_run_id,
    }


def _evaluate_text_asset_examples(
    *,
    baseline_text: str,
    candidate_text: str,
    heldout_examples: list[DailyAuditEvalExample],
    asset_type: str,
    target_name: str,
) -> HeldoutEvaluationResult:
    baseline_module = SkillModule(baseline_text) if asset_type == "skill" else TextAssetModule(baseline_text)
    candidate_module = SkillModule(candidate_text) if asset_type == "skill" else TextAssetModule(candidate_text)
    return evaluate_text_candidate(
        baseline_module=baseline_module,
        candidate_module=candidate_module,
        heldout_examples=heldout_examples,
        asset_type=asset_type,
        target_name=target_name,
    )


def successful_asset_outcome(
    *,
    asset_type: str,
    target: str,
    status: str,
    candidate_path: Path | str | None,
    reason: str | None = None,
    verification_status: str | None = None,
    evaluation: HeldoutEvaluationResult | None = None,
    commit_sha: str | None = None,
) -> SelfEvolutionAssetOutcome:
    fields = _evaluation_fields(evaluation) if evaluation is not None else {}
    return SelfEvolutionAssetOutcome(
        asset_type=asset_type,
        target=target,
        status=status,
        reason=reason,
        candidate_path=str(candidate_path) if candidate_path is not None else None,
        verification_status=verification_status,
        baseline_score=fields.get("baseline_score"),
        candidate_score=fields.get("candidate_score"),
        heldout_examples=fields.get("heldout_examples"),
        gate_reason=fields.get("gate_reason"),
        commit_sha=commit_sha,
    )


def failed_asset_outcome(*, asset_type: str, target: str, exc: Exception) -> SelfEvolutionAssetOutcome:
    if isinstance(exc, SelfEvolutionSkipped):
        return SelfEvolutionAssetOutcome(asset_type=asset_type, target=target, status="skipped", reason=exc.reason)
    if isinstance(exc, SelfEvolutionRejected):
        return SelfEvolutionAssetOutcome(
            asset_type=asset_type,
            target=target,
            status="rejected",
            reason=exc.reason,
            gate_reason=exc.reason,
        )
    reason = str(exc).strip() or exc.__class__.__name__
    return SelfEvolutionAssetOutcome(asset_type=asset_type, target=target, status="failed", reason=reason)


def finalize_agent_self_evolution_result(
    *,
    agent_type: str,
    outputs: list[Path | str],
    asset_outcomes: list[SelfEvolutionAssetOutcome],
) -> AgentSelfEvolutionResult:
    if not asset_outcomes:
        return AgentSelfEvolutionResult(
            agent_type=agent_type,
            status="skipped",
            reason="no_targets_configured",
            outputs=[],
            asset_outcomes=[],
        )
    first_failed = next((item for item in asset_outcomes if item.status == "failed"), None)
    if first_failed is not None:
        return AgentSelfEvolutionResult(
            agent_type=agent_type,
            status="failed",
            reason=first_failed.reason or first_failed.gate_reason or "self_evolution_failed",
            outputs=list(outputs),
            asset_outcomes=list(asset_outcomes),
        )
    if any(_is_productive_asset_outcome(item) for item in asset_outcomes):
        return AgentSelfEvolutionResult(
            agent_type=agent_type,
            status="reported",
            outputs=list(outputs),
            asset_outcomes=list(asset_outcomes),
        )
    return AgentSelfEvolutionResult(
        agent_type=agent_type,
        status="skipped",
        reason=_preferred_skip_reason(list(asset_outcomes)),
        outputs=list(outputs),
        asset_outcomes=list(asset_outcomes),
    )


def merge_agent_self_evolution_results(*results: AgentSelfEvolutionResult) -> AgentSelfEvolutionResult:
    filtered = [result for result in results if result is not None]
    if not filtered:
        return AgentSelfEvolutionResult(
            agent_type="unknown",
            status="skipped",
            reason="no_targets_configured",
            outputs=[],
            asset_outcomes=[],
        )
    outputs = [item for result in filtered for item in (result.outputs or [])]
    asset_outcomes = [item for result in filtered for item in (result.asset_outcomes or [])]
    first_failed = next((item for item in asset_outcomes if item.status == "failed"), None)
    if first_failed is not None:
        return AgentSelfEvolutionResult(
            agent_type=filtered[0].agent_type,
            status="failed",
            reason=first_failed.reason or first_failed.gate_reason or "self_evolution_failed",
            outputs=outputs,
            asset_outcomes=asset_outcomes,
        )
    if any(_is_productive_asset_outcome(item) for item in asset_outcomes):
        return AgentSelfEvolutionResult(
            agent_type=filtered[0].agent_type,
            status="reported",
            outputs=outputs,
            asset_outcomes=asset_outcomes,
        )
    return AgentSelfEvolutionResult(
        agent_type=filtered[0].agent_type,
        status="skipped",
        reason=_preferred_skip_reason(asset_outcomes),
        outputs=outputs,
        asset_outcomes=asset_outcomes,
    )


def run_skill_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    skill_name: str,
    iterations: int = 10,
    default_branch: str | None = None,
) -> tuple[Path, HeldoutEvaluationResult]:
    examples = spec.build_skill_examples(project_id, limit=20)
    if not examples:
        raise SelfEvolutionSkipped("no_recorded_raw_runs")
    skill_path = find_skill(spec, skill_name, default_branch=default_branch)
    frontmatter, body = _parse_skill_document(skill_path.read_text(encoding="utf-8"))
    train_examples, val_examples, heldout_examples = split_eval_examples(examples)
    run_dir = evolution_root(spec.agent_type, project_id) / "runs" / "skills" / skill_name
    run_dir.mkdir(parents=True, exist_ok=True)
    result = optimize_anything(
        seed_candidate=body,
        evaluator=lambda candidate, example: _evaluate_text_asset_example(
            asset_type="skill",
            candidate_text=_extract_candidate_text(candidate),
            example=example,
        ),
        dataset=train_examples,
        valset=val_examples or train_examples,
        objective=(
            f"Improve the {spec.agent_type} skill '{skill_name}' so it better captures reusable workflow "
            "guidance from recorded successful runs."
        ),
        background=(
            "Optimize only the skill text. Higher score means the skill text better aligns with the "
            "task inputs and expected behavior from recorded examples."
        ),
        config=GEPAConfig(
            engine=EngineConfig(
                run_dir=str(run_dir),
                max_metric_calls=max(8, iterations * 4),
                parallel=False,
                cache_evaluation=True,
                cache_evaluation_storage="disk",
                candidate_selection_strategy="pareto",
            ),
            reflection=ReflectionConfig(
                reflection_lm=_make_reflection_lm(),
                reflection_minibatch_size=3,
            ),
        ),
    )
    evolved_body = _extract_candidate_text(getattr(result, "best_candidate", body)).strip()
    if not evolved_body:
        raise RuntimeError("Optimizer returned an empty evolved skill body")
    evaluation = _evaluate_text_asset_examples(
        baseline_text=body,
        candidate_text=evolved_body,
        heldout_examples=heldout_examples,
        asset_type="skill",
        target_name=skill_name,
    )
    if evaluation.gate_reason:
        raise SelfEvolutionRejected(evaluation.gate_reason)
    return (
        write_skill_candidate(
            spec=spec,
            project_id=project_id,
            skill_name=skill_name,
            content=_reassemble_skill(frontmatter, evolved_body),
        ),
        evaluation,
    )


def _apply_status_and_commit(
    *,
    spec: AgentSelfEvolutionSpec,
    asset_type: str,
    project_id: str,
    target: str,
    candidate_path: Path,
    default_branch: str | None = None,
) -> tuple[str, str | None, str | None]:
    if not self_repo_enabled():
        return "candidate_generated", None, None

    apply_fn = {
        "skill": spec.apply_skill_candidate,
        "prompt": spec.apply_prompt_candidate,
        "tool_description": spec.apply_tool_description_candidate,
        "code": spec.apply_code_candidate,
    }.get(asset_type)
    if not callable(apply_fn):
        return "candidate_generated", None, None

    result = apply_fn(project_id, target, candidate_path, default_branch)
    if isinstance(result, str):
        return "applied", None, result
    if result is None:
        return "skipped", "already_applied", None
    status = str(getattr(result, "status", None) or "candidate_generated")
    reason = getattr(result, "reason", None)
    commit_sha = getattr(result, "commit_sha", None)
    return status, reason, commit_sha


def _run_text_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    target_name: str,
    baseline_text: str,
    asset_type: str,
    examples: list[DailyAuditEvalExample],
    iterations: int = 10,
) -> tuple[str, HeldoutEvaluationResult]:
    if not examples:
        raise SelfEvolutionSkipped("no_recorded_raw_runs")
    train_examples, val_examples, heldout_examples = split_eval_examples(examples)
    run_dir = evolution_root(spec.agent_type, project_id) / "runs" / asset_type / target_name
    run_dir.mkdir(parents=True, exist_ok=True)
    result = optimize_anything(
        seed_candidate=baseline_text,
        evaluator=lambda candidate, example: _evaluate_text_asset_example(
            asset_type=asset_type,
            candidate_text=_extract_candidate_text(candidate),
            example=example,
        ),
        dataset=train_examples,
        valset=val_examples or train_examples,
        objective=(
            f"Improve the {spec.agent_type} {asset_type} asset '{target_name}' so it better aligns "
            "with recorded expected behavior."
        ),
        background=(
            "Optimize only this text asset. Higher score means the candidate text better reflects the "
            "expected behavior captured in the recorded examples."
        ),
        config=GEPAConfig(
            engine=EngineConfig(
                run_dir=str(run_dir),
                max_metric_calls=max(8, iterations * 4),
                parallel=False,
                cache_evaluation=True,
                cache_evaluation_storage="disk",
                candidate_selection_strategy="pareto",
            ),
            reflection=ReflectionConfig(
                reflection_lm=_make_reflection_lm(),
                reflection_minibatch_size=3,
            ),
        ),
    )
    evolved_text = _extract_candidate_text(getattr(result, "best_candidate", baseline_text)).strip()
    if not evolved_text:
        raise RuntimeError("Optimizer returned an empty evolved text asset")
    evaluation = _evaluate_text_asset_examples(
        baseline_text=baseline_text,
        candidate_text=evolved_text,
        heldout_examples=heldout_examples,
        asset_type=asset_type,
        target_name=target_name,
    )
    if evaluation.gate_reason:
        raise SelfEvolutionRejected(evaluation.gate_reason)
    return evolved_text, evaluation


def run_prompt_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    target_name: str,
    iterations: int = 10,
    default_branch: str | None = None,
) -> tuple[Path, HeldoutEvaluationResult]:
    baseline_text = load_prompt_asset_text(spec, target_name, default_branch=default_branch)
    evolved_text, evaluation = _run_text_evolution(
        spec=spec,
        project_id=project_id,
        target_name=target_name,
        baseline_text=baseline_text,
        asset_type="prompt",
        examples=spec.build_prompt_examples(project_id, target_name),
        iterations=iterations,
    )
    return (
        write_prompt_candidate(spec=spec, project_id=project_id, target_name=target_name, content=evolved_text),
        evaluation,
    )


def run_tool_description_evolution(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    target_name: str,
    iterations: int = 10,
    default_branch: str | None = None,
) -> tuple[Path, HeldoutEvaluationResult]:
    baseline_text = load_tool_descriptions(spec, default_branch=default_branch)[target_name]
    evolved_text, evaluation = _run_text_evolution(
        spec=spec,
        project_id=project_id,
        target_name=target_name,
        baseline_text=baseline_text,
        asset_type="tool_description",
        examples=spec.build_tool_examples(project_id, target_name),
        iterations=iterations,
    )
    return (
        write_tool_description_candidate(
            spec=spec,
            project_id=project_id,
            target_name=target_name,
            content=evolved_text,
        ),
        evaluation,
    )


def run_text_self_evolution_for_spec(
    *,
    spec: AgentSelfEvolutionSpec,
    project_id: str,
    default_branch: str | None = None,
    enabled: bool,
    include_skills: bool = True,
    include_prompts: bool = True,
    include_tools: bool = True,
    include_code: bool = True,
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

    for skill_name in list_skills(spec, default_branch=default_branch) if include_skills else []:
        try:
            candidate_path, evaluation = run_skill_evolution(
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

    for target_name in list_prompt_targets(spec, default_branch=default_branch) if include_prompts else []:
        try:
            candidate_path, evaluation = run_prompt_evolution(
                spec=spec,
                project_id=project_id,
                target_name=target_name,
                default_branch=default_branch,
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
                successful_asset_outcome(
                    asset_type="prompt",
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
            asset_outcomes.append(failed_asset_outcome(asset_type="prompt", target=target_name, exc=exc))

    for target_name in list_tool_description_targets(spec, default_branch=default_branch) if include_tools else []:
        try:
            candidate_path, evaluation = run_tool_description_evolution(
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

    for target_path in list_code_targets(spec, default_branch=default_branch) if include_code else []:
        asset_outcomes.append(
            SelfEvolutionAssetOutcome(
                asset_type="code",
                target=target_path,
                status="skipped",
                reason="code_self_evolution_placeholder",
            )
        )

    return finalize_agent_self_evolution_result(
        agent_type=spec.agent_type,
        outputs=outputs,
        asset_outcomes=asset_outcomes,
    )
