"""Metadata loaders for daily audit tool descriptions."""

from __future__ import annotations

import json

from agent.scenes.daily_audit.models import DailyAuditSubagentType
from agent.selfevolution.assets import scene_tool_metadata_path


def load_tool_descriptions(*, default_branch: str | None = None) -> dict[str, str]:
    return json.loads(scene_tool_metadata_path("daily_audit", default_branch=default_branch).read_text(encoding="utf-8"))


def describe_daily_subagent(subagent_type: DailyAuditSubagentType) -> str:
    descriptions = load_tool_descriptions()
    return descriptions[subagent_type]
