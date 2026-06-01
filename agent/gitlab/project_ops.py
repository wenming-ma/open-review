"""Project-level GitLab helpers for proactive workflows."""

from __future__ import annotations

from agent.gitlab.client import get_project


def get_project_default_branch(project_id: str | int) -> str:
    """Return the GitLab project's default branch."""
    project = get_project(project_id)
    branch = str(getattr(project, "default_branch", "") or "").strip()
    if not branch:
        raise RuntimeError(f"Project {project_id} does not expose a default branch")
    return branch


def create_project_issue(
    project_id: str | int,
    *,
    title: str,
    description: str,
) -> int | None:
    """Create a project issue and return the GitLab issue IID."""
    project = get_project(project_id)
    created = project.issues.create({"title": title, "description": description})
    return getattr(created, "iid", None)


def create_project_merge_request(
    project_id: str | int,
    *,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    draft: bool = False,
):
    """Create a project merge request and return the GitLab MR object."""
    project = get_project(project_id)
    mr_title = f"Draft: {title}" if draft and not title.startswith("Draft:") else title
    return project.mergerequests.create(
        {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": mr_title,
            "description": description,
            "remove_source_branch": False,
        }
    )
