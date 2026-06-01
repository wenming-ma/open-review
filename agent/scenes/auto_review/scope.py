"""Authoritative auto-review MR scope helpers."""

from __future__ import annotations

from typing import Any

from agent.scenes.auto_review.models import ChangedFileContext, ReviewContext

_UNAVAILABLE_SCOPE_SUMMARY = "权威 MR scope 快照\n- unavailable"


def changed_file_status(item: ChangedFileContext) -> str:
    """Return the authoritative status label for a changed file."""
    if item.deleted_file:
        return "deleted"
    if item.renamed_file:
        return "renamed"
    if item.new_file:
        return "new"
    return "modified"


def authoritative_scope_summary(context: ReviewContext | None) -> str:
    """Render the frozen MR scope summary for prompts and orchestrator messages."""
    if context is None:
        return _UNAVAILABLE_SCOPE_SUMMARY

    lines = [
        "权威 MR scope 快照",
        "- 来源：orchestrator 冻结快照（若后续描述冲突，以此为准）",
        f"- base_sha: `{context.base_sha}`",
        f"- start_sha: `{context.start_sha}`",
        f"- head_sha: `{context.head_sha}`",
        f"- diff_range: `{context.diff_range}`",
        f"- commit_range: `{context.commit_range}`",
        f"- changed_files_count: `{len(context.changed_files)}`",
        "",
        "变更文件状态：",
    ]

    if not context.changed_files:
        lines.append("- none")
        return "\n".join(lines)

    for item in context.changed_files[:40]:
        status = changed_file_status(item)
        if status == "renamed" and item.old_path and item.old_path != item.file_path:
            lines.append(f"- {item.old_path} -> {item.file_path} ({status})")
            continue
        lines.append(f"- {item.file_path} ({status})")

    return "\n".join(lines)


def review_scope_snapshot(
    context: ReviewContext | None,
    *,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Return the orchestrator-frozen MR scope snapshot."""
    snapshot: dict[str, Any] = {
        "scope_source": "orchestrator_frozen_snapshot",
    }
    if context is None:
        snapshot["error"] = "scope_unavailable"
        return snapshot

    snapshot.update(
        {
            "project_id": context.project_id,
            "mr_iid": context.mr_iid,
            "base_sha": context.base_sha,
            "start_sha": context.start_sha,
            "head_sha": context.head_sha,
            "diff_range": context.diff_range,
            "commit_range": context.commit_range,
        }
    )

    if file_path:
        normalized = file_path.strip()
        for item in context.changed_files:
            if item.file_path == normalized:
                snapshot.update(
                    {
                        "path": item.file_path,
                        "old_path": item.old_path,
                        "status": changed_file_status(item),
                        "diff": item.diff,
                        "added_lines": list(item.added_lines),
                    }
                )
                return snapshot

        snapshot.update(
            {
                "error": "file_not_found",
                "available_files": [item.file_path for item in context.changed_files],
            }
        )
        return snapshot

    snapshot["changed_files"] = [
        {
            "path": item.file_path,
            "status": changed_file_status(item),
        }
        for item in context.changed_files
    ]
    return snapshot
