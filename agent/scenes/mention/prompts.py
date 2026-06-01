"""Prompt builders for the agent-driven mention workflow."""

from __future__ import annotations

import re

from agent.prompt import EDA_STANDARDS
from agent.rlm import REPO_ANALYST_DESCRIPTION
from agent.scenes.mention.models import MentionContext, MentionSubagentType
from agent.scenes.mention.scope import authoritative_scope_summary
from agent.scenes.mention.selfevolution.prompts import load_prompt_asset_text

_SUBAGENT_DESCRIPTIONS: dict[MentionSubagentType, str] = {
    "dialogs": "Debug failures, isolate root causes, and explain the evidence chain behind a bug or regression.",
    "review": "Review pending conclusions or code changes for correctness, regressions, and missing validation.",
    "repo-analyst": REPO_ANALYST_DESCRIPTION,
}

_SUBAGENT_ROLE_RULES: dict[MentionSubagentType, str] = {
    "dialogs": (
        "Focus on debugging and diagnosis. Return the most likely root cause, the supporting evidence, "
        "and the smallest next action the main Mention Agent should take."
    ),
    "review": (
        "Review the proposed conclusion or pending code changes. Challenge weak assumptions, point out "
        "regression risk, and state clearly whether the result looks correct enough to proceed."
    ),
    "repo-analyst": (
        "Use repository-scale recursive analysis when the task needs cross-file reasoning, whole-repo synthesis, "
        "or impact tracing beyond what a narrow auxiliary subagent can reliably hold in context. Pass JSON with "
        "`question`, and include `file_paths` and `keywords` when you know relevant files or symbols."
    ),
}

_HIDDEN_OPEN_REVIEW_MARKER_RE = re.compile(r"^\s*<!--\s*open-review-[^>]*-->\s*$")


def describe_mention_subagent(subagent_type: MentionSubagentType) -> str:
    return _SUBAGENT_DESCRIPTIONS[subagent_type]


def _strip_hidden_open_review_markers(text: str) -> str:
    if not text.strip():
        return ""
    lines = [line for line in text.splitlines() if not _HIDDEN_OPEN_REVIEW_MARKER_RE.match(line)]
    cleaned = "\n".join(lines).strip()
    return cleaned


def _thread_text(context: MentionContext) -> str:
    if context.discussion_messages:
        items = context.discussion_messages
    else:
        items = context.recent_mr_activity[-12:]
    if not items:
        return "(no discussion history)"
    lines = []
    for item in items:
        location = ""
        if item.file_path:
            location = f" ({item.file_path}:{item.line or '?'})"
        body = _strip_hidden_open_review_markers(item.body or "")
        lines.append(f"- [{item.created_at or 'unknown'}] {item.author}{location}: {body}")
    return "\n".join(lines)


def _batched_note_text(context: MentionContext) -> str:
    if not context.batched_notes:
        return "- none"
    lines = []
    for item in context.batched_notes:
        body = _strip_hidden_open_review_markers(item.body or "")
        lines.append(f"- note {item.note_id or '?'} by {item.author}: {body}")
    return "\n".join(lines)


def _mr_state_text(context: MentionContext) -> str:
    snapshot = context.mr_snapshot
    changed = []
    for item in snapshot.changed_files[:20]:
        suffix = []
        if item.new_file:
            suffix.append("new")
        if item.deleted_file:
            suffix.append("deleted")
        if item.renamed_file:
            suffix.append("renamed")
        changed.append(f"- {item.file_path}" + (f" ({', '.join(suffix)})" if suffix else ""))
    changed_text = "\n".join(changed) or "- none"
    commit_text = "\n".join(f"- {item}" for item in snapshot.commit_messages[:10]) or "- none"
    return f"""MR: !{snapshot.mr_iid} {snapshot.title}
Author: {snapshot.author}
Source branch: {snapshot.source_branch}
Target branch: {snapshot.target_branch}
Head SHA: {snapshot.head_sha}
Diff range: {snapshot.diff_range}

Description:
{snapshot.description or "(empty)"}

Changed files:
{changed_text}

Commit messages:
{commit_text}
"""


def build_mention_author_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    context: MentionContext,
) -> str:
    return load_prompt_asset_text("author-prompt").format(
        eda_standards=EDA_STANDARDS,
        file_tool_repo_dir=file_tool_repo_dir,
        repo_dir=repo_dir,
        thread_text=_thread_text(context),
        batched_note_text=_batched_note_text(context),
        mr_state_text=_mr_state_text(context),
        authoritative_scope_summary=authoritative_scope_summary(context),
    )


def build_mention_agent_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    context: MentionContext,
) -> str:
    return build_mention_author_prompt(repo_dir, file_tool_repo_dir, context)


def build_mention_reviewer_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    context: MentionContext,
) -> str:
    return load_prompt_asset_text("reviewer-prompt").format(
        eda_standards=EDA_STANDARDS,
        file_tool_repo_dir=file_tool_repo_dir,
        repo_dir=repo_dir,
        thread_text=_thread_text(context),
        batched_note_text=_batched_note_text(context),
        mr_state_text=_mr_state_text(context),
        authoritative_scope_summary=authoritative_scope_summary(context),
    )


def build_mention_auxiliary_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    subagent_type: MentionSubagentType,
    context: MentionContext,
) -> str:
    return load_prompt_asset_text("auxiliary-prompt").format(
        subagent_type=subagent_type,
        responsibility=_SUBAGENT_ROLE_RULES[subagent_type],
        eda_standards=EDA_STANDARDS,
        file_tool_repo_dir=file_tool_repo_dir,
        repo_dir=repo_dir,
        thread_text=_thread_text(context),
        batched_note_text=_batched_note_text(context),
        mr_state_text=_mr_state_text(context),
        authoritative_scope_summary=authoritative_scope_summary(context),
    )
