"""Evaluation helpers for daily audit self-evolution."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DailyAuditEvalExample:
    task_input: str
    expected_behavior: str
    source_run_id: str
    unit_label: str = ""
    file_path: str = ""
    recommended_action: str = ""
    used_subagents: tuple[str, ...] = ()


@dataclass(frozen=True)
class HeldoutEvaluationResult:
    baseline_score: float | None
    candidate_score: float | None
    heldout_examples: int
    gate_reason: str | None = None


def split_eval_examples(
    examples: list[DailyAuditEvalExample],
) -> tuple[list[DailyAuditEvalExample], list[DailyAuditEvalExample], list[DailyAuditEvalExample]]:
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


def predict_with_module(module: Any, task_input: str) -> Any:
    if callable(module):
        try:
            return module(task_input=task_input)
        except TypeError:
            return module(task_input)
    output = str(getattr(module, "skill_text", "") or getattr(module, "asset_text", "") or "").strip()
    return type("Prediction", (), {"output": output})()


def average_metric(module: Any, examples: list[DailyAuditEvalExample], metric: Callable[[Any, Any, Any], float]) -> float | None:
    if not examples:
        return None
    scores = []
    for example in examples:
        prediction = predict_with_module(module, example.task_input)
        scores.append(float(metric(example, prediction)))
    return sum(scores) / len(scores)


def _token_set(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text) if len(token) > 2}


def _expected_overlap_score(example: DailyAuditEvalExample, output: str) -> float:
    expected_words = _token_set(example.expected_behavior)
    output_words = _token_set(output)
    if not output.strip():
        return 0.0
    if not expected_words:
        return 0.5
    return len(expected_words & output_words) / len(expected_words)


def _unit_focus_score(example: DailyAuditEvalExample, output: str) -> float:
    hints = []
    if example.unit_label:
        hints.append(example.unit_label.lower())
    if example.file_path:
        hints.append(example.file_path.lower())
        hints.append(Path(example.file_path).name.lower())
    if not hints:
        return 0.5
    lowered = output.lower()
    hits = sum(1 for hint in hints if hint and hint in lowered)
    return hits / len(hints)


def _boundedness_score(output: str) -> float:
    size = len(output.strip())
    if size == 0:
        return 0.0
    if 40 <= size <= 3_000:
        return 1.0
    if size <= 4_500:
        return 0.7
    return 0.2


def _action_alignment_score(example: DailyAuditEvalExample, output: str) -> float:
    if not example.recommended_action:
        return 0.5
    lowered = output.lower()
    if example.recommended_action == "autofix":
        return 1.0 if any(term in lowered for term in ("autofix", "fix", "patch")) else 0.3
    return 1.0 if "autofix" not in lowered else 0.2


def _subagent_alignment_score(example: DailyAuditEvalExample, output: str) -> float:
    if not example.used_subagents:
        return 0.5
    lowered = output.lower()
    hits = 0
    for subagent in example.used_subagents:
        terms = {subagent.lower(), subagent.replace("_", " ").lower(), subagent.replace("_", "-").lower()}
        if any(term in lowered for term in terms):
            hits += 1
    return hits / len(example.used_subagents)


def _structure_score(output: str) -> float:
    if not output.strip():
        return 0.0
    if len(output) > 4_500:
        return 0.2
    if any(marker in output for marker in ("\n- ", "\n1.", "\n## ", "\n### ")):
        return 1.0
    if "\n" in output:
        return 0.7
    return 0.4


def metric_for_asset_type(asset_type: str) -> Callable[[Any, Any, Any], float]:
    def metric(example, prediction, trace=None) -> float:
        del trace
        output = str(getattr(prediction, "output", "") or "")
        overlap = _expected_overlap_score(example, output)
        unit_focus = _unit_focus_score(example, output)
        boundedness = _boundedness_score(output)
        action = _action_alignment_score(example, output)
        subagent = _subagent_alignment_score(example, output)
        structure = _structure_score(output)
        if asset_type == "tool_description":
            score = (0.2 * overlap) + (0.1 * unit_focus) + (0.2 * boundedness) + (0.5 * subagent)
        elif asset_type == "prompt":
            score = (0.35 * overlap) + (0.15 * unit_focus) + (0.2 * boundedness) + (0.1 * action) + (0.2 * structure)
        else:
            score = (0.55 * overlap) + (0.2 * unit_focus) + (0.15 * boundedness) + (0.1 * action)
        return max(0.0, min(1.0, score))

    return metric


def evaluate_text_candidate(
    *,
    baseline_module: Any,
    candidate_module: Any,
    heldout_examples: list[DailyAuditEvalExample],
    asset_type: str,
    target_name: str,
) -> HeldoutEvaluationResult:
    if not heldout_examples:
        return HeldoutEvaluationResult(None, None, 0, None)
    metric = metric_for_asset_type(asset_type)
    baseline_score = average_metric(baseline_module, heldout_examples, metric)
    candidate_score = average_metric(candidate_module, heldout_examples, metric)
    gate_reason = None
    if baseline_score is not None and candidate_score is not None and candidate_score + 1e-9 < baseline_score:
        gate_reason = (
            f"held-out regression for {target_name}: baseline={baseline_score:.3f} "
            f"candidate={candidate_score:.3f}"
        )
    return HeldoutEvaluationResult(
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        heldout_examples=len(heldout_examples),
        gate_reason=gate_reason,
    )
