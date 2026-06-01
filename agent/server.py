"""LangGraph compatibility entry point for the auto-review agent."""

from __future__ import annotations

import logging

from langgraph.types import RunnableConfig

from agent.gitlab.mr_info import get_mr_metadata
from agent.sandbox.manager import setup_sandbox
from agent.scenes.auto_review.graph import build_auto_review_agent
from agent.utils.thread_id import generate_thread_id

logger = logging.getLogger(__name__)


async def get_agent(config: RunnableConfig):
    """Create the auto-review agent for a given MR.

    config["configurable"] must contain:
    - project_id: GitLab project path (e.g. 'group/project')
    - mr_iid: merge request internal ID
    - thread_id: optional deterministic ID for sandbox reuse
    """
    ctx = config.get("configurable", {})
    project_id = ctx.get("project_id", "")
    mr_iid = ctx.get("mr_iid")
    thread_id = ctx.get("thread_id")
    model_id = ctx.get("model_id")

    if not project_id or mr_iid is None:
        raise ValueError("configurable.project_id and configurable.mr_iid are required")

    if not thread_id:
        thread_id = generate_thread_id(project_id, mr_iid)

    logger.info("get_agent: project=%s mr=!%s thread=%s", project_id, mr_iid, thread_id)

    meta = get_mr_metadata(project_id, mr_iid)
    sandbox, repo_dir = await setup_sandbox(thread_id, project_id, meta.source_branch)

    return build_auto_review_agent(
        sandbox=sandbox,
        repo_dir=repo_dir,
        source_branch=meta.source_branch,
        target_branch=meta.target_branch,
        model_id=model_id,
    )
