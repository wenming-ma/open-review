"""FastAPI application — receives GitLab webhooks and dispatches to agent workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import agent.bootstrap_env as _bootstrap_env  # noqa: F401  # isort: skip
from agent.admin.router import router as admin_router
from agent.admin.router import static_mount as admin_static
from agent.config import settings
from agent.controlplane import get_config_service, get_tracking_service
from agent.gitlab.identity import get_bot_username, schedule_bot_identity_prime
from agent.observability.phoenix import configure_phoenix_tracing
from agent.runtime.models import EventEnvelope
from agent.runtime.queue import enqueue_gitlab_event
from agent.utils.gitlab_project_targets import normalize_gitlab_project_targets
from agent.utils.thread_id import generate_thread_id

logger = logging.getLogger(__name__)

@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    configure_phoenix_tracing()
    prime_task = schedule_bot_identity_prime(logger=logger, context="startup")
    try:
        yield
    finally:
        if not prime_task.done():
            prime_task.cancel()
        await asyncio.gather(prime_task, return_exceptions=True)


app = FastAPI(title="Open Review", version="0.1.0", lifespan=_app_lifespan)
app.mount("/admin/static", admin_static, name="admin-static")
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Helpers (adapted from PR-Agent gitlab_webhook.py)
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


def _is_bot_user(data: dict) -> bool:
    name = data.get("user", {}).get("username", "").lower()
    bot_username = (get_bot_username() or "").lower()
    return bool(bot_username) and name == bot_username


def _is_draft(data: dict) -> bool:
    attrs = data.get("object_attributes", {})
    if "draft" in attrs:
        return bool(attrs["draft"])
    title = attrs.get("title", "")
    return title.startswith("Draft:") or title.startswith("WIP:")


def _is_draft_ready(data: dict) -> bool:
    changes = data.get("changes", {})
    if "draft" not in changes:
        return False
    prev = changes["draft"].get("previous")
    curr = changes["draft"].get("current")
    if isinstance(prev, str):
        prev = prev.lower() == "true"
    if isinstance(curr, str):
        curr = curr.lower() == "true"
    return prev is True and curr is False


def _is_target_project(project_path: str) -> bool:
    candidate = str(project_path or "").strip()
    if not candidate:
        return False
    try:
        targets = normalize_gitlab_project_targets(
            [str(item) for item in settings.GITLAB_TARGET_PROJECTS],
            api_url=settings.GITLAB_API_URL,
            external_url=settings.GITLAB_EXTERNAL_URL,
        )
    except ValueError:
        logger.warning("Configured GITLAB_TARGET_PROJECTS contains invalid entries", exc_info=True)
        return False
    return candidate in set(targets)


def _project_agent_enabled(project_path: str, key: str) -> bool:
    try:
        return bool(get_config_service().get_project_agent_config(project_path).get(key))
    except Exception:
        logger.warning("Could not load project agent config for %s", project_path, exc_info=True)
        return False


def _extract_mr_context(data: dict) -> dict:
    attrs = data.get("object_attributes", {})
    project = data.get("project", {})
    return {
        "project_id": project.get("path_with_namespace", ""),
        "mr_iid": attrs.get("iid"),
        "source_branch": attrs.get("source_branch", ""),
        "target_branch": attrs.get("target_branch", "main"),
        "title": attrs.get("title", ""),
    }


def _extract_head_sha(data: dict) -> str | None:
    attrs = data.get("object_attributes", {})
    last_commit = attrs.get("last_commit") or data.get("merge_request", {}).get("last_commit") or {}
    return last_commit.get("id") or last_commit.get("sha")


def _clean_mention_body(comment_body: str) -> str:
    bot_username = get_bot_username()
    if not bot_username:
        return comment_body.strip()
    bot_tag_pattern = _bot_mention_pattern(bot_username)
    return bot_tag_pattern.sub("", comment_body, count=1).strip()


def _bot_mention_pattern(bot_username: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![\w.-])@{re.escape(bot_username)}(?![\w.-])",
        re.IGNORECASE,
    )


def _mention_requested(data: dict) -> bool:
    comment_body = str(data.get("object_attributes", {}).get("note", "") or "")
    bot_username = get_bot_username()
    if not bot_username:
        return False
    return _bot_mention_pattern(bot_username).search(comment_body) is not None


def _feedback_candidates(project_id: str) -> list[dict]:
    return get_tracking_service().list_runs(project_id=project_id, limit=200)


def _match_published_object(
    *,
    run: dict,
    mr_iid: int | None = None,
    issue_iid: int | None = None,
    note_id: int | None = None,
    discussion_id: str | None = None,
) -> tuple[bool, str | None]:
    if issue_iid is not None and run.get("published_issue_iid") == issue_iid:
        return True, "published_issue_iid"
    if mr_iid is not None and run.get("published_merge_request_iid") == mr_iid:
        return True, "published_merge_request_iid"
    for item in run.get("published_objects", []):
        if not isinstance(item, dict):
            continue
        if discussion_id and str(item.get("discussion_id") or "") == discussion_id:
            return True, "published_object_discussion"
        external_id = item.get("external_id")
        if note_id is not None and str(external_id or "") == str(note_id):
            return True, "published_object_note"
        if issue_iid is not None and item.get("issue_iid") == issue_iid:
            return True, "published_object_issue"
        if mr_iid is not None and item.get("merge_request_iid") == mr_iid:
            return True, "published_object_merge_request"
    return False, None


def _find_feedback_target_run(data: dict) -> tuple[dict | None, str | None]:
    project_id = str(data.get("project", {}).get("path_with_namespace", "") or "").strip()
    if not project_id:
        return None, None
    candidates = _feedback_candidates(project_id)
    attrs = data.get("object_attributes", {}) or {}
    note_id = attrs.get("id")
    discussion_id = attrs.get("discussion_id")
    mr_iid = (
        data.get("merge_request", {}).get("iid")
        or attrs.get("iid")
    )
    issue_iid = data.get("issue", {}).get("iid") or attrs.get("iid")

    for run in candidates:
        matched, method = _match_published_object(
            run=run,
            mr_iid=mr_iid if data.get("merge_request") else None,
            issue_iid=issue_iid if data.get("issue") or data.get("object_kind") == "issue" else None,
            note_id=note_id,
            discussion_id=discussion_id,
        )
        if matched:
            return run, method

    if mr_iid is not None:
        head_sha = _extract_head_sha(data)
        for run in candidates:
            if run.get("event_type") != "auto_review":
                continue
            if run.get("mr_iid") != mr_iid:
                continue
            if run.get("state") != "succeeded":
                continue
            if head_sha and run.get("head_sha") == head_sha:
                return run, "latest_auto_review_same_head_sha"
    return None, None


def _feedback_payload(data: dict, *, association_method: str) -> dict:
    attrs = data.get("object_attributes", {}) or {}
    merge_request = data.get("merge_request", {}) or {}
    issue = data.get("issue", {}) or {}
    object_kind = str(data.get("object_kind") or "")
    feedback_kind = object_kind
    if object_kind == "note":
        feedback_kind = "issue_note" if issue else "mr_note"
    return {
        "feedback_kind": feedback_kind,
        "association_method": association_method,
        "payload_json": data,
        "author": str(data.get("user", {}).get("username", "unknown") or "unknown"),
        "mr_iid": merge_request.get("iid") or attrs.get("iid"),
        "issue_iid": issue.get("iid") or attrs.get("iid"),
        "note_id": attrs.get("id"),
        "discussion_id": attrs.get("discussion_id"),
        "emoji_name": attrs.get("name") or attrs.get("award_name"),
        "created_at": attrs.get("created_at") or data.get("created_at") or "",
    }


def _ingest_feedback_event(data: dict) -> dict | None:
    run, method = _find_feedback_target_run(data)
    if run is None or method is None:
        return None
    payload = _feedback_payload(data, association_method=method)
    get_tracking_service().append_feedback_event(run["run_id"], payload)
    return {"run_id": run["run_id"], "association_method": method}


# ---------------------------------------------------------------------------
# Background task handlers
# ---------------------------------------------------------------------------

async def _handle_auto_review(data: dict) -> None:
    """Scene 1: auto-review on MR open/update."""
    ctx = _extract_mr_context(data)
    thread_id = generate_thread_id(ctx["project_id"], ctx["mr_iid"])
    logger.info("[AutoReview] MR !%s in %s branch=%s", ctx["mr_iid"], ctx["project_id"], ctx["source_branch"])

    try:
        from agent.sandbox.manager import setup_sandbox
        from agent.scenes.auto_review.orchestrator import run_auto_review

        sandbox, repo_dir = await setup_sandbox(
            thread_id, ctx["project_id"], ctx["source_branch"]
        )
        result = await run_auto_review(
            project_id=ctx["project_id"],
            mr_iid=ctx["mr_iid"],
            repo_dir=repo_dir,
            sandbox=sandbox,
        )
        logger.info(
            "[AutoReview] Result MR !%s status=%s mode=%s confirmed=%s suspicious=%s open_questions=%s inline=%s",
            ctx["mr_iid"],
            result.status,
            result.review_mode,
            result.confirmed_findings_count,
            result.suspicious_findings_count,
            result.open_questions_count,
            result.inline_comments_count,
        )
    except Exception:
        logger.exception("[AutoReview] Failed MR !%s", ctx["mr_iid"])


async def _handle_mention(data: dict) -> None:
    """Handle @mention in MR comment via the primary Mention Agent workflow."""
    mr_data = data.get("merge_request", {})
    project = data.get("project", {})
    note = data.get("object_attributes", {})

    project_id = project.get("path_with_namespace", "")
    mr_iid = mr_data.get("iid")
    source_branch = mr_data.get("source_branch", "")
    comment_body = note.get("note", "")
    note_id = note.get("id")
    discussion_id = note.get("discussion_id")
    note_author = data.get("user", {}).get("username", "unknown")
    thread_id = generate_thread_id(project_id, mr_iid)

    # Strip @mention from the body
    clean_body = _clean_mention_body(comment_body)

    logger.info("[Mention] MR !%s: %s", mr_iid, clean_body[:80])

    # Acknowledge with eyes reaction
    try:
        from agent.gitlab.comments import add_eyes_reaction
        add_eyes_reaction(project_id, mr_iid, note_id)
    except Exception:
        logger.debug("Could not add eyes reaction", exc_info=True)

    try:
        from agent.sandbox.manager import setup_sandbox
        from agent.scenes.mention.orchestrator import run_mention

        # If source_branch missing from note data, fetch from MR metadata
        if not source_branch:
            from agent.gitlab.mr_info import get_mr_metadata
            meta = get_mr_metadata(project_id, mr_iid)
            source_branch = meta.source_branch

        sandbox, repo_dir = await setup_sandbox(thread_id, project_id, source_branch)
        await run_mention(
            project_id=project_id,
            mr_iid=mr_iid,
            repo_dir=repo_dir,
            sandbox=sandbox,
            note_id=note_id,
            discussion_id=discussion_id,
            note_body=clean_body,
            note_author=note_author,
        )
        logger.info("[Mention] Done MR !%s", mr_iid)
    except Exception:
        logger.exception("[Mention] Failed MR !%s", mr_iid)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive GitLab webhook events and dispatch to agent workflows."""
    token = request.headers.get("X-Gitlab-Token", "")
    if token != settings.GITLAB_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    body = await request.body()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "error", "reason": "invalid JSON"}, status_code=400)

    object_kind = data.get("object_kind")
    if object_kind != "merge_request" and _is_bot_user(data):
        return {"status": "ignored", "reason": "bot user"}

    mention_requested = object_kind == "note" and data.get("event_type") == "note" and _mention_requested(data)
    feedback_info = None if mention_requested else _ingest_feedback_event(data)

    # MR events → auto review
    if object_kind == "merge_request":
        project_path = data.get("project", {}).get("path_with_namespace", "")
        if not _is_target_project(project_path):
            return {"status": "ignored", "reason": "project not configured"}
        action = data.get("object_attributes", {}).get("action")
        if action in ("close", "merge"):
            ctx = _extract_mr_context(data)
            await enqueue_gitlab_event(
                EventEnvelope(
                    event_id=f"mr:{ctx['project_id']}:{ctx['mr_iid']}:{action}:sandbox-cleanup",
                    event_type="sandbox_cleanup",
                    project_id=ctx["project_id"],
                    mr_iid=ctx["mr_iid"],
                    source_branch=ctx["source_branch"],
                    target_branch=ctx["target_branch"],
                    title=ctx["title"],
                    head_sha=_extract_head_sha(data),
                    payload=data,
                )
            )
            return {"status": "accepted", "scene": "sandbox_cleanup"}
        if not _project_agent_enabled(project_path, "AUTO_REVIEW_ENABLED"):
            return {"status": "ignored", "reason": "auto_review disabled for project"}
        if action in ("open", "reopen"):
            if _is_draft(data):
                return {"status": "ignored", "reason": "draft MR"}
            ctx = _extract_mr_context(data)
            await enqueue_gitlab_event(
                EventEnvelope(
                    event_id=f"mr:{ctx['project_id']}:{ctx['mr_iid']}:{action}:{_extract_head_sha(data) or 'unknown'}",
                    event_type="auto_review",
                    project_id=ctx["project_id"],
                    mr_iid=ctx["mr_iid"],
                    source_branch=ctx["source_branch"],
                    target_branch=ctx["target_branch"],
                    title=ctx["title"],
                    head_sha=_extract_head_sha(data),
                    payload=data,
                )
            )
            return {"status": "accepted", "scene": "auto_review"}
        if action == "update" and data.get("object_attributes", {}).get("oldrev"):
            if _is_draft(data):
                return {"status": "ignored", "reason": "draft MR"}
            ctx = _extract_mr_context(data)
            await enqueue_gitlab_event(
                EventEnvelope(
                    event_id=f"mr:{ctx['project_id']}:{ctx['mr_iid']}:{action}:{_extract_head_sha(data) or 'unknown'}",
                    event_type="auto_review",
                    project_id=ctx["project_id"],
                    mr_iid=ctx["mr_iid"],
                    source_branch=ctx["source_branch"],
                    target_branch=ctx["target_branch"],
                    title=ctx["title"],
                    head_sha=_extract_head_sha(data),
                    payload=data,
                )
            )
            return {"status": "accepted", "scene": "auto_review_push"}
        if action == "update" and _is_draft_ready(data):
            ctx = _extract_mr_context(data)
            await enqueue_gitlab_event(
                EventEnvelope(
                    event_id=f"mr:{ctx['project_id']}:{ctx['mr_iid']}:{action}:draft-ready:{_extract_head_sha(data) or 'unknown'}",
                    event_type="auto_review",
                    project_id=ctx["project_id"],
                    mr_iid=ctx["mr_iid"],
                    source_branch=ctx["source_branch"],
                    target_branch=ctx["target_branch"],
                    title=ctx["title"],
                    head_sha=_extract_head_sha(data),
                    payload=data,
                )
            )
            return {"status": "accepted", "scene": "auto_review_draft_ready"}
        if feedback_info is not None:
            return {"status": "accepted", "scene": "feedback", **feedback_info}

    # Comment with @mention → unified mention agent
    elif object_kind == "note" and data.get("event_type") == "note":
        if "merge_request" not in data:
            if feedback_info is not None:
                return {"status": "accepted", "scene": "feedback", **feedback_info}
            return {"status": "ignored", "reason": "not a MR comment"}
        project_path = data.get("project", {}).get("path_with_namespace", "")
        if not _is_target_project(project_path):
            if feedback_info is not None:
                return {"status": "accepted", "scene": "feedback", **feedback_info}
            return {"status": "ignored", "reason": "project not configured"}
        if not _project_agent_enabled(project_path, "MENTION_ENABLED"):
            if feedback_info is not None:
                return {"status": "accepted", "scene": "feedback", **feedback_info}
            return {"status": "ignored", "reason": "mention disabled for project"}
        comment_body = data.get("object_attributes", {}).get("note", "")
        bot_username = get_bot_username()
        if not bot_username:
            return {"status": "ignored", "reason": "bot identity unavailable"}
        if _bot_mention_pattern(bot_username).search(comment_body) is None:
            if feedback_info is not None:
                return {"status": "accepted", "scene": "feedback", **feedback_info}
            return {"status": "ignored", "reason": "no @mention"}
        mr_data = data.get("merge_request", {})
        project = data.get("project", {})
        note = data.get("object_attributes", {})
        await enqueue_gitlab_event(
            EventEnvelope(
                event_id=f"note:{project.get('path_with_namespace', '')}:{mr_data.get('iid')}:{note.get('id')}",
                event_type="mention",
                project_id=project.get("path_with_namespace", ""),
                mr_iid=mr_data.get("iid"),
                source_branch=mr_data.get("source_branch", ""),
                head_sha=_extract_head_sha(data),
                note_id=note.get("id"),
                discussion_id=note.get("discussion_id"),
                note_body=_clean_mention_body(comment_body),
                note_author=data.get("user", {}).get("username", "unknown"),
                payload=data,
            )
        )
        return {"status": "accepted", "scene": "mention"}

    if feedback_info is not None:
        return {"status": "accepted", "scene": "feedback", **feedback_info}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
