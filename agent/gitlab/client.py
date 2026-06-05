"""GitLab API connection factory."""

from __future__ import annotations

import gitlab
import requests
from ipaddress import ip_address
from urllib.parse import urlparse

from agent.config import settings


def _should_bypass_proxy(url: str) -> bool:
    """Return true for local/private GitLab URLs that should not use host proxies."""
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "gitlab", "gitlab.local", "host.docker.internal"}:
        return True
    if "." not in host:
        return True
    if host.endswith(".local"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def get_gitlab_client() -> gitlab.Gitlab:
    """Create an authenticated GitLab client."""
    session = None
    if _should_bypass_proxy(settings.GITLAB_API_URL):
        session = requests.Session()
        session.trust_env = False

    return gitlab.Gitlab(
        url=settings.GITLAB_API_URL,
        private_token=settings.GITLAB_TOKEN,
        ssl_verify=settings.GITLAB_SSL_VERIFY,
        keep_base_url=True,
        session=session,
    )


def get_project(project_id: str | int):
    """Get a GitLab project by ID or path (e.g. 'group/project')."""
    gl = get_gitlab_client()
    return gl.projects.get(project_id)


def get_mr(project_id: str | int, mr_iid: int):
    """Get a merge request by project and MR IID."""
    project = get_project(project_id)
    return project.mergerequests.get(mr_iid)
