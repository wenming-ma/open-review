"""Prompt builders for the daily audit workflow."""

from __future__ import annotations

from typing import Literal

from agent.scenes.daily_audit.models import DailyAuditContext, DailyAuditSubagentType
from agent.scenes.daily_audit.selfevolution.tools.metadata import describe_daily_subagent
from agent.selfevolution.assets import load_scene_prompt_asset_text

_PROMPT_ALIASES = {
    "primary-agent-prompt": "workflow-auditor-prompt",
}


def load_prompt_asset_text(target_name: str, *, default_branch: str | None = None) -> str:
    target_name = _PROMPT_ALIASES.get(target_name, target_name)
    return load_scene_prompt_asset_text("daily_audit", target_name, default_branch=default_branch)


def _selected_unit_block(context: DailyAuditContext) -> str:
    unit = context.selected_unit
    if unit is None:
        return "(not selected yet)"
    location = f" @ {unit.file_path}" if unit.file_path else ""
    span = ""
    if unit.start_line is not None:
        if unit.end_line is not None and unit.end_line != unit.start_line:
            span = f":{unit.start_line}-{unit.end_line}"
        else:
            span = f":{unit.start_line}"
    detail_lines = [f"[{unit.unit_type}] {unit.label}{location}{span}"]
    if unit.entrypoint_kind:
        detail_lines.append(f"Entrypoint kind: {unit.entrypoint_kind}")
    if unit.entrypoint_symbol:
        detail_lines.append(f"Entrypoint symbol: {unit.entrypoint_symbol}")
    if unit.workflow_summary:
        detail_lines.append(f"Workflow summary: {unit.workflow_summary}")
    if unit.entry_evidence:
        detail_lines.append("Entry evidence:")
        detail_lines.extend(f"- {item}" for item in unit.entry_evidence)
    return "\n".join(detail_lines)


def build_daily_audit_agent_prompt(
    *,
    repo_dir: str,
    file_tool_repo_dir: str,
    context: DailyAuditContext,
    stage: Literal["direction", "analysis"],
) -> str:
    target_name = "direction-finder-prompt" if stage == "direction" else "workflow-auditor-prompt"
    template = load_prompt_asset_text(target_name, default_branch=context.default_branch)
    return template.format(
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        experiment_root=context.experiment_root or "(not prepared)",
        project_id=context.project_id,
        default_branch=context.default_branch,
        run_id=context.run_id,
        session_id=context.session_id or "(not set)",
        selected_unit=_selected_unit_block(context),
    )


def build_daily_audit_auxiliary_prompt(
    *,
    repo_dir: str,
    file_tool_repo_dir: str,
    context: DailyAuditContext,
    subagent_type: DailyAuditSubagentType,
) -> str:
    template = load_prompt_asset_text("auxiliary-agent-prompt", default_branch=context.default_branch)
    return template.format(
        subagent_type=subagent_type,
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        project_id=context.project_id,
        default_branch=context.default_branch,
        run_id=context.run_id,
        responsibility=describe_daily_subagent(subagent_type),
    )
