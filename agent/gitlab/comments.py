"""Post comments, inline comments, and reactions on GitLab MRs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from agent.gitlab.client import get_mr

logger = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 65000
_MARKER_RE = re.compile(r"<!--\s*([a-z0-9-]+):\s*([^\s>]+)\s*-->")


@dataclass
class MRCommentRecord:
    note_id: int | None
    discussion_id: str | None
    author: str
    body: str
    created_at: str
    file_path: str | None
    line: int | None
    is_system: bool
    kind: str


def _truncate(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 100] + "\n\n... (truncated)"


def _extract_markers(body: str) -> dict[str, str]:
    return {name: value for name, value in _MARKER_RE.findall(body or "")}


def post_mr_comment(project_id: str | int, mr_iid: int, body: str) -> int | None:
    """Post a regular comment on a merge request. Returns the note ID."""
    mr = get_mr(project_id, mr_iid)
    body = _truncate(body)
    note = mr.notes.create({"body": body})
    logger.info("Posted comment %s on %s!%s", note.id, project_id, mr_iid)
    return note.id


def upsert_mr_comment_by_marker(
    project_id: str | int,
    mr_iid: int,
    body: str,
    *,
    marker_name: str,
    marker_value: str,
) -> int | None:
    """Create or update a top-level MR note identified by a hidden marker."""
    mr = get_mr(project_id, mr_iid)
    truncated = _truncate(body)
    for note in mr.notes.list(get_all=True):
        note_data = _to_note_dict(note)
        markers = _extract_markers(note_data.get("body", ""))
        if markers.get(marker_name) != marker_value:
            continue
        setattr(note, "body", truncated)
        note.save()
        logger.info(
            "Updated comment %s on %s!%s marker=%s:%s",
            getattr(note, "id", None),
            project_id,
            mr_iid,
            marker_name,
            marker_value,
        )
        return getattr(note, "id", None)

    note = mr.notes.create({"body": truncated})
    logger.info(
        "Posted comment %s on %s!%s marker=%s:%s",
        note.id,
        project_id,
        mr_iid,
        marker_name,
        marker_value,
    )
    return note.id


def reply_to_mr_discussion(
    project_id: str | int,
    mr_iid: int,
    discussion_id: str,
    body: str,
) -> int | None:
    """Reply to an existing merge request discussion thread. Returns the note ID."""
    mr = get_mr(project_id, mr_iid)
    discussion = mr.discussions.get(discussion_id)
    note = discussion.notes.create({"body": _truncate(body)})
    logger.info(
        "Posted discussion reply %s on %s!%s discussion=%s",
        note.id,
        project_id,
        mr_iid,
        discussion_id,
    )
    return note.id


def post_diff_discussion(
    project_id: str | int,
    mr_iid: int,
    body: str,
    *,
    new_path: str,
    old_path: str,
    new_line: int | None = None,
    old_line: int | None = None,
    fallback_to_note: bool = True,
) -> int | str | None:
    """Post a merge request diff discussion using explicit old/new line positioning."""
    mr = get_mr(project_id, mr_iid)
    diff_refs = mr.diff_refs
    body = _truncate(body)

    pos = {
        "position_type": "text",
        "new_path": new_path,
        "old_path": old_path,
        "base_sha": diff_refs["base_sha"],
        "start_sha": diff_refs["start_sha"],
        "head_sha": diff_refs["head_sha"],
    }
    if new_line is not None:
        pos["new_line"] = new_line
    if old_line is not None:
        pos["old_line"] = old_line

    try:
        discussion = mr.discussions.create({"body": body, "position": pos})
        logger.info(
            "Diff discussion on %s!%s new=%s:%s old=%s:%s",
            project_id,
            mr_iid,
            new_path,
            new_line,
            old_path,
            old_line,
        )
        return getattr(discussion, "id", None) or f"{new_path}:{new_line or old_line}"
    except Exception:
        if not fallback_to_note:
            logger.warning(
                "Diff discussion failed for %s!%s new=%s:%s old=%s:%s without note fallback",
                project_id,
                mr_iid,
                new_path,
                new_line,
                old_path,
                old_line,
                exc_info=True,
            )
            return None
        logger.warning(
            "Diff discussion failed for %s!%s new=%s:%s old=%s:%s, falling back to regular note",
            project_id,
            mr_iid,
            new_path,
            new_line,
            old_path,
            old_line,
            exc_info=True,
        )
        line_ref = new_line or old_line or 0
        fallback = f"**{new_path}:{line_ref}**\n\n{body}"
        note = mr.notes.create({"body": _truncate(fallback)})
        return getattr(note, "id", None)


def post_inline_comment(
    project_id: str | int,
    mr_iid: int,
    file_path: str,
    line: int,
    body: str,
    *,
    old_path: str | None = None,
    old_line: int | None = None,
    fallback_to_note: bool = True,
) -> int | str | None:
    """Post an inline comment (discussion) on a specific line in the MR diff.

    Uses ``mr.discussions.create()`` with a position object.
    Falls back to a regular note if positioning fails.
    """
    return post_diff_discussion(
        project_id,
        mr_iid,
        body,
        new_path=file_path,
        old_path=old_path or file_path,
        new_line=None if old_line is not None else line,
        old_line=old_line,
        fallback_to_note=fallback_to_note,
    )


def add_eyes_reaction(project_id: str | int, mr_iid: int, note_id: int) -> None:
    """Add an eyes emoji reaction to a note to signal processing."""
    mr = get_mr(project_id, mr_iid)
    try:
        note = mr.notes.get(note_id)
        note.awardemojis.create({"name": "eyes"})
    except Exception:
        logger.debug("Could not add eyes reaction to note %s", note_id, exc_info=True)


def _to_note_dict(note) -> dict:
    if isinstance(note, dict):
        return note
    return getattr(note, "attributes", None) or getattr(note, "_attrs", None) or note.__dict__


def _build_record(note_data: dict, *, discussion_id: str | None, kind: str) -> MRCommentRecord:
    position = note_data.get("position") or {}
    file_path = position.get("new_path") or position.get("old_path")
    line = position.get("new_line") or position.get("old_line")
    author = note_data.get("author", {}) or {}
    return MRCommentRecord(
        note_id=note_data.get("id"),
        discussion_id=discussion_id,
        author=author.get("username", "unknown"),
        body=note_data.get("body", ""),
        created_at=note_data.get("created_at", ""),
        file_path=file_path,
        line=line,
        is_system=bool(note_data.get("system")),
        kind=kind,
    )


def list_mr_comments(project_id: str | int, mr_iid: int) -> list[MRCommentRecord]:
    """List top-level MR notes."""
    mr = get_mr(project_id, mr_iid)
    records = []
    for note in mr.notes.list(get_all=True):
        note_data = _to_note_dict(note)
        records.append(_build_record(note_data, discussion_id=None, kind="note"))
    return records


def list_mr_discussion_comments(project_id: str | int, mr_iid: int) -> list[MRCommentRecord]:
    """List notes inside MR discussions, including inline comments."""
    mr = get_mr(project_id, mr_iid)
    records = []
    for discussion in mr.discussions.list(get_all=True):
        discussion_data = getattr(discussion, "attributes", {}) or {}
        discussion_id = discussion_data.get("id") or getattr(discussion, "id", None)
        notes = discussion_data.get("notes")
        if notes is None:
            notes = getattr(discussion, "notes", [])
        for note in notes:
            note_data = _to_note_dict(note)
            records.append(
                _build_record(note_data, discussion_id=discussion_id, kind="discussion")
            )
    return records


def list_mr_activity(project_id: str | int, mr_iid: int) -> list[MRCommentRecord]:
    """Return all MR notes and discussion notes in chronological order."""
    comments = list_mr_comments(project_id, mr_iid)
    discussions = list_mr_discussion_comments(project_id, mr_iid)
    items = comments + discussions
    items.sort(key=lambda item: (item.created_at or "", item.note_id or 0))
    return items
