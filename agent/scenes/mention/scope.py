"""Authoritative MR scope helpers for mention workflow."""

from __future__ import annotations

from typing import Any

from agent.scenes.auto_review.scope import changed_file_status
from agent.scenes.mention.models import MentionContext, MRSnapshot


def _snapshot(context: MentionContext | MRSnapshot) -> MRSnapshot:
    return context.mr_snapshot if isinstance(context, MentionContext) else context


def authoritative_scope_summary(context: MentionContext | MRSnapshot) -> str:
    """Render the frozen mention MR scope summary."""
    snapshot = _snapshot(context)
    lines = [
        "权威 MR scope 快照",
        "- 来源：orchestrator 冻结快照（若后续描述冲突，以此为准）",
        f"- base_sha: `{snapshot.base_sha}`",
        f"- start_sha: `{snapshot.start_sha}`",
        f"- head_sha: `{snapshot.head_sha}`",
        f"- diff_range: `{snapshot.diff_range}`",
        f"- commit_range: `{snapshot.commit_range}`",
        "",
        "变更文件状态：",
    ]
    if not snapshot.changed_files:
        lines.append("- none")
        return "\n".join(lines)

    for item in snapshot.changed_files[:20]:
        status = changed_file_status(item)
        if status == "renamed" and item.old_path and item.old_path != item.file_path:
            lines.append(f"- {item.old_path} -> {item.file_path} ({status})")
        else:
            lines.append(f"- {item.file_path} ({status})")
    return "\n".join(lines)


def review_scope_snapshot(context: MentionContext | MRSnapshot, *, file_path: str | None = None) -> dict[str, Any]:
    """Return the orchestrator-frozen mention MR scope snapshot."""
    snapshot = _snapshot(context)
    result: dict[str, Any] = {
        "scope_source": "orchestrator_frozen_snapshot",
        "project_id": snapshot.project_id,
        "mr_iid": snapshot.mr_iid,
        "base_sha": snapshot.base_sha,
        "start_sha": snapshot.start_sha,
        "head_sha": snapshot.head_sha,
        "diff_range": snapshot.diff_range,
        "commit_range": snapshot.commit_range,
    }

    if file_path:
        normalized = file_path.strip()
        for item in snapshot.changed_files:
            if item.file_path == normalized:
                result.update(
                    {
                        "path": item.file_path,
                        "old_path": item.old_path,
                        "status": changed_file_status(item),
                        "diff": item.diff,
                        "added_lines": list(item.added_lines),
                    }
                )
                return result

        result.update(
            {
                "error": "file_not_found",
                "available_files": [item.file_path for item in snapshot.changed_files],
            }
        )
        return result

    result["changed_files"] = [
        {"path": item.file_path, "status": changed_file_status(item)}
        for item in snapshot.changed_files
    ]
    return result
