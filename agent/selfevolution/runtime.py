"""Shared runtime dispatcher for agent-scoped self-evolution."""

from __future__ import annotations

from types import SimpleNamespace


def run_agent_self_evolution_cycle(
    *,
    agent_type: str,
    project_id: str,
    default_branch: str,
    event=None,
) -> object:
    normalized = str(agent_type or "").strip()
    if normalized == "daily_audit":
        from agent.scenes.daily_audit.selfevolution.engine import run_daily_audit_evolution_cycle

        return run_daily_audit_evolution_cycle(
            project_id=project_id,
            default_branch=default_branch,
            event=event,
        )
    if normalized == "mention":
        from agent.scenes.mention.selfevolution.engine import run_mention_evolution_cycle

        return run_mention_evolution_cycle(
            project_id=project_id,
            default_branch=default_branch,
            event=event,
        )
    if normalized == "auto_review":
        from agent.scenes.auto_review.selfevolution.engine import run_auto_review_evolution_cycle

        return run_auto_review_evolution_cycle(
            project_id=project_id,
            default_branch=default_branch,
            event=event,
        )
    return SimpleNamespace(status="failed", reason=f"unknown_agent_type:{normalized}", output_count=0)

