"""Tests for the staged auto-review orchestrator."""

from __future__ import annotations

import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.gitlab.comments import MRCommentRecord
from agent.runtime.termination import RunTerminationRequested
from agent.scenes.auto_review import orchestrator
from agent.scenes.auto_review.models import (
    AutoReviewRunResult,
    CandidateFinding,
    ChangedFileContext,
    ChiefReviewDecision,
    EvidenceBundle,
    OpenQuestion,
    RankedReview,
    ReviewContext,
    ReviewSeedContext,
    SpecialistReviewReport,
)
from agent.scenes.auto_review.prompts import (
    AUTO_REVIEW_DIRECTOR_PROMPT,
    build_auto_review_investigation_subagent_prompt,
    build_auto_review_specialist_prompt,
)


@pytest.fixture(autouse=True)
def _mock_valid_gitlab_identity(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "resolve_bot_identity",
        lambda **_kwargs: SimpleNamespace(
            identity=SimpleNamespace(username="open-review-bot"),
            source="live",
            error=None,
            fetched_at="2026-04-10T10:00:00+00:00",
        ),
    )
    monkeypatch.setattr(orchestrator, "get_bot_username", lambda **_kwargs: "open-review-bot")


def _make_context(**overrides) -> ReviewContext:
    data = {
        "project_id": "team/project",
        "mr_iid": 42,
        "title": "Fix router regression",
        "description": "Some MR description",
        "author": "dev",
        "url": "http://gitlab/team/project/-/merge_requests/42",
        "source_branch": "feature/router-fix",
        "target_branch": "main",
        "base_sha": "base123",
        "start_sha": "start123",
        "head_sha": "head123",
        "repo_dir": "/tmp/repo",
        "review_run_id": "run-123",
        "review_mode": "full",
        "diff_range": "origin/main...HEAD",
        "commit_range": "origin/main..HEAD",
        "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
        "diff_fingerprint": "fp123",
        "changed_files": [],
        "commit_messages": [],
        "previous_review_head_sha": None,
        "previous_review_diff_fingerprint": None,
        "previous_bot_comments": [],
        "previous_bot_dedupe_keys": [],
        "recent_human_comments": [],
        "static_analysis_findings": [],
        "skip_reason": None,
    }
    data.update(overrides)
    return ReviewContext(**data)


def test_build_comment_context_extracts_markers():
    record = MRCommentRecord(
        note_id=1,
        discussion_id="abc",
        author="open-review-bot",
        body=(
            "Issue body\n"
            "<!-- open-review-review-run: run-1 -->\n"
            "<!-- open-review-dedupe: dedupe-1 -->\n"
            "<!-- open-review-head-sha: head-1 -->\n"
        ),
        created_at="2026-04-09T00:00:00Z",
        file_path="src/router.cpp",
        line=19,
        is_system=False,
        kind="discussion",
    )

    context = orchestrator._build_comment_context(record)

    assert context.is_bot is True
    assert context.dedupe_keys == ["dedupe-1"]
    assert context.head_sha == "head-1"
    assert context.file_path == "src/router.cpp"
    assert context.line == 19


def test_ensure_review_refs_uses_host_repo_helper(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        orchestrator,
        "ensure_repo_refs",
        lambda *, project_id, repo_dir, source_branch, target_branch, sandbox=None: captured.update(
            {
                "project_id": project_id,
                "repo_dir": repo_dir,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "sandbox": sandbox,
            }
        ),
    )

    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")
    orchestrator._ensure_review_refs(
        "team/project",
        "/tmp/repo",
        "feature/router-fix",
        "main",
        sandbox=sandbox,
    )

    assert captured == {
        "project_id": "team/project",
        "repo_dir": "/tmp/repo",
        "source_branch": "feature/router-fix",
        "target_branch": "main",
        "sandbox": sandbox,
    }


def test_finalize_ranked_review_skips_previous_bot_duplicates():
    context = _make_context(
        previous_bot_dedupe_keys=["dup-1"],
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -18,2 +18,2 @@\n-old\n+new\n",
                added_lines=[19, 22],
            )
        ],
    )
    ranked = RankedReview(
        summary="ranked summary",
        confirmed_findings=[
            CandidateFinding(
                source_lane="security",
                file_path="src/router.cpp",
                line=19,
                category="security",
                severity="high",
                confidence="high",
                summary="Null check missing",
                details="A null pointer can be dereferenced.",
                evidence=["caller passes nullable pointer"],
                dedupe_key="dup-1",
            ),
            CandidateFinding(
                source_lane="regression",
                file_path="src/router.cpp",
                line=22,
                category="regression",
                severity="medium",
                confidence="high",
                summary="State transition misses rollback path",
                details="Rollback no longer runs for failed routes.",
                evidence=["diff removes the rollback branch"],
                dedupe_key="dup-2",
            ),
        ],
    )

    finalized = orchestrator._finalize_ranked_review(context, ranked, lane_results=[])

    assert [item.dedupe_key for item in finalized.confirmed_findings] == ["dup-2"]
    assert [item.dedupe_key for item in finalized.inline_candidates] == ["dup-2"]


def test_max_published_findings_respects_configured_limit(monkeypatch):
    monkeypatch.setattr(orchestrator.settings, "AUTO_REVIEW_MAX_PUBLISHED_FINDINGS", 3)

    assert orchestrator._max_published_findings(7) == 3
    assert orchestrator._max_published_findings(2) == 2


def test_format_summary_comment_includes_markers_and_counts():
    context = _make_context(
        review_mode="incremental",
        head_sha="head999",
        diff_fingerprint="fp999",
        diff_pack_compressed=True,
        diff_pack_overflow_files=["src/overflow.cpp"],
    )
    ranked = RankedReview(
        recommendation="建议重新修改",
        summary="Ranked findings ready for publication.",
        confirmed_findings=[
            CandidateFinding(
                source_lane="contracts",
                file_path="src/router.cpp",
                line=37,
                category="contracts",
                severity="medium",
                confidence="high",
                summary="Missing test coverage for error branch",
                details="The new branch has no regression test.",
                evidence=["no nearby test updates"],
                dedupe_key="dup-3",
            )
        ],
        suspicious_findings=[
            CandidateFinding(
                source_lane="contracts",
                file_path="src/router.cpp",
                line=40,
                category="contracts",
                severity="low",
                confidence="medium",
                summary="可能还缺一个边界断言",
                details="当前调查没有看到更强证据，需要人工确认。",
                evidence=["附近只有成功路径测试"],
                dedupe_key="dup-4",
            )
        ],
        open_questions=[
            OpenQuestion(
                source_lane="contracts",
                summary="是否故意只覆盖成功路径",
                details="作者意图尚不清楚。",
                evidence=["commit message 未说明"],
            )
        ],
        inline_candidates=[
            CandidateFinding(
                source_lane="contracts",
                file_path="src/router.cpp",
                line=37,
                category="contracts",
                severity="medium",
                confidence="high",
                summary="Missing test coverage for error branch",
                details="The new branch has no regression test.",
                evidence=["no nearby test updates"],
                dedupe_key="dup-3",
            )
        ],
    )
    lane_results = [SpecialistReviewReport(lane="contracts", status="ok")]

    body = orchestrator._format_summary_comment(context, ranked, lane_results)

    assert "审查结论：**建议重新修改**" in body
    assert "模式：`incremental`" in body
    assert "已确认问题：`1`" in body
    assert "可疑问题：`1`" in body
    assert "开放问题：`1`" in body
    assert "Inline 评论：`1`" in body
    assert "### 已确认问题" not in body
    assert "### 可疑问题" not in body
    assert "### 开放问题" not in body
    assert "### 检查项" not in body
    assert "contracts：正常" not in body
    assert "src/router.cpp" not in body
    assert "<!-- open-review-head-sha: head999 -->" in body
    assert "<!-- open-review-diff-fingerprint: fp999 -->" in body
    assert "<!-- open-review-summary-kind: auto-review -->" in body


def test_ranked_review_accepts_findings_alias():
    finding = CandidateFinding(
        source_lane="contracts",
        file_path="src/router.cpp",
        line=37,
        category="contracts",
        severity="medium",
        confidence="high",
        summary="Missing error-path coverage",
        details="The new branch still lacks a regression test.",
        evidence=["no nearby test updates"],
        dedupe_key="dup-alias",
    )

    ranked = RankedReview.model_validate(
        {
            "summary": "有候选问题。",
            "confirmed_findings": [finding.model_dump(mode="json")],
            "suspicious_findings": [],
            "open_questions": [],
            "inline_candidates": [],
        }
    )

    assert ranked.summary == "有候选问题。"
    assert [item.dedupe_key for item in ranked.confirmed_findings] == ["dup-alias"]


def test_specialist_review_report_accepts_candidate_findings_alias():
    finding = CandidateFinding(
        source_lane="contracts",
        file_path="src/router.cpp",
        line=37,
        category="contracts",
        severity="medium",
        confidence="high",
        summary="Missing error-path coverage",
        details="The new branch still lacks a regression test.",
        evidence=["no nearby test updates"],
        dedupe_key="dup-candidate",
    )

    report = SpecialistReviewReport.model_validate(
        {
            "lane": "contracts",
            "summary": "调查完成",
            "candidate_findings": [finding.model_dump(mode="json")],
            "investigation_notes": ["查了附近测试和头文件"],
            "supporting_evidence": ["只改了 router.cpp"],
            "open_questions": ["是否故意不测失败路径"],
        }
    )

    assert [item.dedupe_key for item in report.candidate_findings] == ["dup-candidate"]
    assert report.findings == report.candidate_findings
    assert report.investigation_notes == ["查了附近测试和头文件"]
    assert report.supporting_evidence == ["只改了 router.cpp"]
    assert report.open_questions == ["是否故意不测失败路径"]


def test_format_summary_comment_omits_lane_health_details():
    context = _make_context()
    ranked = RankedReview(
        recommendation="建议合并",
        summary="已发现需要人工确认的问题。",
    )
    lane_results = [
        SpecialistReviewReport(
            lane="contracts",
            status="degraded",
            summary="lane summary",
            tool_error_count=1,
            semantic_failure_count=2,
            degraded_reason="file_tool_errors_detected",
        )
    ]

    body = orchestrator._format_summary_comment(context, ranked, lane_results)

    assert "contracts：降级" not in body
    assert "工具错误 1" not in body
    assert "语义失败 2" not in body
    assert "file_tool_errors_detected" not in body


def test_specialist_and_subagent_prompts_do_not_delegate_publish_or_suppress():
    specialist_prompt = build_auto_review_specialist_prompt("/workspace/repo", "/workspace/repo", "security")
    subagent_prompt = build_auto_review_investigation_subagent_prompt(
        "/workspace/repo",
        "/workspace/repo",
        "trace-impact",
    )

    assert "You are not responsible for deciding whether a finding should be published or suppressed." in specialist_prompt
    assert "If you do not have enough evidence for a publishable issue" not in specialist_prompt
    assert "You are not responsible for deciding whether a finding should be published or suppressed." in subagent_prompt
    assert "Return a concise factual report" not in subagent_prompt


def test_git_inspector_prompt_forces_shell_git_and_blocks_temp_script_flow():
    prompt = build_auto_review_investigation_subagent_prompt(
        "/workspace/repo",
        "/workspace/repo",
        "git-inspector",
    )

    assert "execute" in prompt
    assert "status --short" in prompt
    assert "diff --unified=3 --find-renames" in prompt
    assert "log --oneline" in prompt
    assert "/workspace/tmp" in prompt
    assert "Do not write temporary helper scripts" in prompt
    assert ".git/*" in prompt


def test_director_prompt_reports_without_suppress_or_publish_decision():
    assert "Only you decide which candidate findings are ultimately published, suppressed, or left unresolved." not in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "You do not suppress findings." in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "must call all five" not in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "todo" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "call the same specialist or investigation subagent multiple times" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "code-grounded evidence" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "Do not nitpick" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "If there are no confirmed bugs" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "recommendation" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "confirmed_findings" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "suspicious_findings" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "open_questions" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "specialist_reports" not in AUTO_REVIEW_DIRECTOR_PROMPT


def test_director_prompt_prioritizes_regression_and_existing_feature_safety():
    assert "openreview / open-swe style" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "highest priority" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "broken existing behavior" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "introduced new bugs" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "follow the affected workflow end-to-end" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "call `repo-analyst` once before finalizing" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "impact-chain gaps" in AUTO_REVIEW_DIRECTOR_PROMPT


def test_director_prompt_keeps_positive_observations_out_of_findings():
    assert "Do not promote positive observations" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "no sensitive data" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "confirmed findings" in AUTO_REVIEW_DIRECTOR_PROMPT
    assert "investigation notes" in AUTO_REVIEW_DIRECTOR_PROMPT


def test_specialist_prompt_uses_scope_driven_safety_checklist():
    prompt = build_auto_review_specialist_prompt("/workspace/repo", "/workspace/repo", "correctness")

    assert "highest priority is to determine whether the current fix or feature change" in prompt
    assert "breaks existing behavior or introduces a new bug elsewhere" in prompt
    assert "Trace the full affected workflow" in prompt
    assert "prefer `repo-analyst`" in prompt
    assert "use `repo-analyst` or explicitly state why local evidence is sufficient" in prompt
    assert "shared or public helper" in prompt
    assert "widely used function" in prompt
    assert "callers, callees, triggers" in prompt
    assert "Only expand these checks when they are relevant to the changed workflow or impacted features" in prompt
    assert "performance risks" in prompt
    assert "comments or syntax" in prompt
    assert "null handling" in prompt
    assert "duplicate existing helpers or logic" in prompt


def test_specialist_prompt_requires_actionable_negative_findings_only():
    prompt = build_auto_review_specialist_prompt("/workspace/repo", "/workspace/repo", "correctness")

    assert "Only put actionable negative issues in `candidate_findings`" in prompt
    assert "positive observations" in prompt
    assert "investigation_notes" in prompt
    assert "tiny or meta-only diff" in prompt


def test_build_review_context_skips_when_head_sha_already_reviewed(monkeypatch):
    diff_text = "diff --git a/x b/x\n"
    diff_fingerprint = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    metadata = SimpleNamespace(
        title="Fix router regression",
        description="desc",
        source_branch="feature/router-fix",
        target_branch="main",
        author="dev",
        url="http://gitlab/mr/42",
        base_sha="base",
        start_sha="start",
        head_sha="head123",
    )
    activity = [
        MRCommentRecord(
            note_id=10,
            discussion_id=None,
            author="open-review-bot",
            body=f"<!-- open-review-head-sha: head123 -->\n<!-- open-review-diff-fingerprint: {diff_fingerprint} -->",
            created_at="2026-04-09T00:00:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="note",
        )
    ]

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: activity)
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_review_ranges",
        lambda *_args: ("full", "origin/main...HEAD", "origin/main..HEAD"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=diff_text,
            stderr="",
        ),
    )
    monkeypatch.setattr(orchestrator, "_collect_changed_files", lambda *_args: [])
    monkeypatch.setattr(orchestrator, "_collect_commit_messages", lambda *_args: [])
    context = orchestrator.build_review_context("team/project", 42, "/tmp/repo")

    assert context.previous_review_head_sha == "head123"
    assert context.skip_reason == "head_sha_already_reviewed"
    assert context.static_analysis_findings == []


def test_build_review_context_does_not_skip_same_head_when_diff_changed(monkeypatch):
    metadata = SimpleNamespace(
        title="Fix router regression",
        description="desc",
        source_branch="feature/router-fix",
        target_branch="main",
        author="dev",
        url="http://gitlab/mr/42",
        base_sha="base",
        start_sha="start",
        head_sha="head123",
    )
    activity = [
        MRCommentRecord(
            note_id=10,
            discussion_id=None,
            author="open-review-bot",
            body="<!-- open-review-head-sha: head123 -->\n<!-- open-review-diff-fingerprint: old-fingerprint -->",
            created_at="2026-04-09T00:00:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="note",
        )
    ]

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: activity)
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_review_ranges",
        lambda *_args: ("full", "origin/main...HEAD", "origin/main..HEAD"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(orchestrator, "_collect_changed_files", lambda *_args: [])
    monkeypatch.setattr(orchestrator, "_collect_commit_messages", lambda *_args: [])

    context = orchestrator.build_review_context("team/project", 42, "/tmp/repo")

    assert context.previous_review_head_sha == "head123"
    assert context.previous_review_diff_fingerprint == "old-fingerprint"
    assert context.skip_reason is None


def test_build_review_context_raises_when_git_diff_fails(monkeypatch):
    metadata = SimpleNamespace(
        title="Fix router regression",
        description="desc",
        source_branch="feature/router-fix",
        target_branch="main",
        author="dev",
        url="http://gitlab/mr/42",
        base_sha="base",
        start_sha="start",
        head_sha="head123",
    )

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: [])
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_review_ranges",
        lambda *_args: ("full", "origin/main...HEAD", "origin/main..HEAD"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=128,
            stdout="",
            stderr="fatal: origin/main...HEAD: no merge base",
        ),
    )

    with pytest.raises(RuntimeError, match="git diff failed"):
        orchestrator.build_review_context("team/project", 42, "/tmp/repo")


def test_build_diff_pack_marks_overflow_when_budget_is_exceeded():
    changed_files = [
        ChangedFileContext(
            file_path="src/a.cpp",
            old_path="src/a.cpp",
            diff="@@ -1 +1 @@\n-int a;\n+int a = 1;\n" * 40,
        ),
        ChangedFileContext(
            file_path="src/b.cpp",
            old_path="src/b.cpp",
            diff="@@ -1 +1 @@\n-int b;\n+int b = 2;\n" * 40,
        ),
    ]

    diff_pack, compressed, overflow = orchestrator._build_diff_pack(changed_files, max_chars=1200)

    assert "## File: `src/a.cpp`" in diff_pack
    assert compressed is True
    assert overflow == ["src/b.cpp"]


def test_lane_message_includes_prepared_diff_pack():
    context = _make_context(
        diff_pack="## File: `src/router.cpp`\n@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
        diff_pack_overflow_files=["src/other.cpp"],
        diff_pack_compressed=True,
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
                new_file=False,
                deleted_file=False,
                renamed_file=False,
            ),
            ChangedFileContext(
                file_path="include/new_header.h",
                old_path="include/new_header.h",
                diff="@@ -0,0 +1 @@\n+int x;\n",
                new_file=True,
                deleted_file=False,
                renamed_file=False,
            ),
        ],
    )

    message = orchestrator._lane_message(context, "regression")

    assert "预构建 Diff 包" in message
    assert "src/router.cpp" in message
    assert "src/other.cpp" in message
    assert "权威 MR scope 快照" in message
    assert "src/router.cpp (modified)" in message
    assert "include/new_header.h (new)" in message


def test_route_review_profile_prefers_deep_for_non_doc_code_changes():
    context = _make_context(
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
            )
        ]
    )

    assert orchestrator._route_review_profile(context) == "deep"


def test_risk_signals_cover_non_cpp_sources_and_manifests():
    context = _make_context(
        changed_files=[
            ChangedFileContext(
                file_path="src/api/handler.ts",
                old_path="src/api/handler.ts",
                diff="@@ -1 +1 @@\n-old\n+new\n",
            ),
            ChangedFileContext(
                file_path="package.json",
                old_path="package.json",
                diff="@@ -1 +1 @@\n-old\n+new\n",
            ),
            ChangedFileContext(
                file_path="schemas/payment.graphql",
                old_path="schemas/payment.graphql",
                diff="@@ -1 +1 @@\n-old\n+new\n",
            ),
        ]
    )

    signals = orchestrator._risk_signals(context)

    assert "build_or_package_system" in signals
    assert "public_contract" in signals


def test_build_evidence_bundle_collects_compile_and_repo_map(monkeypatch):
    context = _make_context(
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
            )
        ]
    )

    monkeypatch.setattr(orchestrator, "_route_review_profile", lambda *_args, **_kwargs: "deep")
    seed = orchestrator.build_review_seed_context(context)

    assert seed == ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=["src/router.cpp"],
        commit_messages=[],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )


@pytest.mark.asyncio
async def test_run_auto_review_uses_staged_pipeline(monkeypatch):
    context = _make_context()
    seed = ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=[],
        commit_messages=[],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )
    director = ChiefReviewDecision(
        summary="summary",
        specialist_reports=[SpecialistReviewReport(lane="correctness", status="ok", summary="done")],
        confirmed_findings=[
            CandidateFinding(
                source_lane="correctness",
                file_path="src/router.cpp",
                line=14,
                category="regression",
                severity="high",
                confidence="high",
                summary="Rollback path removed",
                details="Rollback is no longer called on failure.",
                evidence=["failure branch now returns early"],
                dedupe_key="dup-9",
            )
        ],
        suspicious_findings=[],
        open_questions=[],
    )
    published = {}

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args: context)
    monkeypatch.setattr(orchestrator, "build_review_seed_context", lambda *_args, **_kwargs: seed)
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)

    monkeypatch.setattr(
        orchestrator,
        "_run_specialist_reviews",
        lambda *_args, **_kwargs: pytest.fail("director-driven review should replace orchestrator-owned specialist fan-out"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_chief_review",
        lambda *_args, **_kwargs: pytest.fail("director-driven review should replace chief-review follow-up"),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail("director-driven review should not build legacy evidence bundles"),
    )

    async def fake_run_review_director(*_args, **_kwargs):
        return director

    monkeypatch.setattr(orchestrator, "_run_review_director", fake_run_review_director, raising=False)
    monkeypatch.setattr(
        orchestrator,
        "_finalize_director_decision",
        lambda *_args: RankedReview(
            recommendation="建议重新修改",
            summary="summary",
            confirmed_findings=director.confirmed_findings,
            suspicious_findings=[],
            open_questions=[],
            inline_candidates=director.confirmed_findings,
        ),
        raising=False,
    )

    def fake_publish(ctx, final_ranked, final_lanes):
        published["ctx"] = ctx
        published["ranked"] = final_ranked
        published["lanes"] = final_lanes

    monkeypatch.setattr(orchestrator, "_publish_review", fake_publish)

    result = await orchestrator.run_auto_review(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
    )

    assert result == AutoReviewRunResult(
        status="published",
        review_run_id="run-123",
        review_mode="full",
        recommendation="建议重新修改",
        confirmed_findings_count=1,
        suspicious_findings_count=0,
        open_questions_count=0,
        inline_comments_count=1,
    )


@pytest.mark.asyncio
async def test_run_review_director_uses_canonical_agent_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    context = _make_context(repo_dir="/home/wenming/.cache/open-review-e2e/worktrees/review-mr")
    seed = ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=["src/router.cpp"],
        commit_messages=[],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )

    captured = {}

    class _FakeAgent:
        async def ainvoke(self, payload, config):
            captured["payload"] = payload
            captured["config"] = config
            return {"structured_response": ChiefReviewDecision(summary="ok")}

    harness = orchestrator.AutoReviewDirectorHarness(
        agent=_FakeAgent(),
        director_backend=SimpleNamespace(),
        specialist_backends={
            lane: SimpleNamespace(tool_error_count=0, semantic_failure_count=0, failure_reasons=[])
            for lane in ("correctness", "reliability", "contracts", "performance-build", "security")
        },
        shell_repo_dir="/workspace/worktrees/review-mr",
        file_tool_repo_dir="/workspace/worktrees/review-mr",
    )

    monkeypatch.setattr(orchestrator, "build_auto_review_director_harness", lambda **_kwargs: harness)

    result = await orchestrator._run_review_director(
        context,
        seed,
        sandbox=object(),
        runtime_run_id="runtime-run-1",
    )
    assert result.summary == "ok"
    assert captured["config"]["configurable"]["repo_dir"] == "/workspace/worktrees/review-mr"
    assert captured["config"]["run_name"] == "auto_review team/project!42 @head123 [123]"
    message = captured["payload"]["messages"][0]["content"]
    assert "/workspace/worktrees/review-mr" in message
    assert "文件工具根目录" in message
    assert "/home/wenming/.cache" not in message


@pytest.mark.asyncio
async def test_run_specialist_review_appends_raw_record(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    context = _make_context()

    fake_backend = SimpleNamespace(
        tool_error_count=0,
        semantic_failure_count=0,
        failure_reasons=[],
        shell_repo_dir="/tmp/repo",
        file_tool_repo_dir="/tmp/repo",
    )

    class _FakeAgent:
        async def ainvoke(self, payload, config):
            return {
                "messages": list(payload["messages"]),
                "structured_response": SimpleNamespace(
                    summary="checked",
                    checks_run=["review_scope"],
                    findings=[],
                ),
            }

    monkeypatch.setattr(orchestrator, "AutoReviewLaneBackend", lambda *_args, **_kwargs: fake_backend)
    monkeypatch.setattr(orchestrator, "build_auto_review_lane_agent", lambda *_args, **_kwargs: _FakeAgent())

    result = await orchestrator._run_specialist_review(
        context,
        EvidenceBundle(review_profile="deep"),
        lane="correctness",
        sandbox=object(),
        model_id=None,
        runtime_run_id="runtime-run-1",
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_run_auto_review_raises_termination_before_publish(monkeypatch):
    context = _make_context()
    director = ChiefReviewDecision(summary="ok")
    calls = {"termination_checks": 0}

    async def fake_raise_if_run_termination_requested(**_kwargs):
        calls["termination_checks"] += 1
        if calls["termination_checks"] == 3:
            raise RunTerminationRequested(
                run_id="runtime-run-1",
                actor_key="team/project!42",
                reason="user_terminated",
            )

    async def fake_run_review_director(*_args, **_kwargs):
        return director

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        orchestrator,
        "build_review_seed_context",
        lambda *_args, **_kwargs: ReviewSeedContext(
            review_profile="deep",
            diff_range=context.diff_range,
            commit_range=context.commit_range,
            changed_files=[],
            commit_messages=[],
            recent_human_comments=[],
            previous_bot_comment_summaries=[],
        ),
    )
    monkeypatch.setattr(orchestrator, "_run_review_director", fake_run_review_director)
    monkeypatch.setattr(
        orchestrator,
        "_finalize_director_decision",
        lambda *_args, **_kwargs: RankedReview(summary="ok"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_publish_review",
        lambda *_args, **_kwargs: pytest.fail("terminated auto review runs must not publish"),
    )
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "raise_if_run_termination_requested", fake_raise_if_run_termination_requested)

    with pytest.raises(RunTerminationRequested, match="user_terminated"):
        await orchestrator.run_auto_review(
            project_id="team/project",
            mr_iid=42,
            repo_dir="/tmp/repo",
            sandbox=object(),
            runtime_run_id="runtime-run-1",
        )


@pytest.mark.asyncio
async def test_run_review_director_message_contains_dynamic_context_only(monkeypatch):
    context = _make_context(
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -1 +1 @@\n-int x;\n+int x = 1;\n",
                new_file=False,
                deleted_file=False,
                renamed_file=False,
            ),
            ChangedFileContext(
                file_path="include/new_header.h",
                old_path="include/new_header.h",
                diff="@@ -0,0 +1 @@\n+int x;\n",
                new_file=True,
                deleted_file=False,
                renamed_file=False,
            ),
        ]
    )
    seed = ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=["src/router.cpp"],
        commit_messages=["fix: router handling"],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )

    captured = {}

    class _FakeAgent:
        async def ainvoke(self, payload, config):
            captured["payload"] = payload
            captured["config"] = config
            return {"structured_response": ChiefReviewDecision(summary="ok")}

    harness = orchestrator.AutoReviewDirectorHarness(
        agent=_FakeAgent(),
        director_backend=SimpleNamespace(),
        specialist_backends={
            lane: SimpleNamespace(tool_error_count=0, semantic_failure_count=0, failure_reasons=[])
            for lane in ("correctness", "reliability", "contracts", "performance-build", "security")
        },
        shell_repo_dir="/workspace/worktrees/review-mr",
        file_tool_repo_dir="/workspace/worktrees/review-mr",
    )

    monkeypatch.setattr(orchestrator, "build_auto_review_director_harness", lambda **_kwargs: harness)

    await orchestrator._run_review_director(context, seed, sandbox=object())

    message = captured["payload"]["messages"][0]["content"]
    assert "本次 Merge Request 自动审查上下文" in message
    assert "git -C /workspace/worktrees/review-mr diff --unified=3 --find-renames origin/main...HEAD" in message
    assert "权威 MR scope 快照" in message
    assert "src/router.cpp (modified)" in message
    assert "include/new_header.h (new)" in message
    assert "packed-refs" not in message
    assert "worktree HEAD" not in message
    assert "specialist subagents" not in message


def test_deterministic_director_decision_uses_candidate_findings():
    finding = CandidateFinding(
        source_lane="security",
        file_path="src/router.cpp",
        line=19,
        category="security",
        severity="high",
        confidence="high",
        summary="Null check missing",
        details="A null pointer can be dereferenced.",
        evidence=["caller passes nullable pointer"],
        dedupe_key="dup-1",
    )
    reports = [
        SpecialistReviewReport(
            lane="security",
            summary="调查完成",
            candidate_findings=[finding],
        )
    ]

    decision = orchestrator._deterministic_director_decision(_make_context(), reports)

    assert decision.recommendation == "建议重新修改"
    assert [item.dedupe_key for item in decision.confirmed_findings] == ["dup-1"]
    assert decision.suspicious_findings == []


def test_finalize_director_decision_defaults_to_merge_when_no_confirmed_findings():
    ranked = orchestrator._finalize_director_decision(
        _make_context(),
        ChiefReviewDecision(
            summary="未发现明确问题。",
            confirmed_findings=[],
            suspicious_findings=[],
            open_questions=[],
        ),
    )

    assert ranked.recommendation == "建议合并"


def test_finalize_director_decision_preserves_agent_recommendation_with_confirmed_findings():
    ranked = orchestrator._finalize_director_decision(
        _make_context(),
        ChiefReviewDecision(
            recommendation="建议合并",
            summary="问题存在但不阻碍合并。",
            confirmed_findings=[
                CandidateFinding(
                    source_lane="reliability",
                    file_path="src/router.cpp",
                    line=19,
                    category="reliability",
                    severity="medium",
                    confidence="high",
                    summary="pre-existing flaky test risk",
                    details="该问题不是本次 MR 引入，且不阻碍合并。",
                    evidence=["历史测试已有相同模式"],
                    dedupe_key="dup-preserve-recommendation",
                )
            ],
            suspicious_findings=[],
            open_questions=[],
        ),
    )

    assert ranked.recommendation == "建议合并"


def test_finalize_director_decision_skips_previous_bot_duplicates():
    context = _make_context(
        previous_bot_dedupe_keys=["dup-existing"],
        changed_files=[
            ChangedFileContext(
                file_path="src/router.cpp",
                old_path="src/router.cpp",
                diff="@@ -1 +1 @@\n-old\n+new\n",
                added_lines=[1],
            )
        ],
    )
    ranked = orchestrator._finalize_director_decision(
        context,
        ChiefReviewDecision(
            recommendation="建议重新修改",
            summary="仍有问题。",
            confirmed_findings=[
                CandidateFinding(
                    source_lane="correctness",
                    file_path="src/router.cpp",
                    line=1,
                    category="regression",
                    severity="high",
                    confidence="high",
                    summary="Already reported regression",
                    details="This exact issue was already reported by a previous bot comment.",
                    evidence=["same diff evidence"],
                    dedupe_key="dup-existing",
                ),
                CandidateFinding(
                    source_lane="correctness",
                    file_path="src/router.cpp",
                    line=1,
                    category="regression",
                    severity="high",
                    confidence="high",
                    summary="New regression",
                    details="This is a new issue.",
                    evidence=["new diff evidence"],
                    dedupe_key="dup-new",
                ),
            ],
            suspicious_findings=[],
            open_questions=[],
        ),
    )

    assert [item.dedupe_key for item in ranked.confirmed_findings] == ["dup-new"]
    assert [item.dedupe_key for item in ranked.inline_candidates] == ["dup-new"]


@pytest.mark.asyncio
async def test_run_auto_review_does_not_expose_degraded_lanes(monkeypatch):
    context = _make_context()
    seed = ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=[],
        commit_messages=[],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )
    specialist_reports = [
        SpecialistReviewReport(
            lane="contracts",
            status="degraded",
            tool_error_count=1,
            semantic_failure_count=1,
            degraded_reason="read:file_not_found",
        )
    ]
    director = ChiefReviewDecision(summary="summary", specialist_reports=specialist_reports)
    ranked = RankedReview(recommendation="建议合并", summary="summary")

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args: context)
    monkeypatch.setattr(orchestrator, "build_review_seed_context", lambda *_args, **_kwargs: seed)
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_publish_review", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        orchestrator,
        "_run_specialist_reviews",
        lambda *_args, **_kwargs: pytest.fail("legacy orchestrator-owned specialists should not run"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_chief_review",
        lambda *_args, **_kwargs: pytest.fail("legacy chief-review step should not run"),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail("legacy evidence bundle should not run"),
    )

    async def fake_run_review_director(*_args, **_kwargs):
        return director

    monkeypatch.setattr(orchestrator, "_run_review_director", fake_run_review_director, raising=False)
    monkeypatch.setattr(orchestrator, "_finalize_director_decision", lambda *_args: ranked, raising=False)

    result = await orchestrator.run_auto_review(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
    )

    assert result.status == "published"
    assert not hasattr(result, "failed_lanes")


@pytest.mark.asyncio
async def test_run_auto_review_fails_closed_when_director_decision_missing(monkeypatch):
    context = _make_context()
    seed = ReviewSeedContext(
        review_profile="deep",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        changed_files=[],
        commit_messages=[],
        recent_human_comments=[],
        previous_bot_comment_summaries=[],
    )

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args: context)
    monkeypatch.setattr(orchestrator, "build_review_seed_context", lambda *_args, **_kwargs: seed)
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)

    published = {"called": False}

    async def fake_run_review_director(*_args, **_kwargs):
        raise orchestrator.DirectorReviewFailure("missing structured_response")

    async def fake_publish(*_args, **_kwargs):
        published["called"] = True

    monkeypatch.setattr(orchestrator, "_run_review_director", fake_run_review_director, raising=False)
    monkeypatch.setattr(orchestrator, "_publish_review", fake_publish)

    result = await orchestrator.run_auto_review(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
    )

    assert result.status == "failed"
    assert result.reason == "missing structured_response"
    assert published["called"] is False


@pytest.mark.asyncio
async def test_run_auto_review_skips_stale_expected_head(monkeypatch):
    context = _make_context(head_sha="head-new")

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args: context)

    result = await orchestrator.run_auto_review(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
        expected_head_sha="head-old",
    )

    assert result.status == "skipped"
    assert result.reason == "stale_webhook_head_sha"


@pytest.mark.asyncio
async def test_run_auto_review_fails_when_gitlab_identity_is_unavailable(monkeypatch):
    context = _make_context()

    monkeypatch.setattr(orchestrator, "build_review_context", lambda *_args: context)
    monkeypatch.setattr(
        orchestrator,
        "resolve_bot_identity",
        lambda **_kwargs: SimpleNamespace(identity=None, source="unavailable", error="GitLab unavailable", fetched_at=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_review_director",
        lambda *_args, **_kwargs: pytest.fail("unavailable identity should block review execution before director runs"),
    )

    result = await orchestrator.run_auto_review(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
    )

    assert result.status == "failed"
    assert "GitLab unavailable" in (result.reason or "")


@pytest.mark.asyncio
async def test_publish_review_upserts_persistent_summary(monkeypatch):
    context = _make_context()
    ranked = RankedReview(
        summary="summary",
        confirmed_findings=[
            CandidateFinding(
                source_lane="regression",
                file_path="src/router.cpp",
                line=14,
                category="regression",
                severity="high",
                confidence="high",
                summary="Rollback path removed",
                details="Rollback is no longer called on failure.",
                evidence=["failure branch now returns early"],
                dedupe_key="dup-9",
            )
        ],
        suspicious_findings=[],
        open_questions=[],
        inline_candidates=[
            CandidateFinding(
                source_lane="regression",
                file_path="src/router.cpp",
                line=14,
                category="regression",
                severity="high",
                confidence="high",
                summary="Rollback path removed",
                details="Rollback is no longer called on failure.",
                evidence=["failure branch now returns early"],
                dedupe_key="dup-9",
            )
        ],
    )
    calls = {}

    monkeypatch.setattr(
        orchestrator,
        "post_inline_comment",
        lambda *args: calls.setdefault("inline", []).append(args),
    )

    def fake_upsert(project_id, mr_iid, body, *, marker_name, marker_value):
        calls["summary"] = (project_id, mr_iid, body, marker_name, marker_value)
        return 91

    monkeypatch.setattr(orchestrator, "upsert_mr_comment_by_marker", fake_upsert)

    await orchestrator._publish_review(context, ranked, [])

    assert len(calls["inline"]) == 1
    assert calls["summary"][0:2] == ("team/project", 42)
    assert calls["summary"][3:] == ("open-review-summary-kind", "auto-review")
