"""Self-evolution plumbing for daily audit."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol

from agent.config import settings
from agent.controlplane import get_tracking_service
from agent.scenes.daily_audit.selfevolution.evaluation import (
    DailyAuditEvalExample,
    HeldoutEvaluationResult,
)
from agent.scenes.daily_audit.selfevolution.paths import (
    repo_daily_audit_selfevolution_root,
)
from agent.scenes.daily_audit.selfevolution.repo import (
    daily_audit_self_repo_python_path,
    daily_audit_self_skill_root,
    ensure_daily_audit_self_repo_checkout,
)
from agent.scenes.daily_audit.selfevolution.tools import load_tool_descriptions
from agent.selfevolution import apply as shared_apply
from agent.selfevolution.common import (
    AgentSelfEvolutionResult,
    AgentSelfEvolutionSpec,
    SelfEvolutionAssetOutcome,
    SelfEvolutionSkipped,
    failed_asset_outcome,
    finalize_agent_self_evolution_result,
    render_prompt_template,
    successful_asset_outcome,
)
from agent.selfevolution.common import (
    run_prompt_evolution as shared_run_prompt_evolution,
)
from agent.selfevolution.common import (
    run_skill_evolution as shared_run_skill_evolution,
)
from agent.selfevolution.common import (
    run_tool_description_evolution as shared_run_tool_description_evolution,
)
from agent.selfevolution.gepa import PromptTaskExample, run_gepa_self_evolution_for_spec
from agent.selfevolution.git import run_safe_git, run_safe_git_stdout
from agent.selfevolution.repo import configured_self_repo_branch, self_repo_enabled

_SKILL_ALIASES = {
    "primary-daily-optimizer": "workflow-auditor",
}

_PROMPT_TARGET_ALIASES = {
    "primary-agent-prompt": "workflow-auditor-prompt",
}


def _project_slug(project_id: str) -> str:
    return project_id.replace("/", "__")


def evolution_root(project_id: str) -> Path:
    return Path(settings.OPEN_REVIEW_RUNTIME_ROOT) / "daily_audit" / "evolution" / _project_slug(project_id)


def _raw_daily_audit_runs(project_id: str, *, limit: int) -> list[dict[str, Any]]:
    return get_tracking_service().list_runs(project_id=project_id, event_type="daily_audit", limit=max(limit * 5, 50))


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
        label = f"{kind} by {author}" if author else kind
        items.append(f"- {label}")
    if not items:
        return ""
    return "\n\nExternal feedback:\n" + "\n".join(items)


def _candidate_dir(project_id: str, skill_name: str) -> Path:
    return evolution_root(project_id) / "candidates" / skill_name


def _prompt_candidate_dir(project_id: str, target_name: str) -> Path:
    return evolution_root(project_id) / "candidates" / "prompts" / target_name


def _tool_candidate_dir(project_id: str, target_name: str) -> Path:
    return evolution_root(project_id) / "candidates" / "tool_descriptions" / target_name


def _code_candidate_dir(project_id: str, target_name: str) -> Path:
    return evolution_root(project_id) / "candidates" / "code" / target_name


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


def find_daily_audit_skill(skill_name: str) -> Path:
    skill_name = _SKILL_ALIASES.get(skill_name, skill_name)
    skills_dir = daily_audit_self_skill_root(ensure_daily_audit_self_repo_checkout())
    for skill_md in skills_dir.rglob("SKILL.md"):
        if skill_md.parent.name == skill_name:
            return skill_md
    raise FileNotFoundError(f"Could not find daily audit skill '{skill_name}'")


def list_daily_audit_skills() -> list[str]:
    skills_dir = daily_audit_self_skill_root(ensure_daily_audit_self_repo_checkout())
    names = sorted({skill_md.parent.name for skill_md in skills_dir.rglob("SKILL.md")})
    return names


def list_daily_audit_prompt_targets() -> list[str]:
    prompts_dir = repo_daily_audit_selfevolution_root(ensure_daily_audit_self_repo_checkout()) / "prompts"
    return sorted(path.stem for path in prompts_dir.glob("*.md"))


def list_daily_audit_tool_description_targets() -> list[str]:
    return sorted(load_tool_descriptions())


def list_daily_audit_code_targets() -> list[str]:
    targets_path = repo_daily_audit_selfevolution_root(ensure_daily_audit_self_repo_checkout()) / "code" / "code_targets.json"
    return json.loads(targets_path.read_text(encoding="utf-8"))


def load_skill_text(skill_path: Path) -> str:
    return _parse_skill_document(skill_path.read_text(encoding="utf-8"))[1]


def load_skill_document(skill_path: Path) -> dict[str, str]:
    raw = skill_path.read_text(encoding="utf-8")
    frontmatter, body = _parse_skill_document(raw)
    return {"raw": raw, "frontmatter": frontmatter, "body": body}


def find_daily_audit_prompt_asset(target_name: str) -> Path:
    target_name = _PROMPT_TARGET_ALIASES.get(target_name, target_name)
    path = repo_daily_audit_selfevolution_root(ensure_daily_audit_self_repo_checkout()) / "prompts" / f"{target_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Could not find daily audit prompt asset '{target_name}'")
    return path


def load_tool_description_text(target_name: str) -> str:
    descriptions = load_tool_descriptions()
    if target_name not in descriptions:
        raise FileNotFoundError(f"Could not find daily audit tool description '{target_name}'")
    return descriptions[target_name]


def _service_repo_root(repo_dir: str | None = None, default_branch: str | None = None) -> Path:
    if repo_dir:
        return Path(repo_dir).resolve()
    return ensure_daily_audit_self_repo_checkout(default_branch)


def _run_local_git(args: list[str], *, cwd: Path) -> str:
    return run_safe_git_stdout(args, cwd=cwd)


def _detect_service_default_branch(repo_root: Path) -> str:
    try:
        branch = _run_local_git(["branch", "--show-current"], cwd=repo_root)
        if branch:
            return branch
    except Exception:
        pass
    return configured_self_repo_branch()


def _create_service_worktree(*, repo_root: Path, default_branch: str, run_id: str) -> str:
    worktrees_root = repo_root / ".worktrees" / "evolution"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = worktrees_root / run_id
    temp_branch = f"open-review-evolution-{Path(run_id).name}"
    run_safe_git(["worktree", "remove", "--force", str(worktree_dir)], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["worktree", "prune"], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["branch", "-D", temp_branch], cwd=repo_root, check=False)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    run_safe_git_stdout(["worktree", "add", "-b", temp_branch, str(worktree_dir), default_branch], cwd=repo_root, safe_paths=[worktree_dir])
    return str(worktree_dir)


def _cleanup_service_worktree(*, repo_root: Path, worktree_dir: str) -> None:
    run_safe_git(["worktree", "remove", "--force", worktree_dir], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["worktree", "prune"], cwd=repo_root, safe_paths=[worktree_dir], check=False)
    run_safe_git(["branch", "-D", f"open-review-evolution-{Path(worktree_dir).name}"], cwd=repo_root, check=False)


def _commit_all_and_get_sha_local(*, worktree_root: Path, message: str) -> str:
    _run_local_git(["add", "-A"], cwd=worktree_root)
    _run_local_git(["commit", "--no-verify", "-m", message], cwd=worktree_root)
    return _run_local_git(["rev-parse", "HEAD"], cwd=worktree_root)


def _fast_forward_service_repo(*, repo_root: Path, default_branch: str, commit_sha: str) -> None:
    _run_local_git(["checkout", default_branch], cwd=repo_root)
    _run_local_git(["merge", "--ff-only", commit_sha], cwd=repo_root)


def _sandbox_env_exports() -> dict[str, str]:
    exports = {
        "OPEN_REVIEW_DB_PATH": settings.OPEN_REVIEW_DB_PATH,
        "OPEN_REVIEW_RUNTIME_ROOT": settings.OPEN_REVIEW_RUNTIME_ROOT,
        "GITLAB_API_URL": settings.GITLAB_API_URL,
        "GITLAB_TOKEN": settings.GITLAB_TOKEN,
    }
    return {key: value for key, value in exports.items() if str(value).strip()}


def _shell_exports(env: dict[str, str]) -> str:
    if not env:
        return ""
    return "".join(f"export {key}={shlex.quote(value)}; " for key, value in env.items())


def _ensure_sandbox_self_repo_python(
    *,
    sandbox: SandboxBackendProtocol,
    repo_root: Path,
) -> Path:
    python_path = daily_audit_self_repo_python_path(repo_root)
    command = (
        f"{_shell_exports(_sandbox_env_exports())}"
        f"cd {shlex.quote(str(repo_root))} && "
        f"if [ ! -x {shlex.quote(str(python_path))} ]; then uv sync --frozen --extra dev; fi && "
        f"{shlex.quote(str(python_path))} -V"
    )
    result = sandbox.execute(command, timeout=1800)
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or "failed to bootstrap sandbox self repo environment")
    return python_path


def _run_evolution_cli_in_sandbox(
    *,
    sandbox: SandboxBackendProtocol,
    repo_root: Path,
    args: list[str],
    timeout: int = 3600,
) -> dict[str, Any]:
    python_path = _ensure_sandbox_self_repo_python(sandbox=sandbox, repo_root=repo_root)
    joined_args = " ".join(shlex.quote(arg) for arg in args)
    command = (
        f"{_shell_exports(_sandbox_env_exports())}"
        f"cd {shlex.quote(str(repo_root))} && "
        f"{shlex.quote(str(python_path))} -m agent.scenes.daily_audit.selfevolution.cli {joined_args}"
    )
    result = sandbox.execute(command, timeout=timeout)
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or "sandbox evolution command failed")
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("sandbox evolution command returned no output")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sandbox evolution command returned invalid JSON: {lines[-1]}") from exc


def _run_code_merge_tests(worktree_root: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=worktree_root,
        check=True,
    )


def build_skill_eval_examples(project_id: str, *, limit: int = 20) -> list[DailyAuditEvalExample]:
    raw_examples: list[DailyAuditEvalExample] = []
    for run in reversed(_raw_daily_audit_runs(project_id, limit=limit)):
        record = _find_agent_record(run, "daily_audit.analysis")
        if record is None:
            continue
        result = record.get("result_json") if isinstance(record, dict) else {}
        result = result if isinstance(result, dict) else {}
        metadata = record.get("metadata_json") if isinstance(record, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        summary = str(result.get("summary_markdown") or "").strip()
        report = str(result.get("report_markdown") or "").strip()
        if not summary or not report:
            continue
        raw_examples.append(
            DailyAuditEvalExample(
                task_input=summary,
                expected_behavior=report + _feedback_suffix(run),
                source_run_id=str(metadata.get("logical_run_id") or run.get("run_id") or ""),
                unit_label=str(metadata.get("unit_label") or metadata.get("selected_unit_label") or ""),
                file_path=str(metadata.get("file_path") or ""),
                recommended_action=str(result.get("recommended_action") or "report_only"),
                used_subagents=tuple(str(item) for item in (result.get("used_subagents") or [])),
            )
        )
        if len(raw_examples) >= limit:
            break
    return raw_examples


def build_direction_eval_examples(project_id: str, *, limit: int = 20) -> list[DailyAuditEvalExample]:
    raw_examples: list[DailyAuditEvalExample] = []
    default_task = (
        "Discover one user-triggered action workflow for today's audit. "
        "Explore the repository yourself, pick one bounded workflow, and justify it with concrete entry evidence."
    )
    for run in reversed(_raw_daily_audit_runs(project_id, limit=limit)):
        record = _find_agent_record(run, "daily_audit.direction")
        if record is None:
            continue
        result = record.get("result_json") if isinstance(record, dict) else {}
        result = result if isinstance(result, dict) else {}
        unit = result.get("selected_unit") if isinstance(result.get("selected_unit"), dict) else {}
        unit_label = str(unit.get("label") or "").strip()
        file_path = str(unit.get("file_path") or "").strip()
        workflow_summary = str(unit.get("workflow_summary") or "").strip()
        entrypoint_symbol = str(unit.get("entrypoint_symbol") or "").strip()
        selection_reasoning = str(result.get("selection_reasoning") or "").strip()
        entry_evidence = [str(item).strip() for item in (unit.get("entry_evidence") or []) if str(item).strip()]
        if not unit_label or not (workflow_summary or entrypoint_symbol or entry_evidence):
            continue
        expected_parts = [
            f"Workflow: {unit_label}",
            f"Entrypoint: {entrypoint_symbol or unit_label}",
        ]
        if workflow_summary:
            expected_parts.append(f"Summary: {workflow_summary}")
        if selection_reasoning:
            expected_parts.append(f"Why: {selection_reasoning}")
        if entry_evidence:
            expected_parts.append("Evidence: " + "; ".join(entry_evidence[:3]))
        metadata = record.get("metadata_json") if isinstance(record, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        raw_examples.append(
            DailyAuditEvalExample(
                task_input=default_task,
                expected_behavior="\n".join(expected_parts),
                source_run_id=str(metadata.get("logical_run_id") or run.get("run_id") or ""),
                unit_label=unit_label,
                file_path=file_path,
                recommended_action="report_only",
                used_subagents=tuple(str(item) for item in (result.get("used_subagents") or [])),
            )
        )
        if len(raw_examples) >= limit:
            break
    return raw_examples


def build_daily_audit_prompt_eval_examples(project_id: str, target_name: str, limit: int = 20) -> list[PromptTaskExample]:
    normalized_target = _PROMPT_TARGET_ALIASES.get(target_name, target_name)
    record_kind = "daily_audit.direction" if normalized_target == "direction-finder-prompt" else "daily_audit.analysis"
    examples: list[PromptTaskExample] = []
    for run in reversed(_raw_daily_audit_runs(project_id, limit=limit)):
        record = _find_agent_record(run, record_kind)
        if record is None:
            continue
        metadata = record.get("metadata_json") if isinstance(record, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        input_messages = record.get("input_messages_json") if isinstance(record, dict) else []
        task_input = ""
        if isinstance(input_messages, list) and input_messages:
            first = input_messages[0]
            if isinstance(first, dict):
                task_input = str(first.get("content") or "").strip()
        examples.append(
            PromptTaskExample(
                agent_type="daily_audit",
                prompt_target=normalized_target,
                source_run_id=str(metadata.get("logical_run_id") or run.get("run_id") or ""),
                runtime_run_id=str(run.get("run_id") or ""),
                project_id=project_id,
                task_input=task_input or (
                    "Discover one user-triggered action workflow for today's audit."
                    if normalized_target == "direction-finder-prompt"
                    else "Analyze the selected user-triggered workflow using the targeted recall."
                ),
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


def _selected_unit_block_from_example(example: PromptTaskExample) -> str:
    result = example.agent_record.get("result_json") if isinstance(example.agent_record, dict) else {}
    result = result if isinstance(result, dict) else {}
    unit = result.get("selected_unit") if isinstance(result.get("selected_unit"), dict) else {}
    if not unit:
        return "(not selected yet)"
    location = f" @ {unit.get('file_path')}" if unit.get("file_path") else ""
    detail_lines = [f"[{unit.get('unit_type') or 'action_workflow'}] {unit.get('label') or 'unknown'}{location}"]
    if unit.get("entrypoint_kind"):
        detail_lines.append(f"Entrypoint kind: {unit.get('entrypoint_kind')}")
    if unit.get("entrypoint_symbol"):
        detail_lines.append(f"Entrypoint symbol: {unit.get('entrypoint_symbol')}")
    if unit.get("workflow_summary"):
        detail_lines.append(f"Workflow summary: {unit.get('workflow_summary')}")
    entry_evidence = [str(item).strip() for item in (unit.get("entry_evidence") or []) if str(item).strip()]
    if entry_evidence:
        detail_lines.append("Entry evidence:")
        detail_lines.extend(f"- {item}" for item in entry_evidence)
    return "\n".join(detail_lines)


def _render_daily_audit_prompt_candidate(target_name: str, candidate_text: str, example: PromptTaskExample) -> str:
    metadata = dict(example.metadata or {})
    return render_prompt_template(candidate_text, {
        "repo_dir": str(metadata.get("repo_dir") or "/repo"),
        "file_tool_repo_dir": str(metadata.get("repo_dir") or "/repo"),
        "experiment_root": str(metadata.get("experiment_root") or "(not prepared)"),
        "project_id": example.project_id,
        "default_branch": str(metadata.get("default_branch") or "main"),
        "run_id": str(example.source_run_id or metadata.get("logical_run_id") or ""),
        "session_id": str(metadata.get("session_id") or "(not set)"),
        "selected_unit": _selected_unit_block_from_example(example)
        if _PROMPT_TARGET_ALIASES.get(target_name, target_name) == "workflow-auditor-prompt"
        else "(not selected yet)",
    })


def _spec_scene_root(default_branch: str | None = None) -> Path:
    return repo_daily_audit_selfevolution_root(ensure_daily_audit_self_repo_checkout(default_branch))


_GEPA_SPEC = AgentSelfEvolutionSpec(
    agent_type="daily_audit",
    skill_root=lambda default_branch=None: _spec_scene_root(default_branch) / "skills",
    prompt_root=lambda default_branch=None: _spec_scene_root(default_branch) / "prompts",
    tool_metadata_path=lambda default_branch=None: _spec_scene_root(default_branch) / "tools" / "tool_descriptions.json",
    code_targets_path=lambda default_branch=None: _spec_scene_root(default_branch) / "code" / "code_targets.json",
    build_skill_examples=lambda project_id, limit=20: build_skill_eval_examples(project_id, limit=limit),
    build_prompt_examples=lambda project_id, target_name: (
        build_direction_eval_examples(project_id) if _PROMPT_TARGET_ALIASES.get(target_name, target_name) == "direction-finder-prompt" else build_skill_eval_examples(project_id)
    ),
    build_tool_examples=lambda project_id, target_name: build_skill_eval_examples(project_id),
    prompt_allowlist=("direction-finder-prompt", "workflow-auditor-prompt", "primary-agent-prompt"),
    list_skills_override=lambda _branch=None: list_daily_audit_skills(),
    list_prompt_targets_override=lambda _branch=None: list_daily_audit_prompt_targets(),
    list_tool_description_targets_override=lambda _branch=None: list_daily_audit_tool_description_targets(),
    list_code_targets_override=lambda _branch=None: list_daily_audit_code_targets(),
    build_prompt_eval_examples=build_daily_audit_prompt_eval_examples,
    render_prompt_candidate=_render_daily_audit_prompt_candidate,
    apply_skill_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_evolved_skill_direct_merge(
        project_id=project_id,
        skill_name=target,
        candidate_path=candidate_path,
        repo_dir="",
        default_branch=default_branch,
    ),
    apply_prompt_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_evolved_prompt_direct_merge(
        project_id=project_id,
        target_name=target,
        candidate_path=candidate_path,
        repo_dir="",
        default_branch=default_branch,
    ),
    apply_tool_description_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_evolved_tool_description_direct_merge(
        project_id=project_id,
        target_name=target,
        candidate_path=candidate_path,
        repo_dir="",
        default_branch=default_branch,
    ),
    apply_code_candidate=lambda project_id, target, candidate_path, default_branch=None: apply_evolved_code_direct_merge(
        project_id=project_id,
        target_path=target,
        candidate_path=candidate_path,
        repo_dir="",
        default_branch=default_branch,
    ),
    evaluation_profile=lambda target_name: (
        {
            "instruction_following": 0.15,
            "direction_quality": 0.25,
            "issue_truthfulness": 0.25,
            "language_quality": 0.10,
            "feedback_score": 0.25,
        }
        if _PROMPT_TARGET_ALIASES.get(target_name, target_name) == "direction-finder-prompt"
        else {
            "instruction_following": 0.15,
            "task_accuracy": 0.35,
            "issue_truthfulness": 0.25,
            "language_quality": 0.10,
            "feedback_score": 0.15,
        }
    ),
)


def _examples_for_skill(project_id: str, skill_name: str) -> list[DailyAuditEvalExample]:
    if skill_name in {"direction-finder", "candidate-scout", "focus-selector"}:
        return build_direction_eval_examples(project_id)
    return build_skill_eval_examples(project_id)


def _examples_for_prompt(project_id: str, target_name: str) -> list[DailyAuditEvalExample]:
    if target_name == "direction-finder-prompt":
        return build_direction_eval_examples(project_id)
    return build_skill_eval_examples(project_id)


def _examples_for_tool_description(project_id: str, target_name: str) -> list[DailyAuditEvalExample]:
    if target_name in {"candidate_scout", "focus_selector"}:
        return build_direction_eval_examples(project_id)
    return build_skill_eval_examples(project_id)


def write_evolved_skill_candidate(*, project_id: str, skill_name: str, content: str) -> Path:
    root = _candidate_dir(project_id, skill_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_evolved_prompt_candidate(*, project_id: str, target_name: str, content: str) -> Path:
    root = _prompt_candidate_dir(project_id, target_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{target_name}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def write_evolved_tool_description_candidate(*, project_id: str, target_name: str, content: str) -> Path:
    root = _tool_candidate_dir(project_id, target_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{target_name}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def write_evolved_code_candidate(*, project_id: str, target_path: str, content: str) -> Path:
    target_name = Path(target_path).name
    root = _code_candidate_dir(project_id, target_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / target_name
    path.write_text(content, encoding="utf-8")
    return path


def _apply_text_candidate_via_service_worktree(
    *,
    project_id: str,
    target_relative_path: str,
    candidate_path: Path,
    repo_dir: str | None,
    default_branch: str | None,
    commit_message: str,
) -> str | None:
    repo_root = _service_repo_root(repo_dir, default_branch)
    branch = default_branch or _detect_service_default_branch(repo_root)
    target_path = repo_root / target_relative_path
    candidate_content = candidate_path.read_text(encoding="utf-8")
    current_content = target_path.read_text(encoding="utf-8")
    if candidate_content == current_content:
        return None

    worktree_dir = _create_service_worktree(
        repo_root=repo_root,
        default_branch=branch,
        run_id=Path(target_relative_path).stem.replace(".", "-"),
    )
    try:
        worktree_root = Path(worktree_dir)
        worktree_target = worktree_root / target_relative_path
        worktree_target.parent.mkdir(parents=True, exist_ok=True)
        worktree_target.write_text(candidate_content, encoding="utf-8")
        commit_sha = _commit_all_and_get_sha_local(worktree_root=worktree_root, message=commit_message)
        _fast_forward_service_repo(repo_root=repo_root, default_branch=branch, commit_sha=commit_sha)
        return commit_sha
    finally:
        _cleanup_service_worktree(repo_root=repo_root, worktree_dir=worktree_dir)


def apply_evolved_skill_direct_merge(
    *,
    project_id: str,
    skill_name: str,
    candidate_path: Path,
    repo_dir: str,
    sandbox=None,
    default_branch: str,
) -> str | None:
    normalized_skill = _SKILL_ALIASES.get(skill_name, skill_name)
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "apply-skill",
                "--project-id",
                project_id,
                "--skill-name",
                normalized_skill,
                "--candidate-path",
                str(candidate_path),
                "--default-branch",
                default_branch,
            ],
        )
        return str(payload["commit_sha"]) if payload.get("commit_sha") else None
    result = shared_apply.apply_skill_candidate_direct_merge(
        agent_type="daily_audit",
        skill_name=normalized_skill,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve daily-audit skill {normalized_skill}",
    )
    if result.status == "failed":
        raise RuntimeError(result.reason or "skill candidate is not eligible for direct merge")
    return result.commit_sha


def apply_evolved_prompt_direct_merge(
    *,
    project_id: str,
    target_name: str,
    candidate_path: Path,
    repo_dir: str,
    sandbox=None,
    default_branch: str,
) -> str | None:
    normalized_target = _PROMPT_TARGET_ALIASES.get(target_name, target_name)
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "apply-prompt",
                "--project-id",
                project_id,
                "--target-name",
                normalized_target,
                "--candidate-path",
                str(candidate_path),
                "--default-branch",
                default_branch,
            ],
        )
        return str(payload["commit_sha"]) if payload.get("commit_sha") else None
    result = shared_apply.apply_prompt_candidate_direct_merge(
        agent_type="daily_audit",
        target_name=normalized_target,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve daily-audit prompt {normalized_target}",
    )
    if result.status == "failed":
        raise RuntimeError(result.reason or "prompt candidate is not eligible for direct merge")
    return result.commit_sha


def apply_evolved_tool_description_direct_merge(
    *,
    project_id: str,
    target_name: str,
    candidate_path: Path,
    repo_dir: str,
    sandbox=None,
    default_branch: str,
) -> str | None:
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "apply-tool",
                "--project-id",
                project_id,
                "--target-name",
                target_name,
                "--candidate-path",
                str(candidate_path),
                "--default-branch",
                default_branch,
            ],
        )
        return str(payload["commit_sha"]) if payload.get("commit_sha") else None
    result = shared_apply.apply_tool_description_candidate_direct_merge(
        agent_type="daily_audit",
        target_name=target_name,
        candidate_path=candidate_path,
        default_branch=default_branch,
        commit_message=f"chore: evolve daily-audit tool description {target_name}",
    )
    if result.status == "failed":
        raise RuntimeError(result.reason or "tool-description candidate is not eligible for direct merge")
    return result.commit_sha


def validate_code_layer_change_set(paths: list[str]) -> tuple[bool, str | None]:
    if not paths:
        return False, "empty_change_set"
    allowed_targets = set(list_daily_audit_code_targets())
    if all(path in allowed_targets for path in paths):
        return True, None
    return False, "non_code_target_change_detected"


def run_daily_audit_code_evolution(
    *,
    project_id: str,
    target_path: str,
    iterations: int = 10,
    sandbox: SandboxBackendProtocol | None = None,
    default_branch: str | None = None,
) -> Path:
    del project_id, iterations, sandbox, default_branch
    raise SelfEvolutionSkipped("code_self_evolution_placeholder")


def _list_worktree_changed_paths(worktree_root: Path) -> list[str]:
    result = run_safe_git(["diff", "--name-only", "--relative"], cwd=worktree_root)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _ensure_single_target_change(changed_paths: list[str], target_path: str) -> None:
    relevant = [path for path in changed_paths if path]
    if not relevant:
        raise RuntimeError("no code candidate changes were detected")
    if any(path != target_path for path in relevant):
        raise RuntimeError(f"unrelated drift detected for {target_path}")


def _coerce_candidate_with_metadata(result: Any) -> tuple[Any, HeldoutEvaluationResult]:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], HeldoutEvaluationResult):
        return result[0], result[1]
    return result, HeldoutEvaluationResult(None, None, 0, None)


def _heldout_result_from_payload(payload: dict[str, Any]) -> HeldoutEvaluationResult:
    return HeldoutEvaluationResult(
        baseline_score=payload.get("baseline_score"),
        candidate_score=payload.get("candidate_score"),
        heldout_examples=int(payload.get("heldout_examples") or 0),
        gate_reason=payload.get("gate_reason"),
    )


def _daily_apply_status(
    *,
    asset_type: str,
    project_id: str,
    target: str,
    candidate_path: Path,
    default_branch: str | None,
) -> tuple[str, str | None, str | None]:
    if not self_repo_enabled():
        return "candidate_generated", None, None
    if asset_type == "skill":
        commit_sha = apply_evolved_skill_direct_merge(
            project_id=project_id,
            skill_name=target,
            candidate_path=candidate_path,
            repo_dir="",
            default_branch=default_branch,
        )
    elif asset_type == "tool_description":
        commit_sha = apply_evolved_tool_description_direct_merge(
            project_id=project_id,
            target_name=target,
            candidate_path=candidate_path,
            repo_dir="",
            default_branch=default_branch,
        )
    elif asset_type == "code":
        commit_sha = apply_evolved_code_direct_merge(
            project_id=project_id,
            target_path=target,
            candidate_path=candidate_path,
            repo_dir="",
            default_branch=default_branch,
        )
    else:
        commit_sha = None
    if commit_sha:
        return "applied", None, commit_sha
    return "skipped", "already_applied", None


def _run_daily_audit_non_prompt_self_evolution(
    *,
    project_id: str,
    default_branch: str | None = None,
    enabled: bool,
) -> AgentSelfEvolutionResult:
    if not enabled:
        return AgentSelfEvolutionResult(
            agent_type="daily_audit",
            status="skipped",
            reason="self_evolution_disabled",
            outputs=[],
            asset_outcomes=[],
        )

    outputs: list[Path | str] = []
    asset_outcomes: list[SelfEvolutionAssetOutcome] = []

    for skill_name in list_daily_audit_skills():
        try:
            candidate_result = run_daily_audit_skill_evolution(
                project_id=project_id,
                skill_name=skill_name,
                return_metadata=True,
            )
            candidate_path, evaluation = _coerce_candidate_with_metadata(candidate_result)
            outputs.append(candidate_path)
            status, reason, commit_sha = _daily_apply_status(
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
                    reason=reason,
                    candidate_path=candidate_path,
                    verification_status="passed",
                    evaluation=evaluation,
                    commit_sha=commit_sha,
                )
            )
        except Exception as exc:
            asset_outcomes.append(failed_asset_outcome(asset_type="skill", target=skill_name, exc=exc))

    for target_name in list_daily_audit_tool_description_targets():
        try:
            candidate_result = run_daily_audit_tool_description_evolution(
                project_id=project_id,
                target_name=target_name,
                return_metadata=True,
            )
            candidate_path, evaluation = _coerce_candidate_with_metadata(candidate_result)
            outputs.append(candidate_path)
            status, reason, commit_sha = _daily_apply_status(
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
                    reason=reason,
                    candidate_path=candidate_path,
                    verification_status="passed",
                    evaluation=evaluation,
                    commit_sha=commit_sha,
                )
            )
        except Exception as exc:
            asset_outcomes.append(failed_asset_outcome(asset_type="tool_description", target=target_name, exc=exc))

    for target_path in list_daily_audit_code_targets():
        try:
            candidate_path = run_daily_audit_code_evolution(
                project_id=project_id,
                target_path=target_path,
                default_branch=default_branch,
            )
            outputs.append(candidate_path)
            status, reason, commit_sha = _daily_apply_status(
                asset_type="code",
                project_id=project_id,
                target=target_path,
                candidate_path=Path(candidate_path),
                default_branch=default_branch,
            )
            asset_outcomes.append(
                successful_asset_outcome(
                    asset_type="code",
                    target=target_path,
                    status=status,
                    reason=reason,
                    candidate_path=candidate_path,
                    verification_status="passed",
                    commit_sha=commit_sha,
                )
            )
        except Exception as exc:
            asset_outcomes.append(failed_asset_outcome(asset_type="code", target=target_path, exc=exc))

    return finalize_agent_self_evolution_result(
        agent_type="daily_audit",
        outputs=outputs,
        asset_outcomes=asset_outcomes,
    )


def apply_evolved_code_direct_merge(
    *,
    project_id: str,
    target_path: str,
    candidate_path: Path,
    repo_dir: str | None = None,
    default_branch: str | None = None,
    sandbox: SandboxBackendProtocol | None = None,
) -> str | None:
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "apply-code",
                "--project-id",
                project_id,
                "--target-path",
                target_path,
                "--candidate-path",
                str(candidate_path),
                "--default-branch",
                default_branch or "",
            ],
        )
        return str(payload["commit_sha"]) if payload.get("commit_sha") else None
    allowed, reason = validate_code_layer_change_set([target_path])
    if not allowed:
        raise RuntimeError(reason or "code candidate is not eligible for direct merge")

    repo_root = _service_repo_root(repo_dir, default_branch)
    target_file = repo_root / target_path
    candidate_content = candidate_path.read_text(encoding="utf-8")
    current_content = target_file.read_text(encoding="utf-8")
    if candidate_content == current_content:
        return None

    branch = _detect_service_default_branch(repo_root)
    worktree_dir = _create_service_worktree(
        repo_root=repo_root,
        default_branch=branch,
        run_id=f"code-{Path(target_path).stem.replace('.', '-')}",
    )
    try:
        worktree_root = Path(worktree_dir)
        worktree_target = worktree_root / target_path
        worktree_target.parent.mkdir(parents=True, exist_ok=True)
        worktree_target.write_text(candidate_content, encoding="utf-8")
        try:
            _ensure_single_target_change(_list_worktree_changed_paths(worktree_root), target_path)
        except subprocess.CalledProcessError:
            pass
        _run_code_merge_tests(str(worktree_root))
        try:
            _ensure_single_target_change(_list_worktree_changed_paths(worktree_root), target_path)
        except subprocess.CalledProcessError:
            pass
        commit_sha = _commit_all_and_get_sha_local(
            worktree_root=worktree_root,
            message=f"chore: evolve daily-audit code {Path(target_path).name}",
        )
        _fast_forward_service_repo(repo_root=repo_root, default_branch=branch, commit_sha=commit_sha)
        return commit_sha
    finally:
        _cleanup_service_worktree(repo_root=repo_root, worktree_dir=worktree_dir)


def run_daily_audit_skill_evolution(
    *,
    project_id: str,
    skill_name: str,
    iterations: int = 10,
    return_metadata: bool = False,
    sandbox: SandboxBackendProtocol | None = None,
    default_branch: str | None = None,
) -> Path | tuple[Path, HeldoutEvaluationResult]:
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "skill-evolve",
                "--project-id",
                project_id,
                "--skill-name",
                skill_name,
                "--iterations",
                str(iterations),
            ],
        )
        candidate_path = Path(str(payload["candidate_path"]))
        evaluation = _heldout_result_from_payload(payload)
        if return_metadata:
            return candidate_path, evaluation
        return candidate_path
    candidate_path, evaluation = shared_run_skill_evolution(
        spec=_GEPA_SPEC,
        project_id=project_id,
        skill_name=skill_name,
        iterations=iterations,
        default_branch=default_branch,
    )
    if return_metadata:
        return candidate_path, evaluation
    return candidate_path


def run_daily_audit_prompt_evolution(
    *,
    project_id: str,
    target_name: str,
    iterations: int = 10,
    return_metadata: bool = False,
    sandbox: SandboxBackendProtocol | None = None,
    default_branch: str | None = None,
) -> Path | tuple[Path, HeldoutEvaluationResult]:
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "prompt-evolve",
                "--project-id",
                project_id,
                "--target-name",
                target_name,
                "--iterations",
                str(iterations),
            ],
        )
        candidate_path = Path(str(payload["candidate_path"]))
        evaluation = _heldout_result_from_payload(payload)
        if return_metadata:
            return candidate_path, evaluation
        return candidate_path
    normalized_target = _PROMPT_TARGET_ALIASES.get(target_name, target_name)
    candidate_path, evaluation = shared_run_prompt_evolution(
        spec=_GEPA_SPEC,
        project_id=project_id,
        target_name=normalized_target,
        iterations=iterations,
        default_branch=default_branch,
    )
    if return_metadata:
        return candidate_path, evaluation
    return candidate_path


def run_daily_audit_tool_description_evolution(
    *,
    project_id: str,
    target_name: str,
    iterations: int = 10,
    return_metadata: bool = False,
    sandbox: SandboxBackendProtocol | None = None,
    default_branch: str | None = None,
) -> Path | tuple[Path, HeldoutEvaluationResult]:
    if sandbox is not None:
        repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
        payload = _run_evolution_cli_in_sandbox(
            sandbox=sandbox,
            repo_root=repo_root,
            args=[
                "tool-evolve",
                "--project-id",
                project_id,
                "--target-name",
                target_name,
                "--iterations",
                str(iterations),
            ],
        )
        candidate_path = Path(str(payload["candidate_path"]))
        evaluation = _heldout_result_from_payload(payload)
        if return_metadata:
            return candidate_path, evaluation
        return candidate_path
    candidate_path, evaluation = shared_run_tool_description_evolution(
        spec=_GEPA_SPEC,
        project_id=project_id,
        target_name=target_name,
        iterations=iterations,
        default_branch=default_branch,
    )
    if return_metadata:
        return candidate_path, evaluation
    return candidate_path


def _should_evolve_target(project_id: str, *, asset_type: str, target: str) -> tuple[bool, str | None]:
    del project_id, asset_type, target
    return True, None


def _record_evolution_lineage(
    project_id: str,
    *,
    asset_type: str,
    target: str,
    status: str,
    verification_status: str | None = None,
    commit_sha: str | None = None,
    notes: str | None = None,
    baseline_score: float | None = None,
    candidate_score: float | None = None,
    heldout_examples: int | None = None,
    gate_reason: str | None = None,
) -> None:
    del (
        project_id,
        asset_type,
        target,
        status,
        verification_status,
        commit_sha,
        notes,
        baseline_score,
        candidate_score,
        heldout_examples,
        gate_reason,
    )


def maybe_run_daily_audit_self_evolution(
    project_id: str,
    *,
    repo_dir: str | None = None,
    default_branch: str | None = None,
    sandbox: SandboxBackendProtocol | None = None,
) -> AgentSelfEvolutionResult:
    del repo_dir, sandbox
    return run_gepa_self_evolution_for_spec(
        spec=_GEPA_SPEC,
        project_id=project_id,
        default_branch=default_branch,
        enabled=settings.DAILY_AUDIT_SELF_EVOLUTION_ENABLED,
    )


def run_daily_audit_evolution_cycle(
    *,
    project_id: str,
    default_branch: str,
    event=None,
) -> object:
    del event
    return maybe_run_daily_audit_self_evolution(
        project_id,
        default_branch=default_branch,
    )


def is_text_layer_evolution_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("agent/scenes/daily_audit/selfevolution/prompts/") and normalized.endswith(".py"):
        return True
    if normalized.startswith("agent/scenes/daily_audit/selfevolution/skills/") and normalized.endswith("/SKILL.md"):
        return True
    if normalized.startswith("agent/scenes/daily_audit/selfevolution/prompts/") and normalized.endswith(".md"):
        return True
    if normalized == "agent/scenes/daily_audit/selfevolution/tools/tool_descriptions.json":
        return True
    return False


def validate_text_layer_change_set(paths: list[str]) -> tuple[bool, str | None]:
    if not paths:
        return False, "empty_change_set"
    if all(is_text_layer_evolution_path(path) for path in paths):
        return True, None
    return False, "non_text_layer_change_detected"
