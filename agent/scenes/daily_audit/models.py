"""Structured models for the daily audit workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AuditUnitType = Literal["feature", "function", "loop", "code_region", "action_workflow"]
FindingCategory = Literal["bug", "performance", "optimization"]
FindingConfidence = Literal["low", "medium", "high"]
RecommendedAction = Literal["report_only", "autofix"]
DailyAuditStatus = Literal["reported", "merge_request_opened", "skipped", "failed"]
DailyAuditSubagentType = Literal[
    "candidate_scout",
    "focus_selector",
    "analysis_specialist",
    "correctness_reviewer",
    "performance_reviewer",
    "optimization_reviewer",
    "verification_agent",
    "evolution_curator",
    "repo-analyst",
]


class AuditCandidate(BaseModel):
    unit_type: AuditUnitType
    label: str
    file_path: str | None = None
    rationale: str = ""
    start_line: int | None = None
    end_line: int | None = None
    entrypoint_kind: str = ""
    entrypoint_symbol: str = ""
    workflow_summary: str = ""
    entry_evidence: list[str] = Field(default_factory=list)


class AuditUnit(AuditCandidate):
    pass


class DailyFinding(BaseModel):
    category: FindingCategory
    confidence: FindingConfidence = "medium"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None


class DailyAuditContext(BaseModel):
    project_id: str
    actor_key: str
    repo_dir: str
    default_branch: str
    run_id: str
    session_id: str = ""
    experiment_root: str = ""
    candidates: list[AuditCandidate] = Field(default_factory=list)
    selected_unit: AuditUnit | None = None


class DailyAuditSelectionResponse(BaseModel):
    selected_unit: AuditUnit
    selection_reasoning: str = ""
    used_subagents: list[DailyAuditSubagentType] = Field(default_factory=list)


class SubagentObservation(BaseModel):
    subagent: DailyAuditSubagentType
    summary: str


class DailyAuditAgentResponse(BaseModel):
    selected_unit: AuditUnit
    summary_markdown: str = ""
    report_markdown: str = ""
    recommended_action: RecommendedAction = "report_only"
    findings: list[DailyFinding] = Field(default_factory=list)
    used_subagents: list[DailyAuditSubagentType] = Field(default_factory=list)
    subagent_observations: list[SubagentObservation] = Field(default_factory=list)
    merge_request_title: str | None = None
    merge_request_description: str | None = None
    commit_message: str | None = None
    branch_name: str | None = None


class DailyAuditRunResult(BaseModel):
    status: DailyAuditStatus
    reason: str | None = None
    unit_type: AuditUnitType | None = None
    unit_label: str | None = None
    experiment_root: str | None = None
    finding_count: int = 0
    used_subagents: list[str] = Field(default_factory=list)
    issue_iid: int | None = None
    merge_request_iid: int | None = None
    merge_request_url: str | None = None
    commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    changed_line_count: int = 0
    degraded_reason: str | None = None
