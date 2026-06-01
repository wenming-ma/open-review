"""Fetch MR metadata, diffs, and file contents from GitLab."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agent.config import settings
from agent.gitlab.client import get_mr, get_project
from agent.utils.gitlab_project_targets import build_gitlab_merge_request_url

logger = logging.getLogger(__name__)


@dataclass
class MRMetadata:
    project_id: str
    mr_iid: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    author: str
    url: str
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass
class FileChange:
    new_path: str
    old_path: str
    diff: str
    new_file: bool
    deleted_file: bool
    renamed_file: bool


def get_mr_metadata(project_id: str, mr_iid: int) -> MRMetadata:
    """Fetch core MR metadata and diff refs."""
    mr = get_mr(project_id, mr_iid)
    diff_refs = mr.diff_refs
    configured_url = build_gitlab_merge_request_url(
        project_id,
        mr_iid,
        external_url=settings.GITLAB_EXTERNAL_URL,
    )
    return MRMetadata(
        project_id=project_id,
        mr_iid=mr_iid,
        title=mr.title,
        description=mr.description or "",
        source_branch=mr.source_branch,
        target_branch=mr.target_branch,
        author=mr.author.get("username", "unknown"),
        url=configured_url or mr.web_url,
        base_sha=diff_refs["base_sha"],
        start_sha=diff_refs["start_sha"],
        head_sha=diff_refs["head_sha"],
    )


def get_mr_changes(project_id: str, mr_iid: int) -> list[FileChange]:
    """Fetch the list of changed files with diffs."""
    mr = get_mr(project_id, mr_iid)
    changes = mr.changes()["changes"]
    return [
        FileChange(
            new_path=c["new_path"],
            old_path=c["old_path"],
            diff=c["diff"],
            new_file=c["new_file"],
            deleted_file=c["deleted_file"],
            renamed_file=c["renamed_file"],
        )
        for c in changes
    ]


def get_file_content(project_id: str, file_path: str, ref: str) -> str | None:
    """Get file content at a specific ref (branch/sha)."""
    project = get_project(project_id)
    try:
        f = project.files.get(file_path=file_path, ref=ref)
        return f.decode().decode("utf-8", errors="replace")
    except Exception:
        logger.debug("Could not fetch %s at ref %s", file_path, ref)
        return None


def can_push_to_branch(project_id: str, branch_name: str) -> bool:
    """Return whether the authenticated GitLab user can push to a branch."""
    project = get_project(project_id)
    try:
        branch = project.branches.get(branch_name)
    except Exception:
        logger.warning("Could not fetch branch %s for project %s", branch_name, project_id, exc_info=True)
        return False

    if hasattr(branch, "can_push"):
        return bool(branch.can_push)

    branch_data = getattr(branch, "attributes", None) or getattr(branch, "_attrs", None) or {}
    return bool(branch_data.get("can_push"))
