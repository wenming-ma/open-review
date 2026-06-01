"""Runtime models for durable MR actor execution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.utils.timezone import iso_now

EventType = Literal[
    "auto_review",
    "mention",
    "sandbox_cleanup",
    "daily_audit",
    "agent_self_evolution",
    "daily_audit_evolution",
    "daily_audit_direction_persistence",
    "daily_audit_short_term_persistence",
    "daily_audit_long_term_persistence",
    "daily_audit_skill_persistence",
]
RunState = Literal["queued", "running", "publishing", "succeeded", "skipped", "failed", "stale", "terminated"]
PublishChannel = Literal["mr_note", "discussion_reply", "inline_comment", "project_issue", "merge_request"]
PublishStatus = Literal["claimed", "completed", "failed"]
RunJournalEventType = Literal[
    "stage_started",
    "stage_completed",
    "checkpoint_written",
    "observation",
    "run_restarted",
    "run_failed",
    "run_completed",
    "run_terminated",
]
RunJournalStatus = Literal["running", "completed", "failed", "skipped", "restarted", "terminated"]


class ActorRuntimeStatus(BaseModel):
    actor_key: str
    pending_count: int = 0
    inflight_count: int = 0
    lease_owner: str | None = None
    lease_ttl_seconds: int | None = None
    scheduled: bool = False


class EventEnvelope(BaseModel):
    event_id: str
    event_type: EventType
    project_id: str
    mr_iid: int | None = None
    source_branch: str = ""
    target_branch: str = "main"
    title: str = ""
    head_sha: str | None = None
    note_id: int | None = None
    discussion_id: str | None = None
    note_body: str | None = None
    note_author: str | None = None
    received_at: str = Field(default_factory=iso_now)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def actor_key(self) -> str:
        if self.event_type == "agent_self_evolution":
            agent_type = str(self.payload.get("agent_type") or "").strip()
            return f"{self.project_id}!self_evolution:{agent_type or 'unknown'}"
        if self.event_type in {"daily_audit", "daily_audit_evolution"}:
            return f"{self.project_id}!daily_audit"
        if self.event_type == "daily_audit_direction_persistence":
            return f"{self.project_id}!daily_audit_direction_persistence"
        if self.event_type == "daily_audit_short_term_persistence":
            return f"{self.project_id}!daily_audit_short_term_persistence"
        if self.event_type == "daily_audit_long_term_persistence":
            return f"{self.project_id}!daily_audit_long_term_persistence"
        if self.event_type == "daily_audit_skill_persistence":
            return f"{self.project_id}!daily_audit_skill_persistence"
        return f"{self.project_id}!{self.mr_iid}"


class RunRecord(BaseModel):
    run_id: str
    execution_key: str | None = None
    actor_key: str
    event_type: EventType
    project_id: str
    mr_iid: int | None = None
    event_ids: list[str] = Field(default_factory=list)
    batch_size: int = 1
    head_sha: str | None = None
    note_id: int | None = None
    discussion_id: str | None = None
    state: RunState = "running"
    started_at: str = Field(default_factory=iso_now)
    completed_at: str | None = None
    reason: str | None = None
    error: str | None = None


class PublishReceipt(BaseModel):
    actor_key: str
    op_key: str
    channel: PublishChannel
    external_id: str | None = None
    status: PublishStatus = "completed"
    created_at: str = Field(default_factory=iso_now)


class RunJournalEvent(BaseModel):
    execution_key: str
    run_id: str | None = None
    actor_key: str
    scene: EventType
    workflow_version: str
    stage_key: str | None = None
    event_type: RunJournalEventType
    status: RunJournalStatus
    summary: str | None = None
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=iso_now)


class RunCheckpoint(BaseModel):
    execution_key: str
    actor_key: str
    scene: EventType
    workflow_version: str
    stage_key: str
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=iso_now)


class RunTerminationRequest(BaseModel):
    run_id: str
    actor_key: str
    requested_by: str
    requested_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)


class InflightRunState(BaseModel):
    execution_key: str
    run_id: str
    actor_key: str
    scene: EventType
    workflow_version: str
    stage_key: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    resume_attempts: int = 0
    updated_at: str = Field(default_factory=iso_now)
