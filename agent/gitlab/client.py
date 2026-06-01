"""GitLab API connection factory."""

from __future__ import annotations

import gitlab

from agent.config import settings


def get_gitlab_client() -> gitlab.Gitlab:
    """Create an authenticated GitLab client."""
    return gitlab.Gitlab(
        url=settings.GITLAB_API_URL,
        private_token=settings.GITLAB_TOKEN,
        ssl_verify=settings.GITLAB_SSL_VERIFY,
        keep_base_url=True,
    )


def get_project(project_id: str | int):
    """Get a GitLab project by ID or path (e.g. 'group/project')."""
    gl = get_gitlab_client()
    return gl.projects.get(project_id)


def get_mr(project_id: str | int, mr_iid: int):
    """Get a merge request by project and MR IID."""
    project = get_project(project_id)
    return project.mergerequests.get(mr_iid)
