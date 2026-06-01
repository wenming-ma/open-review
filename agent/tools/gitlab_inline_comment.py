"""Tool: post an inline comment on a specific line in the MR diff."""

from __future__ import annotations

from typing import Any

from langgraph.config import get_config

from agent.gitlab.comments import post_inline_comment


def gitlab_inline_comment(file_path: str, line: int, message: str) -> dict[str, Any]:
    """Post an inline review comment on a specific line in the merge request diff.

    Use this to leave targeted feedback directly on the code that has issues.
    Falls back to a regular comment if the line is not in the diff.

    Args:
        file_path: Path to the file (e.g. 'src/service/router.py')
        line: Line number in the new version of the file
        message: Review comment text (supports markdown)
    """
    config = get_config()
    ctx = config["configurable"]
    project_id = ctx.get("project_id", "")
    mr_iid = ctx.get("mr_iid")

    if not project_id or not mr_iid:
        return {"success": False, "error": "Missing project_id or mr_iid in config"}

    success = post_inline_comment(project_id, mr_iid, file_path, line, message)
    return {"success": success, "file": file_path, "line": line}
