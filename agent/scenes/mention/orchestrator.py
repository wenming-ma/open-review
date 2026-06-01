"""Agent-driven mention workflow."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import shlex

from langchain_core.messages import HumanMessage

from agent.config import settings
from agent.gitlab.comments import (
    MRCommentRecord,
    list_mr_activity,
    post_diff_discussion,
    post_mr_comment,
    reply_to_mr_discussion,
)
from agent.gitlab.identity import get_bot_username, resolve_bot_identity
from agent.gitlab.mr_info import can_push_to_branch, get_mr_metadata
from agent.observability import build_open_review_trace_name, start_open_review_span
from agent.runtime.journal_observer import record_runtime_observation, runtime_observation_scope
from agent.runtime.termination import RunTerminationRequested, raise_if_run_termination_requested
from agent.sandbox.manager import (
    cleanup_temporary_worktree,
    commit_all_and_get_sha,
    create_temporary_worktree,
    push_branch_head,
)
from agent.scenes.auto_review.models import ChangedFileContext
from agent.scenes.auto_review.orchestrator import (
    _collect_changed_files,
    _collect_commit_messages,
    _ensure_review_refs,
)
from agent.scenes.mention.graph import (
    build_mention_agent,
    build_mention_author_agent,
    build_mention_reviewer_agent,
)
from agent.scenes.mention.models import (
    MentionAgentResponse,
    MentionContext,
    MentionExecutionResult,
    MentionInlineSnippet,
    MentionReviewVerdict,
    MentionThreadMessage,
    MRSnapshot,
)
from agent.utils.diff_parser import resolve_diff_line_position
from agent.utils.timezone import compact_timestamp

logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"<!--\s*(open-review-[a-z-]+):\s*([^\s>]+)\s*-->")
_HIDDEN_OPEN_REVIEW_MARKER_RE = re.compile(r"^\s*<!--\s*open-review-[^>]*-->\s*$")
_MAX_MENTION_REVIEW_ROUNDS = 10
_REVIEW_REJECTED_MAX_ROUNDS_REASON = "review_rejected_after_max_rounds"


def _accepts_sandbox_kwarg(func) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return "sandbox" in signature.parameters


def _call_with_optional_sandbox(func, *args, sandbox=None, **kwargs):
    if sandbox is not None and _accepts_sandbox_kwarg(func):
        return func(*args, sandbox=sandbox, **kwargs)
    return func(*args, **kwargs)


def _is_bot_author(author: str) -> bool:
    bot_username = get_bot_username()
    return bool(bot_username) and author.strip().lower() == bot_username.lower()


def _extract_markers(body: str) -> dict[str, str]:
    return dict(_MARKER_RE.findall(body))


def _git_current_diff(repo_dir: str, diff_range: str, *, sandbox=None) -> str:
    from agent.scenes.auto_review.orchestrator import _git

    result = _git(
        repo_dir,
        "diff",
        "--unified=3",
        "--find-renames",
        diff_range,
        sandbox=sandbox,
    )
    return result.stdout if result.returncode == 0 else ""


def _head_is_current(project_id: str, mr_iid: int, expected_head_sha: str | None) -> bool:
    if not expected_head_sha:
        return True
    return get_mr_metadata(project_id, mr_iid).head_sha == expected_head_sha


def _source_branch_is_pushable(context: MentionContext) -> bool:
    try:
        return can_push_to_branch(context.project_id, context.mr_snapshot.source_branch)
    except Exception:
        logger.warning(
            "Pushability preflight failed for %s!%s branch=%s; deferring to git push result",
            context.project_id,
            context.mr_iid,
            context.mr_snapshot.source_branch,
            exc_info=True,
        )
        return True


def _build_run_id(note_id: int, head_sha: str) -> str:
    timestamp = compact_timestamp()
    digest = hashlib.sha256(f"{note_id}:{head_sha}".encode()).hexdigest()[:8]
    return f"{timestamp}-{digest}"


def _record_to_thread_message(record: MRCommentRecord) -> MentionThreadMessage:
    return MentionThreadMessage(
        note_id=record.note_id,
        discussion_id=record.discussion_id,
        author=record.author,
        body=record.body,
        created_at=record.created_at,
        file_path=record.file_path,
        line=record.line,
        is_bot=_is_bot_author(record.author),
        kind=record.kind,
    )


def _normalize_changed_files(items: list[ChangedFileContext | dict]) -> list[ChangedFileContext]:
    return [
        item if isinstance(item, ChangedFileContext) else ChangedFileContext.model_validate(item)
        for item in items
    ]


def _trigger_note(
    activity: list[MRCommentRecord],
    *,
    note_id: int,
    discussion_id: str | None,
    note_body: str,
    note_author: str,
) -> MentionThreadMessage:
    for item in activity:
        if item.note_id == note_id:
            return _record_to_thread_message(item)
    return MentionThreadMessage(
        note_id=note_id,
        discussion_id=discussion_id,
        author=note_author,
        body=note_body,
        is_bot=_is_bot_author(note_author),
        kind="discussion" if discussion_id else "note",
    )


def _skip_reason(activity: list[MRCommentRecord], *, note_id: int, head_sha: str) -> str | None:
    for item in activity:
        if not _is_bot_author(item.author):
            continue
        markers = _extract_markers(item.body)
        if markers.get("open-review-mention-note") == str(note_id) and markers.get("open-review-head-sha") == head_sha:
            return "note_already_processed"
    return None


def _skip_reason_for_note_ids(
    activity: list[MRCommentRecord],
    *,
    note_ids: list[int],
    head_sha: str,
) -> str | None:
    required = {str(note_id) for note_id in note_ids}
    if not required:
        return None

    for item in activity:
        if not _is_bot_author(item.author):
            continue
        markers = _extract_markers(item.body)
        if markers.get("open-review-head-sha") != head_sha:
            continue
        covered = {
            note_id.strip()
            for note_id in markers.get("open-review-covered-notes", "").split(",")
            if note_id.strip()
        }
        primary = markers.get("open-review-mention-note")
        if primary:
            covered.add(primary)
        if required.issubset(covered):
            return "note_already_processed"
    return None


def _batched_note(note: dict, default_discussion_id: str | None) -> MentionThreadMessage:
    return MentionThreadMessage(
        note_id=note.get("note_id"),
        discussion_id=note.get("discussion_id", default_discussion_id),
        author=note.get("note_author", "unknown"),
        body=note.get("note_body", ""),
        created_at=note.get("created_at", ""),
        is_bot=_is_bot_author(note.get("note_author", "unknown")),
        kind="discussion" if note.get("discussion_id", default_discussion_id) else "note",
    )


def build_mention_context(
    *,
    project_id: str,
    mr_iid: int,
    repo_dir: str,
    sandbox=None,
    note_id: int,
    discussion_id: str | None,
    note_body: str,
    note_author: str,
    batched_events: list[dict] | None = None,
) -> MentionContext:
    meta = get_mr_metadata(project_id, mr_iid)
    _call_with_optional_sandbox(
        _ensure_review_refs,
        project_id,
        repo_dir,
        meta.source_branch,
        meta.target_branch,
        sandbox=sandbox,
    )
    diff_range = f"origin/{meta.target_branch}...HEAD"
    commit_range = f"origin/{meta.target_branch}..HEAD"
    diff_text = _call_with_optional_sandbox(
        _git_current_diff,
        repo_dir,
        diff_range,
        sandbox=sandbox,
    )
    changed_files = _normalize_changed_files(
        _call_with_optional_sandbox(
            _collect_changed_files,
            repo_dir,
            diff_range,
            sandbox=sandbox,
        )
        if diff_text
        else []
    )
    commit_messages = _call_with_optional_sandbox(
        _collect_commit_messages,
        repo_dir,
        commit_range,
        sandbox=sandbox,
    )
    activity = sorted(list_mr_activity(project_id, mr_iid), key=lambda item: (item.created_at or "", item.note_id or 0))
    trigger_note = _trigger_note(
        activity,
        note_id=note_id,
        discussion_id=discussion_id,
        note_body=note_body,
        note_author=note_author,
    )
    effective_discussion_id = discussion_id or trigger_note.discussion_id
    batched_notes = [
        _batched_note(item, effective_discussion_id)
        for item in (batched_events or [])
        if item.get("note_id")
    ]
    if not batched_notes:
        batched_notes = [trigger_note]
    covered_note_ids = sorted(
        {
            item.note_id
            for item in batched_notes
            if item.note_id is not None
        }
    )

    discussion_messages = [
        _record_to_thread_message(item)
        for item in activity
        if effective_discussion_id and item.discussion_id == effective_discussion_id
    ]
    recent_activity = [_record_to_thread_message(item) for item in activity[-20:]]

    snapshot = MRSnapshot(
        project_id=project_id,
        mr_iid=mr_iid,
        title=meta.title,
        description=meta.description,
        author=meta.author,
        url=meta.url,
        source_branch=meta.source_branch,
        target_branch=meta.target_branch,
        base_sha=meta.base_sha,
        start_sha=meta.start_sha,
        head_sha=meta.head_sha,
        repo_dir=repo_dir,
        diff_range=diff_range,
        commit_range=commit_range,
        diff_text=diff_text,
        changed_files=changed_files,
        commit_messages=commit_messages,
    )
    return MentionContext(
        project_id=project_id,
        mr_iid=mr_iid,
        note_id=note_id,
        discussion_id=effective_discussion_id,
        note_body=note_body,
        note_author=note_author,
        trigger_note=trigger_note,
        batched_notes=batched_notes,
        covered_note_ids=covered_note_ids,
        discussion_messages=discussion_messages,
        recent_mr_activity=recent_activity,
        reply_target="discussion" if effective_discussion_id else "mr_comment",
        run_id=_build_run_id(note_id, meta.head_sha),
        mr_snapshot=snapshot,
        skip_reason=_skip_reason_for_note_ids(activity, note_ids=covered_note_ids, head_sha=meta.head_sha)
        or _skip_reason(activity, note_id=note_id, head_sha=meta.head_sha),
    )
def _sandbox_command(sandbox, command: str) -> str:
    result = sandbox.execute(command)
    if result.exit_code != 0:
        raise RuntimeError(result.output.strip() or command)
    return result.output


def _working_tree_has_changes(sandbox, worktree_dir: str) -> bool:
    try:
        status = _sandbox_command(sandbox, f"git -C {shlex.quote(worktree_dir)} status --porcelain")
    except AttributeError:
        return bool(_collect_changed_paths(sandbox, worktree_dir))
    normalized = status.strip()
    return bool(normalized and normalized != "<no output>")


def _collect_changed_paths(sandbox, worktree_dir: str) -> list[str]:
    output = _sandbox_command(sandbox, f"git -C {shlex.quote(worktree_dir)} status --short")
    paths = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        paths.append(line[3:] if len(line) > 3 else line)
    return paths


def _commit_and_push(sandbox, project_id: str, worktree_dir: str, source_branch: str) -> str:
    with start_open_review_span(
        "open_review.mention.commit",
        attributes={"open_review.source_branch": source_branch},
        tags=["mention", "commit"],
    ):
        commit_sha = commit_all_and_get_sha(
            worktree_dir=worktree_dir,
            message="fix: address mention request",
            sandbox=sandbox,
        )
    with start_open_review_span(
        "open_review.mention.push",
        attributes={"open_review.source_branch": source_branch},
        tags=["mention", "push"],
    ):
        push_branch_head(
            project_id=project_id,
            worktree_dir=worktree_dir,
            source_branch=source_branch,
            sandbox=sandbox,
        )
    return commit_sha


def _message_text(message) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part for part in parts if part).strip()
    return ""


def _coerce_agent_response(payload) -> MentionAgentResponse:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected mention agent payload type: {type(payload).__name__}")
    structured = payload.get("structured_response")
    if isinstance(structured, MentionAgentResponse):
        return structured.model_copy(update={"reply_markdown": _strip_hidden_open_review_markers(structured.reply_markdown)})
    if isinstance(structured, dict):
        response = MentionAgentResponse.model_validate(structured)
        return response.model_copy(update={"reply_markdown": _strip_hidden_open_review_markers(response.reply_markdown)})
    if "reply_markdown" in payload:
        response = MentionAgentResponse.model_validate(payload)
        return response.model_copy(update={"reply_markdown": _strip_hidden_open_review_markers(response.reply_markdown)})

    keys = ",".join(sorted(payload.keys()))
    raise RuntimeError(f"missing structured_response (payload keys: {keys})")


def _coerce_review_verdict(payload) -> MentionReviewVerdict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected mention reviewer payload type: {type(payload).__name__}")
    structured = payload.get("structured_response")
    if isinstance(structured, MentionReviewVerdict):
        return structured
    if isinstance(structured, dict):
        return MentionReviewVerdict.model_validate(structured)
    if "approved" in payload:
        return MentionReviewVerdict.model_validate(payload)

    keys = ",".join(sorted(payload.keys()))
    raise RuntimeError(f"reviewer missing structured_response (payload keys: {keys})")


def _strip_hidden_open_review_markers(text: str) -> str:
    if not text.strip():
        return ""
    lines = [line for line in text.splitlines() if not _HIDDEN_OPEN_REVIEW_MARKER_RE.match(line)]
    return "\n".join(lines).strip()


def _append_reply_note(reply_markdown: str, note: str) -> str:
    if not note.strip():
        return reply_markdown.strip()
    if not reply_markdown.strip():
        return note.strip()
    return f"{reply_markdown.strip()}\n\n{note.strip()}".strip()


def _append_push_success_note(reply_markdown: str, source_branch: str, commit_sha: str) -> str:
    short_sha = commit_sha[:8]
    return _append_reply_note(
        reply_markdown,
        f"我已经推送改动到 `{source_branch}` 分支。\n新提交：`{short_sha}`。",
    )


def _find_changed_file(
    context: MentionContext,
    file_path: str,
) -> ChangedFileContext | None:
    for item in getattr(context.mr_snapshot, "changed_files", []) or []:
        if item.file_path == file_path or item.old_path == file_path:
            return item
    return None


def _format_inline_snippet_body(snippet: MentionInlineSnippet) -> str:
    return f"```{snippet.lang}\n{snippet.code}\n```"


def _format_inline_snippet_fallback(snippet: MentionInlineSnippet) -> str:
    return f"相关代码：`{snippet.path}:{snippet.line}`\n\n```{snippet.lang}\n{snippet.code}\n```"


def _resolve_snippet_position(context: MentionContext, snippet: MentionInlineSnippet) -> dict[str, int | str] | None:
    changed_file = _find_changed_file(context, snippet.path)
    if changed_file is None:
        return None
    position = resolve_diff_line_position(
        changed_file.diff,
        side=snippet.side,
        line=snippet.line,
    )
    if position is None:
        return None
    payload: dict[str, int | str] = {
        "new_path": changed_file.file_path,
        "old_path": changed_file.old_path,
        "new_line": None,
        "old_line": None,
    }
    if snippet.side == "new":
        if position.new_line is None:
            return None
        payload["new_line"] = position.new_line
        return payload
    if snippet.side == "old":
        if position.old_line is None:
            return None
        payload["old_line"] = position.old_line
        return payload
    if position.new_line is None or position.old_line is None:
        return None
    payload["new_line"] = position.new_line
    payload["old_line"] = position.old_line
    return payload


async def _publish_inline_snippets(
    context: MentionContext,
    reply_markdown: str,
    inline_snippets: list[MentionInlineSnippet],
    *,
    publish_service=None,
) -> str:
    rewritten = reply_markdown.strip()
    seen: set[tuple[str, int, str, str, str]] = set()

    for snippet in inline_snippets:
        dedupe_key = (snippet.path, snippet.line, snippet.side, snippet.lang, snippet.code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        position = _resolve_snippet_position(context, snippet)
        if position is None:
            rewritten = _append_reply_note(rewritten, _format_inline_snippet_fallback(snippet))
            continue

        body = _format_inline_snippet_body(snippet)
        content_hash = hashlib.sha256(snippet.code.encode("utf-8")).hexdigest()[:12]
        if publish_service is not None:
            receipt = await publish_service.publish_inline_comment(
                op_key=(
                    "mention-inline:"
                    f"{context.mr_snapshot.head_sha}:"
                    f"{','.join(str(item) for item in getattr(context, 'covered_note_ids', []) or [])}:"
                    f"{snippet.path}:{snippet.side}:{snippet.line}:{content_hash}"
                ),
                publisher=lambda body=body, position=position: post_diff_discussion(
                    context.project_id,
                    context.mr_iid,
                    body,
                    fallback_to_note=False,
                    **position,
                ),
                record={
                    "object_kind": "inline_comment",
                    "mr_iid": context.mr_iid,
                    "file_path": snippet.path,
                    "line": snippet.line,
                    "body_snapshot": body,
                    "marker_map": {
                        "open-review-mention-run": context.run_id,
                        "open-review-head-sha": context.mr_snapshot.head_sha,
                    },
                },
            )
            published = receipt.external_id is not None
        else:
            published = (
                post_diff_discussion(
                    context.project_id,
                    context.mr_iid,
                    body,
                    fallback_to_note=False,
                    **position,
                )
                is not None
            )
        if not published:
            rewritten = _append_reply_note(rewritten, _format_inline_snippet_fallback(snippet))

    return rewritten.strip()


def _extract_used_subagents(payload) -> list[str]:
    if not isinstance(payload, dict):
        return []

    used: list[str] = []

    def _append(name: str | None) -> None:
        if not name or name in used:
            return
        used.append(name)

    for message in payload.get("messages", []) or []:
        tool_calls = message.get("tool_calls", []) if isinstance(message, dict) else getattr(message, "tool_calls", []) or []
        for call in tool_calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name != "task":
                continue
            args = call.get("args", {}) if isinstance(call, dict) else getattr(call, "args", {}) or {}
            subagent_type = args.get("subagent_type") if isinstance(args, dict) else None
            _append(subagent_type)

    if used:
        return used

    structured = payload.get("structured_response")
    if structured is None:
        return used
    response = MentionAgentResponse.model_validate(structured)
    for name in response.used_subagents:
        _append(name)
    return used


def _author_thread_id(context: MentionContext) -> str:
    return f"mention:{context.run_id}:author"


def _reviewer_thread_id(context: MentionContext) -> str:
    return f"mention:{context.run_id}:reviewer"


def _current_worktree_diff(sandbox, worktree_dir: str) -> str:
    try:
        return _sandbox_command(
            sandbox,
            f"git -C {shlex.quote(worktree_dir)} diff --unified=3 --find-renames",
        )
    except Exception:
        logger.warning("Failed to capture current worktree diff for %s", worktree_dir, exc_info=True)
        return ""


def _format_inline_snippets_for_review(inline_snippets: list[MentionInlineSnippet]) -> str:
    if not inline_snippets:
        return "- none"
    lines = []
    for item in inline_snippets:
        lines.append(
            f"- {item.path}:{item.line} ({item.side}, {item.lang})\n```{item.lang}\n{item.code}\n```"
        )
    return "\n".join(lines)


def _build_reviewer_request(
    context: MentionContext,
    response: MentionAgentResponse,
    *,
    worktree_dir: str,
    round_index: int,
    changed_files: list[str],
    diff_text: str,
) -> str:
    changed_lines = "\n".join(f"- {path}" for path in changed_files) or "- none"
    candidate = {
        "reply_kind": response.reply_kind,
        "used_subagents": list(response.used_subagents),
        "reply_markdown": response.reply_markdown.strip(),
    }
    return (
        f"请审核第 {round_index} 轮 mention 候选结果。\n\n"
        f"## 原始用户请求\n{context.note_body.strip()}\n\n"
        "## 候选结构化结果\n"
        f"{json.dumps(candidate, ensure_ascii=False, indent=2)}\n\n"
        "## 候选 inline 片段\n"
        f"{_format_inline_snippets_for_review(list(response.inline_snippets))}\n\n"
        "## 当前工作树变更文件\n"
        f"{changed_lines}\n\n"
        "## 当前工作树 diff\n"
        f"```diff\n{diff_text.strip() or '(no diff)'}\n```\n\n"
        f"工作树路径：{worktree_dir}\n"
        "请只输出审核结论和最关键的修订反馈。"
    )


def _build_author_revision_message(feedback_markdown: str, *, round_index: int) -> str:
    return (
        f"第 {round_index} 轮 reviewer 没有通过当前候选结果。\n\n"
        "请根据下面的审核反馈直接修订你的候选答复或候选代码修改；保留正确部分，修正问题后重新给出完整结构化结果。\n\n"
        "## Reviewer 反馈\n"
        f"{feedback_markdown.strip() or '(no feedback provided)'}"
    )


async def _invoke_author_agent(
    context: MentionContext,
    *,
    agent,
    worktree_dir: str,
    messages,
    round_index: int,
):
    with start_open_review_span(
        f"open_review.mention.author.round.{round_index}",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.note_id": context.note_id,
            "open_review.run_id": context.run_id,
            "open_review.review_round": round_index,
        },
        tags=["mention", "author", f"round-{round_index}"],
    ):
        await record_runtime_observation(
            "mention author round started",
            details={"mention_role": "author", "mention_round": round_index},
        )
        with runtime_observation_scope(mention_role="author", mention_round=round_index):
            payload = await agent.ainvoke(
                {"messages": messages},
                config={
                    "run_name": build_open_review_trace_name(
                        "mention-author",
                        f"{context.project_id}!{context.mr_iid}",
                        note_id=context.note_id,
                        head_sha=context.mr_snapshot.head_sha,
                        run_key=f"{context.run_id}:author:{round_index}",
                    ),
                    "configurable": {
                        "project_id": context.project_id,
                        "mr_iid": context.mr_iid,
                        "repo_dir": worktree_dir,
                        "thread_id": _author_thread_id(context),
                        "round_index": round_index,
                    }
                },
            )
        return _coerce_agent_response(payload), payload, _extract_used_subagents(payload)


async def _invoke_reviewer_agent(
    context: MentionContext,
    *,
    agent,
    worktree_dir: str,
    messages,
    round_index: int,
):
    with start_open_review_span(
        f"open_review.mention.review.round.{round_index}",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.note_id": context.note_id,
            "open_review.run_id": context.run_id,
            "open_review.review_round": round_index,
        },
        tags=["mention", "review", f"round-{round_index}"],
    ):
        await record_runtime_observation(
            "mention reviewer round started",
            details={"mention_role": "reviewer", "mention_round": round_index},
        )
        with runtime_observation_scope(mention_role="reviewer", mention_round=round_index):
            payload = await agent.ainvoke(
                {"messages": messages},
                config={
                    "run_name": build_open_review_trace_name(
                        "mention-review",
                        f"{context.project_id}!{context.mr_iid}",
                        note_id=context.note_id,
                        head_sha=context.mr_snapshot.head_sha,
                        run_key=f"{context.run_id}:review:{round_index}",
                    ),
                    "configurable": {
                        "project_id": context.project_id,
                        "mr_iid": context.mr_iid,
                        "repo_dir": worktree_dir,
                        "thread_id": _reviewer_thread_id(context),
                        "round_index": round_index,
                    },
                },
            )
        return _coerce_review_verdict(payload), payload


def _materialize_main_agent_result(
    context: MentionContext,
    sandbox,
    *,
    worktree_dir: str,
    response: MentionAgentResponse,
    used_subagents: list[str],
    review_approved: bool,
    review_rounds: int,
    allow_push: bool,
    degraded_reason: str | None = None,
) -> MentionExecutionResult:
    result = MentionExecutionResult(
        intent=response.reply_kind,
        status="replied",
        reply_markdown=response.reply_markdown.strip(),
        covered_note_ids=list(context.covered_note_ids),
        used_subagents=used_subagents,
        inline_snippets=list(response.inline_snippets),
        review_approved=review_approved,
        review_rounds=review_rounds,
        degraded_reason=degraded_reason,
    )

    if not _working_tree_has_changes(sandbox, worktree_dir):
        if response.reply_kind == "code_change":
            result.degraded_reason = degraded_reason or "no_file_changes"
            result.reply_markdown = _append_reply_note(
                result.reply_markdown,
                "我没有推送改动，因为工作树没有实际变化。",
            )
        return result

    changed_files = _collect_changed_paths(sandbox, worktree_dir)
    result.changed_files = changed_files

    if response.reply_kind != "code_change":
        result.degraded_reason = degraded_reason or "dirty_worktree_without_code_change_reply"
        result.reply_markdown = _append_reply_note(
            result.reply_markdown,
            "我没有推送，因为工作树发生了变化，但这次结果没有被声明为代码修改。",
        )
        return result

    max_changed_files = int(settings.MENTION_MAX_CHANGED_FILES or 0)
    if max_changed_files > 0 and len(changed_files) > max_changed_files:
        result.degraded_reason = "changed_file_limit_exceeded"
        result.reply_markdown = _append_reply_note(
            result.reply_markdown,
            f"我没有推送，因为本次改动涉及 {len(changed_files)} 个文件，超过本次允许修改的文件数上限 {max_changed_files}。",
        )
        return result

    if not allow_push:
        result.degraded_reason = degraded_reason or _REVIEW_REJECTED_MAX_ROUNDS_REASON
        result.reply_markdown = _append_reply_note(
            result.reply_markdown,
            f"我没有推送代码，因为 reviewer 连续 {_MAX_MENTION_REVIEW_ROUNDS} 轮都没有通过这版结果。",
        )
        return result

    if not _source_branch_is_pushable(context):
        result.degraded_reason = "source_branch_not_pushable"
        result.reply_markdown = _append_reply_note(
            result.reply_markdown,
            f"我没有推送，因为我没有 GitLab 上 `{context.mr_snapshot.source_branch}` 分支的推送权限。",
        )
        return result

    if not _head_is_current(context.project_id, context.mr_iid, context.mr_snapshot.head_sha):
        result.degraded_reason = "stale_head_sha"
        result.reply_markdown = _append_reply_note(
            result.reply_markdown,
            "我处理期间合并请求的 head 已变化，所以没有推送代码。",
        )
        return result

    result.commit_sha = _commit_and_push(
        sandbox,
        context.project_id,
        worktree_dir,
        context.mr_snapshot.source_branch,
    )
    result.reply_markdown = _append_push_success_note(
        result.reply_markdown,
        context.mr_snapshot.source_branch,
        result.commit_sha,
    )
    result.status = "pushed"
    return result


async def _run_main_agent(
    context: MentionContext,
    sandbox,
    *,
    worktree_dir: str,
    runtime_run_id: str | None = None,
    review_approved: bool = True,
    review_rounds: int = 1,
    allow_push: bool = True,
    degraded_reason: str | None = None,
) -> MentionExecutionResult:
    agent = build_mention_agent(
        sandbox=sandbox,
        repo_dir=worktree_dir,
        source_branch=context.mr_snapshot.source_branch,
        context=context,
        runtime_run_id=runtime_run_id,
    )
    response, payload, used_subagents = await _invoke_author_agent(
        context,
        agent=agent,
        worktree_dir=worktree_dir,
        messages=[HumanMessage(content=context.note_body)],
        round_index=1,
    )
    del payload
    return _materialize_main_agent_result(
        context,
        sandbox,
        worktree_dir=worktree_dir,
        response=response,
        used_subagents=used_subagents,
        review_approved=review_approved,
        review_rounds=review_rounds,
        allow_push=allow_push,
        degraded_reason=degraded_reason,
    )


async def _run_author_reviewer_loop(
    context: MentionContext,
    sandbox,
    *,
    worktree_dir: str,
    runtime_run_id: str | None = None,
) -> MentionExecutionResult:
    with start_open_review_span(
        "open_review.mention.loop",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.note_id": context.note_id,
            "open_review.run_id": context.run_id,
        },
        tags=["mention", "loop"],
    ):
        author_messages = [HumanMessage(content=context.note_body)]
        reviewer_messages = []
        author_agent = build_mention_author_agent(
            sandbox=sandbox,
            repo_dir=worktree_dir,
            source_branch=context.mr_snapshot.source_branch,
            context=context,
            runtime_run_id=runtime_run_id,
        )
        reviewer_agent = build_mention_reviewer_agent(
            sandbox=sandbox,
            repo_dir=worktree_dir,
            context=context,
            runtime_run_id=runtime_run_id,
        )
        latest_response: MentionAgentResponse | None = None
        latest_used_subagents: list[str] = []

        for round_index in range(1, _MAX_MENTION_REVIEW_ROUNDS + 1):
            author_request_messages = list(author_messages)
            response, author_payload, used_subagents = await _invoke_author_agent(
                context,
                agent=author_agent,
                worktree_dir=worktree_dir,
                messages=author_request_messages,
                round_index=round_index,
            )
            latest_response = response
            latest_used_subagents = used_subagents
            author_messages = list(author_payload.get("messages") or author_messages)
            await record_runtime_observation(
                "mention author round completed",
                details={
                    "mention_role": "author",
                    "mention_round": round_index,
                    "reply_kind": response.reply_kind,
                    "used_subagents": used_subagents,
                },
            )

            changed_files = _collect_changed_paths(sandbox, worktree_dir) if _working_tree_has_changes(sandbox, worktree_dir) else []
            diff_text = _current_worktree_diff(sandbox, worktree_dir) if changed_files else ""
            reviewer_messages = list(reviewer_messages) + [
                HumanMessage(
                    content=_build_reviewer_request(
                        context,
                        response,
                        worktree_dir=worktree_dir,
                        round_index=round_index,
                        changed_files=changed_files,
                        diff_text=diff_text,
                    )
                )
            ]
            reviewer_request_messages = list(reviewer_messages)
            verdict, reviewer_payload = await _invoke_reviewer_agent(
                context,
                agent=reviewer_agent,
                worktree_dir=worktree_dir,
                messages=reviewer_request_messages,
                round_index=round_index,
            )
            reviewer_messages = list(reviewer_payload.get("messages") or reviewer_messages)
            await record_runtime_observation(
                "mention reviewer round completed",
                details={
                    "mention_role": "reviewer",
                    "mention_round": round_index,
                    "approved": verdict.approved,
                },
            )

            if verdict.approved:
                return _materialize_main_agent_result(
                    context,
                    sandbox,
                    worktree_dir=worktree_dir,
                    response=response,
                    used_subagents=used_subagents,
                    review_approved=True,
                    review_rounds=round_index,
                    allow_push=True,
                )

            if round_index == _MAX_MENTION_REVIEW_ROUNDS:
                return _materialize_main_agent_result(
                    context,
                    sandbox,
                    worktree_dir=worktree_dir,
                    response=response,
                    used_subagents=used_subagents,
                    review_approved=False,
                    review_rounds=round_index,
                    allow_push=False,
                    degraded_reason=_REVIEW_REJECTED_MAX_ROUNDS_REASON,
                )

            author_messages = list(author_messages) + [
                HumanMessage(
                    content=_build_author_revision_message(
                        verdict.feedback_markdown,
                        round_index=round_index,
                    )
                )
            ]

        if latest_response is None:
            raise RuntimeError("mention author did not produce any candidate response")

        return _materialize_main_agent_result(
            context,
            sandbox,
            worktree_dir=worktree_dir,
            response=latest_response,
            used_subagents=latest_used_subagents,
            review_approved=False,
            review_rounds=_MAX_MENTION_REVIEW_ROUNDS,
            allow_push=False,
            degraded_reason=_REVIEW_REJECTED_MAX_ROUNDS_REASON,
        )


def _format_reply_body(context: MentionContext, result: MentionExecutionResult) -> str:
    run_id = getattr(context, "run_id", "unknown")
    note_id = getattr(context, "note_id", None)
    if note_id is None:
        trigger_note = getattr(context, "trigger_note", None)
        note_id = getattr(trigger_note, "note_id", 0)
    covered_note_ids = result.covered_note_ids or getattr(context, "covered_note_ids", [])
    coverage_line = ""
    if covered_note_ids:
        coverage_line = "本次回复覆盖 note：" + ", ".join(f"#{item}" for item in covered_note_ids) + "。"
    lines = [
        _strip_hidden_open_review_markers(result.reply_markdown),
        *(["", coverage_line] if coverage_line else []),
        "",
        f"<!-- open-review-mention-run: {run_id} -->",
        f"<!-- open-review-mention-note: {note_id} -->",
        f"<!-- open-review-covered-notes: {','.join(str(item) for item in covered_note_ids)} -->",
        f"<!-- open-review-mention-intent: {result.intent} -->",
        f"<!-- open-review-head-sha: {context.mr_snapshot.head_sha} -->",
    ]
    if result.used_subagents:
        lines.append(f"<!-- open-review-used-subagents: {','.join(result.used_subagents)} -->")
    if result.review_approved is not None:
        lines.append(f"<!-- open-review-review-approved: {str(result.review_approved).lower()} -->")
    if result.review_rounds:
        lines.append(f"<!-- open-review-review-rounds: {result.review_rounds} -->")
    if result.commit_sha:
        lines.append(f"<!-- open-review-commit-sha: {result.commit_sha} -->")
    return "\n".join(lines).strip()


async def _publish_mention_result(context: MentionContext, result: MentionExecutionResult, publish_service=None) -> None:
    with start_open_review_span(
        "open_review.mention.publish",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.note_id": getattr(context, "note_id", getattr(getattr(context, "trigger_note", None), "note_id", 0)),
            "open_review.intent": result.intent,
        },
        tags=["mention", "publish"],
    ):
        result.reply_markdown = await _publish_inline_snippets(
            context,
            result.reply_markdown,
            result.inline_snippets,
            publish_service=publish_service,
        )
        body = _format_reply_body(context, result)
        covered_note_ids = result.covered_note_ids or getattr(context, "covered_note_ids", [])
        reply_op_key = (
            f"mention-reply:{context.mr_snapshot.head_sha}:{','.join(str(item) for item in covered_note_ids)}"
        )
        reply_target = getattr(context, "reply_target", "discussion" if getattr(context, "discussion_id", None) else "mr_comment")
        if reply_target == "discussion" and context.discussion_id:
            try:
                if publish_service is not None:
                    await publish_service.publish_discussion_reply(
                        op_key=reply_op_key,
                        publisher=lambda: reply_to_mr_discussion(
                            context.project_id,
                            context.mr_iid,
                            context.discussion_id,
                            body,
                        ),
                        record={
                            "object_kind": "discussion_reply",
                            "mr_iid": context.mr_iid,
                            "discussion_id": context.discussion_id,
                            "body_snapshot": body,
                            "marker_map": {
                                "open-review-mention-run": context.run_id,
                                "open-review-head-sha": context.mr_snapshot.head_sha,
                            },
                        },
                    )
                else:
                    reply_to_mr_discussion(context.project_id, context.mr_iid, context.discussion_id, body)
            except Exception as exc:
                logger.warning(
                    "Discussion reply failed for %s!%s discussion=%s, falling back to MR note: %s: %s",
                    context.project_id,
                    context.mr_iid,
                    context.discussion_id,
                    exc.__class__.__name__,
                    exc,
                )
                if publish_service is not None:
                    await publish_service.publish_mr_note(
                        op_key=f"{reply_op_key}:fallback",
                        publisher=lambda: post_mr_comment(context.project_id, context.mr_iid, body),
                        record={
                            "object_kind": "mr_note",
                            "mr_iid": context.mr_iid,
                            "body_snapshot": body,
                            "marker_map": {
                                "open-review-mention-run": context.run_id,
                                "open-review-head-sha": context.mr_snapshot.head_sha,
                            },
                        },
                    )
                else:
                    post_mr_comment(context.project_id, context.mr_iid, body)
        else:
            if publish_service is not None:
                await publish_service.publish_mr_note(
                    op_key=reply_op_key,
                    publisher=lambda: post_mr_comment(context.project_id, context.mr_iid, body),
                    record={
                        "object_kind": "mr_note",
                        "mr_iid": context.mr_iid,
                        "body_snapshot": body,
                        "marker_map": {
                            "open-review-mention-run": context.run_id,
                            "open-review-head-sha": context.mr_snapshot.head_sha,
                        },
                    },
                )
            else:
                post_mr_comment(context.project_id, context.mr_iid, body)


async def run_mention(
    *,
    project_id: str,
    mr_iid: int,
    repo_dir: str,
    sandbox,
    note_id: int,
    discussion_id: str | None,
    note_body: str,
    note_author: str,
    expected_head_sha: str | None = None,
    batched_events: list[dict] | None = None,
    model_id: str | None = None,
    publish_service=None,
    runtime_run_id: str | None = None,
) -> MentionExecutionResult:
    del model_id
    await raise_if_run_termination_requested(
        run_id=runtime_run_id,
        actor_key=f"{project_id}!{mr_iid}",
    )
    context = build_mention_context(
        project_id=project_id,
        mr_iid=mr_iid,
        repo_dir=repo_dir,
        sandbox=sandbox,
        note_id=note_id,
        discussion_id=discussion_id,
        note_body=note_body,
        note_author=note_author,
        batched_events=batched_events,
    )
    with start_open_review_span(
        build_open_review_trace_name(
            "mention",
            f"{project_id}!{mr_iid}",
            note_id=note_id,
            head_sha=getattr(getattr(context, "mr_snapshot", None), "head_sha", ""),
            run_key=getattr(context, "run_id", f"note-{note_id}"),
        ),
        session_id=f"{project_id}!{mr_iid}",
        user_id=note_author,
        attributes={
            "open_review.project_id": project_id,
            "open_review.mr_iid": mr_iid,
            "open_review.note_id": note_id,
            "open_review.run_id": getattr(context, "run_id", f"note-{note_id}"),
            "open_review.head_sha": getattr(getattr(context, "mr_snapshot", None), "head_sha", ""),
        },
        metadata={"covered_note_ids": getattr(context, "covered_note_ids", [])},
        tags=["mention", "run"],
    ):
        if context.skip_reason:
            return MentionExecutionResult(
                intent="reply",
                status="skipped",
                reply_markdown=f"跳过重复 mention 处理：{context.skip_reason}。",
                degraded_reason=context.skip_reason,
            )
        if expected_head_sha and context.mr_snapshot.head_sha != expected_head_sha:
            return MentionExecutionResult(
                intent="reply",
                status="skipped",
                reply_markdown="执行开始前合并请求的 head 已变化，本次 mention 已跳过。",
                degraded_reason="stale_webhook_head_sha",
            )
        identity = resolve_bot_identity()
        if identity.identity is None:
            return MentionExecutionResult(
                intent="reply",
                status="failed",
                reply_markdown=f"当前无法解析 GitLab Bot 身份：{identity.error or 'unknown error'}",
                degraded_reason="gitlab_bot_identity_unavailable",
            )

        worktree_dir: str | None = None
        try:
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=f"{project_id}!{mr_iid}",
            )
            worktree_dir = create_temporary_worktree(
                sandbox,
                repo_dir=repo_dir,
                head_sha=context.mr_snapshot.head_sha,
                run_id=context.run_id,
            )
            loop_kwargs = {"worktree_dir": worktree_dir}
            if runtime_run_id is not None:
                loop_kwargs["runtime_run_id"] = runtime_run_id
            result = await _run_author_reviewer_loop(
                context,
                sandbox,
                **loop_kwargs,
            )
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=f"{project_id}!{mr_iid}",
            )
            if publish_service is None:
                publish_result = _publish_mention_result(context, result)
            else:
                publish_result = _publish_mention_result(
                    context,
                    result,
                    publish_service=publish_service,
                )
            if inspect.isawaitable(publish_result):
                await publish_result
            return result
        except RunTerminationRequested:
            raise
        except Exception as exc:
            logger.exception("Mention run failed for %s!%s note=%s", project_id, mr_iid, note_id)
            failure = MentionExecutionResult(
                intent="analysis",
                status="failed",
                reply_markdown=f"处理这次 mention 时发生错误：\n\n```\n{exc}\n```",
                covered_note_ids=list(context.covered_note_ids),
                degraded_reason=str(exc),
            )
            if publish_service is None:
                publish_result = _publish_mention_result(context, failure)
            else:
                publish_result = _publish_mention_result(
                    context,
                    failure,
                    publish_service=publish_service,
                )
            if inspect.isawaitable(publish_result):
                await publish_result
            return failure
        finally:
            if worktree_dir:
                cleanup_temporary_worktree(sandbox, repo_dir=repo_dir, worktree_dir=worktree_dir)
