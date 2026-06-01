"""Structured models for the auto-review workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field

Severity = Literal["low", "medium", "high"]
Confidence = Literal["low", "medium", "high"]
ReviewMode = Literal["full", "incremental"]
RunStatus = Literal["published", "skipped", "failed"]
ReviewProfile = Literal["docs_only", "standard", "deep"]
ReviewRecommendation = Literal["建议合并", "建议重新修改"]


class ReviewCommentContext(BaseModel):
    note_id: int | None = None
    discussion_id: str | None = None
    author: str = "unknown"
    body: str = ""
    created_at: str = ""
    file_path: str | None = None
    line: int | None = None
    is_bot: bool = False
    dedupe_keys: list[str] = Field(default_factory=list)
    head_sha: str | None = None
    diff_fingerprint: str | None = None


class ChangedFileContext(BaseModel):
    file_path: str
    old_path: str
    diff: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    added_lines: list[int] = Field(default_factory=list)


class StaticAnalysisFinding(BaseModel):
    tool: str
    file_path: str
    line: int | None = None
    severity: str
    message: str


class ReviewContext(BaseModel):
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
    review_run_id: str
    review_mode: ReviewMode
    diff_range: str
    commit_range: str
    diff_text: str
    diff_fingerprint: str
    diff_pack: str = ""
    diff_pack_compressed: bool = False
    diff_pack_overflow_files: list[str] = Field(default_factory=list)
    changed_files: list[ChangedFileContext] = Field(default_factory=list)
    commit_messages: list[str] = Field(default_factory=list)
    previous_review_head_sha: str | None = None
    previous_review_diff_fingerprint: str | None = None
    previous_bot_comments: list[ReviewCommentContext] = Field(default_factory=list)
    previous_bot_dedupe_keys: list[str] = Field(default_factory=list)
    recent_human_comments: list[ReviewCommentContext] = Field(default_factory=list)
    static_analysis_findings: list[StaticAnalysisFinding] = Field(default_factory=list)
    skip_reason: str | None = None


class CandidateFinding(BaseModel):
    source_lane: str = ""
    file_path: str | None = None
    line: int | None = None
    symbol: str | None = None
    category: str = ""
    severity: Severity = "medium"
    confidence: Confidence = "medium"
    summary: str
    details: str
    evidence: list[str] = Field(default_factory=list)
    recommended_fix: str | None = None
    dedupe_key: str | None = None


class LaneReviewResponse(BaseModel):
    summary: str = ""
    checks_run: list[str] = Field(default_factory=list)
    findings: list[CandidateFinding] = Field(default_factory=list)


class LaneReviewResult(BaseModel):
    lane: str
    status: Literal["ok", "degraded", "error"] = "ok"
    summary: str = ""
    checks_run: list[str] = Field(default_factory=list)
    findings: list[CandidateFinding] = Field(default_factory=list)
    error: str | None = None
    tool_error_count: int = 0
    semantic_failure_count: int = 0
    degraded_reason: str | None = None


class SpecialistReviewReport(BaseModel):
    lane: str
    status: Literal["ok", "degraded", "error"] = "ok"
    summary: str = ""
    checks_run: list[str] = Field(default_factory=list)
    findings: list[CandidateFinding] = Field(
        default_factory=list,
        validation_alias=AliasChoices("findings", "candidate_findings"),
    )
    investigation_notes: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    error: str | None = None
    tool_error_count: int = 0
    semantic_failure_count: int = 0
    degraded_reason: str | None = None

    @property
    def candidate_findings(self) -> list[CandidateFinding]:
        return self.findings


class OpenQuestion(BaseModel):
    source_lane: str | None = None
    file_path: str | None = None
    line: int | None = None
    summary: str
    details: str = ""
    evidence: list[str] = Field(default_factory=list)


class ReviewSeedContext(BaseModel):
    review_profile: ReviewProfile = "deep"
    diff_range: str = ""
    commit_range: str = ""
    changed_files: list[str] = Field(default_factory=list)
    commit_messages: list[str] = Field(default_factory=list)
    recent_human_comments: list[str] = Field(default_factory=list)
    previous_bot_comment_summaries: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    review_profile: ReviewProfile = "deep"
    repo_map: str = ""
    compile_check: dict[str, Any] | None = None
    risk_signals: list[str] = Field(default_factory=list)


class RankedReview(BaseModel):
    recommendation: ReviewRecommendation | None = None
    summary: str = ""
    confirmed_findings: list[CandidateFinding] = Field(
        default_factory=list,
        validation_alias=AliasChoices("confirmed_findings", "published_findings", "findings"),
    )
    suspicious_findings: list[CandidateFinding] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    inline_candidates: list[CandidateFinding] = Field(default_factory=list)

    @property
    def published_findings(self) -> list[CandidateFinding]:
        return self.inline_candidates


class JudgedFinding(CandidateFinding):
    publishable: bool = True
    publish_reason: str | None = None
    suppress_reason: str | None = None
    impact: Severity = "medium"
    evidence_strength: Confidence = "medium"
    novelty: Confidence = "medium"


class ChiefReviewDecision(BaseModel):
    recommendation: ReviewRecommendation | None = None
    summary: str = ""
    specialist_reports: list[SpecialistReviewReport] = Field(default_factory=list)
    confirmed_findings: list[CandidateFinding] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "confirmed_findings",
            "judged_findings",
            "published_findings",
            "findings",
        ),
    )
    suspicious_findings: list[CandidateFinding] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    @property
    def judged_findings(self) -> list[CandidateFinding]:
        return self.confirmed_findings


class AutoReviewRunResult(BaseModel):
    status: RunStatus
    reason: str | None = None
    review_run_id: str | None = None
    review_mode: ReviewMode | None = None
    recommendation: ReviewRecommendation | None = None
    compressed_review: bool = False
    confirmed_findings_count: int = 0
    suspicious_findings_count: int = 0
    open_questions_count: int = 0
    inline_comments_count: int = 0
