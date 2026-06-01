"""Tool: post a comment on a GitLab MR."""

from __future__ import annotations

from typing import Any

from langgraph.config import get_config

from agent.gitlab.comments import post_mr_comment


def gitlab_comment(message: str) -> dict[str, Any]:
    """Post a comment to the current GitLab merge request.

    Use this to communicate review findings, answers, or status updates.

    Args:
        message: Markdown-formatted comment body.
    """
    config = get_config()
    ctx = config["configurable"]
    project_id = ctx.get("project_id", "")
    mr_iid = ctx.get("mr_iid")

    if not project_id or not mr_iid:
        return {"success": False, "error": "Missing project_id or mr_iid in config"}

    note_id = post_mr_comment(project_id, mr_iid, message)
    return {"success": True, "note_id": note_id}
