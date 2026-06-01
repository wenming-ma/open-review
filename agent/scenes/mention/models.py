"""Structured models for the agent-driven mention workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.scenes.auto_review.models import ChangedFileContext

MentionSubagentType = Literal["dialogs", "review", "repo-analyst"]
MentionReplyKind = Literal["reply", "follow_up", "analysis", "code_change"]
MentionReplyTarget = Literal["discussion", "mr_comment"]
MentionRunStatus = Literal["replied", "pushed", "skipped", "failed"]
MentionInlineSide = Literal["new", "old", "unchanged"]


class MRSnapshot(BaseModel):
    project_id: str
    mr_iid: int
    title: str
    description: str = ""
    author: str = "unknown"
    url: str = ""
    source_branch: str
    target_branch: str
    base_sha: str
    start_sha: str
    head_sha: str
    repo_dir: str
    diff_range: str
    commit_range: str
    diff_text: str
    changed_files: list[ChangedFileContext] = Field(default_factory=list)
    commit_messages: list[str] = Field(default_factory=list)


class MentionThreadMessage(BaseModel):
    note_id: int | None = None
    discussion_id: str | None = None
    author: str = "unknown"
    body: str = ""
    created_at: str = ""
    file_path: str | None = None
    line: int | None = None
    is_bot: bool = False
    kind: str = "note"


class MentionContext(BaseModel):
    project_id: str
    mr_iid: int
    note_id: int
    discussion_id: str | None = None
    note_body: str
    note_author: str = "unknown"
    trigger_note: MentionThreadMessage
    batched_notes: list[MentionThreadMessage] = Field(default_factory=list)
    covered_note_ids: list[int] = Field(default_factory=list)
    discussion_messages: list[MentionThreadMessage] = Field(default_factory=list)
    recent_mr_activity: list[MentionThreadMessage] = Field(default_factory=list)
    reply_target: MentionReplyTarget = "mr_comment"
    run_id: str
    mr_snapshot: MRSnapshot
    skip_reason: str | None = None


class MentionInlineSnippet(BaseModel):
    path: str
    line: int
    side: MentionInlineSide
    code: str
    lang: str = "text"


class MentionAgentResponse(BaseModel):
    reply_markdown: str
    reply_kind: MentionReplyKind = "reply"
    used_subagents: list[MentionSubagentType] = Field(default_factory=list)
    inline_snippets: list[MentionInlineSnippet] = Field(default_factory=list)


class MentionReviewVerdict(BaseModel):
    approved: bool
    feedback_markdown: str = ""


class MentionExecutionResult(BaseModel):
    intent: str
    status: MentionRunStatus
    reply_markdown: str
    covered_note_ids: list[int] = Field(default_factory=list)
    used_subagents: list[str] = Field(default_factory=list)
    inline_snippets: list[MentionInlineSnippet] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    validation_result: dict[str, Any] | None = None
    commit_sha: str | None = None
    review_approved: bool | None = None
    review_rounds: int = 0
    degraded_reason: str | None = None
