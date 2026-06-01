"""Tests for the mention orchestrator."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.gitlab.comments import MRCommentRecord
from agent.runtime.termination import RunTerminationRequested
from agent.scenes.mention import orchestrator
from agent.scenes.mention.models import (
    MentionAgentResponse,
    MentionExecutionResult,
    MentionInlineSnippet,
    MentionReviewVerdict,
)
from agent.scenes.mention.prompts import build_mention_agent_prompt


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


def test_build_mention_context_includes_full_discussion_and_mr_snapshot(monkeypatch):
    metadata = SimpleNamespace(
        title="Explain the router change",
        description="MR description",
        source_branch="feature/router",
        target_branch="main",
        author="dev",
        url="http://gitlab/team/project/-/merge_requests/42",
        base_sha="base123",
        start_sha="start123",
        head_sha="head123",
    )
    activity = [
        MRCommentRecord(
            note_id=10,
            discussion_id="disc-1",
            author="developer",
            body="Please explain the rollback change",
            created_at="2026-04-09T00:00:00Z",
            file_path="src/router.cpp",
            line=41,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=11,
            discussion_id="disc-1",
            author="open-review-bot",
            body="Previous bot reply",
            created_at="2026-04-09T00:01:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=12,
            discussion_id="disc-1",
            author="developer",
            body="@open-review-bot can you fix it now?",
            created_at="2026-04-09T00:02:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=90,
            discussion_id="disc-other",
            author="reviewer",
            body="Unrelated discussion",
            created_at="2026-04-09T00:03:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
    ]

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: activity)
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_git_current_diff", lambda *_args: "diff --git a/src/router.cpp b/src/router.cpp\n")
    monkeypatch.setattr(
        orchestrator,
        "_collect_changed_files",
        lambda *_args: [
            {
                "file_path": "src/router.cpp",
                "old_path": "src/router.cpp",
                "diff": "@@ -1 +1 @@",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "added_lines": [41],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "_collect_commit_messages", lambda *_args: ["fix: keep rollback path"])

    context = orchestrator.build_mention_context(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        note_id=12,
        discussion_id="disc-1",
        note_body="@open-review-bot can you fix it now?",
        note_author="developer",
    )

    assert [item.note_id for item in context.discussion_messages] == [10, 11, 12]
    assert context.trigger_note.note_id == 12
    assert context.reply_target == "discussion"
    assert context.mr_snapshot.head_sha == "head123"
    assert context.mr_snapshot.diff_text.startswith("diff --git")
    assert context.mr_snapshot.changed_files[0].file_path == "src/router.cpp"
    assert context.mr_snapshot.commit_messages == ["fix: keep rollback path"]


def test_commit_and_push_uses_host_git_helpers(monkeypatch):
    calls = []
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")

    monkeypatch.setattr(
        orchestrator,
        "commit_all_and_get_sha",
        lambda *, worktree_dir, message, sandbox=None: calls.append(
            ("commit", worktree_dir, message, sandbox)
        )
        or "deadbeef",
    )
    monkeypatch.setattr(
        orchestrator,
        "push_branch_head",
        lambda *, project_id, worktree_dir, source_branch, sandbox=None: calls.append(
            ("push", project_id, worktree_dir, source_branch, sandbox)
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_sandbox_command",
        lambda *_args, **_kwargs: pytest.fail("sandbox-side git network operations should not run"),
    )

    commit_sha = orchestrator._commit_and_push(
        sandbox,
        "team/project",
        "/tmp/sandbox/worktrees/run-123",
        "feature/router",
    )

    assert commit_sha == "deadbeef"
    assert calls == [
        (
            "commit",
            "/tmp/sandbox/worktrees/run-123",
            "fix: address mention request",
            sandbox,
        ),
        (
            "push",
            "team/project",
            "/tmp/sandbox/worktrees/run-123",
            "feature/router",
            sandbox,
        ),
    ]


def test_build_mention_context_infers_discussion_id_from_trigger_note(monkeypatch):
    metadata = SimpleNamespace(
        title="Explain the router change",
        description="MR description",
        source_branch="feature/router",
        target_branch="main",
        author="dev",
        url="http://gitlab/team/project/-/merge_requests/42",
        base_sha="base123",
        start_sha="start123",
        head_sha="head123",
    )
    activity = [
        MRCommentRecord(
            note_id=10,
            discussion_id="disc-1",
            author="developer",
            body="Please explain the rollback change",
            created_at="2026-04-09T00:00:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=12,
            discussion_id="disc-1",
            author="developer",
            body="@open-review-bot can you fix it now?",
            created_at="2026-04-09T00:02:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
    ]

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: activity)
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_git_current_diff", lambda *_args: "")
    monkeypatch.setattr(orchestrator, "_collect_commit_messages", lambda *_args: [])

    context = orchestrator.build_mention_context(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        note_id=12,
        discussion_id=None,
        note_body="@open-review-bot can you fix it now?",
        note_author="developer",
    )

    assert context.discussion_id == "disc-1"
    assert context.reply_target == "discussion"
    assert [item.note_id for item in context.discussion_messages] == [10, 12]


def test_build_mention_context_tracks_covered_note_ids(monkeypatch):
    metadata = SimpleNamespace(
        title="Explain the router change",
        description="MR description",
        source_branch="feature/router",
        target_branch="main",
        author="dev",
        url="http://gitlab/team/project/-/merge_requests/42",
        base_sha="base123",
        start_sha="start123",
        head_sha="head123",
    )
    activity = [
        MRCommentRecord(
            note_id=10,
            discussion_id="disc-1",
            author="developer",
            body="Please explain the rollback change",
            created_at="2026-04-09T00:00:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=12,
            discussion_id="disc-1",
            author="developer",
            body="@open-review-bot can you fix it now?",
            created_at="2026-04-09T00:02:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
        MRCommentRecord(
            note_id=13,
            discussion_id="disc-1",
            author="developer",
            body="@open-review-bot and please add a test",
            created_at="2026-04-09T00:03:00Z",
            file_path=None,
            line=None,
            is_system=False,
            kind="discussion",
        ),
    ]

    monkeypatch.setattr(orchestrator, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(orchestrator, "list_mr_activity", lambda *_args: activity)
    monkeypatch.setattr(orchestrator, "_ensure_review_refs", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_git_current_diff", lambda *_args: "")
    monkeypatch.setattr(orchestrator, "_collect_commit_messages", lambda *_args: [])

    context = orchestrator.build_mention_context(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        note_id=13,
        discussion_id="disc-1",
        note_body="@open-review-bot and please add a test",
        note_author="developer",
        batched_events=[
            {"note_id": 12, "discussion_id": "disc-1", "note_body": "can you fix it now?", "note_author": "developer"},
            {"note_id": 13, "discussion_id": "disc-1", "note_body": "and please add a test", "note_author": "developer"},
        ],
    )

    assert context.covered_note_ids == [12, 13]
    assert [item.note_id for item in context.batched_notes] == [12, 13]


@pytest.mark.asyncio
async def test_publish_mention_result_replies_in_discussion(monkeypatch):
    result = MentionExecutionResult(
        intent="reply",
        status="replied",
        reply_markdown="Here is the explanation.",
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(head_sha="head123"),
    )
    called = {}

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)
    monkeypatch.setattr(orchestrator, "post_mr_comment", lambda *_args, **_kwargs: pytest.fail("fallback comment should not be used"))

    await orchestrator._publish_mention_result(context, result)

    assert called["reply"][:3] == ("team/project", 42, "disc-1")
    assert "<!-- open-review-mention-note: 12 -->" in called["reply"][3]
    assert "<!-- open-review-covered-notes: 12 -->" in called["reply"][3]
    assert "<!-- open-review-head-sha: head123 -->" in called["reply"][3]


@pytest.mark.asyncio
async def test_publish_mention_result_includes_all_covered_note_ids(monkeypatch):
    result = MentionExecutionResult(
        intent="reply",
        status="replied",
        reply_markdown="Here is the explanation.",
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12, 13],
        trigger_note=SimpleNamespace(note_id=13),
        mr_snapshot=SimpleNamespace(head_sha="head123"),
    )
    called = {}

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)
    monkeypatch.setattr(orchestrator, "post_mr_comment", lambda *_args, **_kwargs: pytest.fail("fallback comment should not be used"))

    await orchestrator._publish_mention_result(context, result)

    assert "<!-- open-review-covered-notes: 12,13 -->" in called["reply"][3]
    assert "本次回复覆盖 note：#12, #13。" in called["reply"][3]


@pytest.mark.asyncio
async def test_publish_mention_result_falls_back_when_discussion_reply_errors(monkeypatch, caplog):
    result = MentionExecutionResult(
        intent="reply",
        status="replied",
        reply_markdown="Here is the explanation.",
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(head_sha="head123"),
    )
    called = {}

    def fake_reply(*_args, **_kwargs):
        raise RuntimeError("discussion reply failed")

    def fake_comment(project_id, mr_iid, body):
        called["comment"] = (project_id, mr_iid, body)
        return 91

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)
    monkeypatch.setattr(orchestrator, "post_mr_comment", fake_comment)

    with caplog.at_level(logging.WARNING):
        await orchestrator._publish_mention_result(context, result)

    assert called["comment"][:2] == ("team/project", 42)
    assert "<!-- open-review-mention-note: 12 -->" in called["comment"][2]
    assert "falling back to MR note" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_publish_mention_result_falls_back_to_top_level_comment(monkeypatch):
    result = MentionExecutionResult(
        intent="follow_up",
        status="replied",
        reply_markdown="Can you narrow the request?",
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id=None,
        trigger_note=SimpleNamespace(note_id=15),
        mr_snapshot=SimpleNamespace(head_sha="head999"),
    )
    called = {}

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", lambda *_args, **_kwargs: pytest.fail("discussion reply should not be used"))

    def fake_comment(project_id, mr_iid, body):
        called["comment"] = (project_id, mr_iid, body)
        return 91

    monkeypatch.setattr(orchestrator, "post_mr_comment", fake_comment)

    await orchestrator._publish_mention_result(context, result)

    assert called["comment"][:2] == ("team/project", 42)
    assert "<!-- open-review-mention-note: 15 -->" in called["comment"][2]


@pytest.mark.asyncio
async def test_publish_mention_result_keeps_single_reply_when_no_inline_snippets(monkeypatch):
    result = MentionExecutionResult(
        intent="analysis",
        status="replied",
        reply_markdown="Single reply only.",
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(head_sha="head123"),
    )
    called = {}

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)

    await orchestrator._publish_mention_result(context, result)

    assert called["reply"][:3] == ("team/project", 42, "disc-1")


@pytest.mark.asyncio
async def test_publish_mention_result_publishes_structured_inline_snippet(monkeypatch):
    result = MentionExecutionResult(
        intent="analysis",
        status="replied",
        reply_markdown="请先看这段关键代码。",
        inline_snippets=[
            MentionInlineSnippet(
                path="src/router.cpp",
                line=14,
                side="new",
                lang="cpp",
                code="if (!ok) {\n    rollback();\n}",
            )
        ],
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(
            head_sha="head123",
            changed_files=[
                orchestrator.ChangedFileContext(
                    file_path="src/router.cpp",
                    old_path="src/router.cpp",
                    diff="@@ -10,0 +14,3 @@\n+if (!ok) {\n+    rollback();\n+}\n",
                    added_lines=[14, 15, 16],
                )
            ],
        ),
    )
    called = {}

    def fake_diff_discussion(project_id, mr_iid, body, **kwargs):
        called["inline"] = (project_id, mr_iid, body, kwargs)
        return 123

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "post_diff_discussion", fake_diff_discussion)
    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)

    await orchestrator._publish_mention_result(context, result)

    assert called["inline"][0:2] == ("team/project", 42)
    assert "```cpp" in called["inline"][2]
    assert "rollback();" in called["inline"][2]
    assert called["inline"][3]["new_path"] == "src/router.cpp"
    assert called["inline"][3]["old_path"] == "src/router.cpp"
    assert called["inline"][3]["new_line"] == 14
    assert called["inline"][3]["old_line"] is None
    assert called["inline"][3]["fallback_to_note"] is False
    assert "请先看这段关键代码。" in called["reply"][3]
    assert "src/router.cpp:14" not in called["reply"][3]
    assert "```cpp" not in called["reply"][3]


@pytest.mark.asyncio
async def test_publish_mention_result_falls_back_to_reply_block_when_snippet_not_inline_eligible(monkeypatch):
    result = MentionExecutionResult(
        intent="analysis",
        status="replied",
        reply_markdown="请看这里。",
        inline_snippets=[
            MentionInlineSnippet(
                path="src/router.cpp",
                line=22,
                side="new",
                lang="cpp",
                code="return retry_count;",
            )
        ],
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(
            head_sha="head123",
            changed_files=[
                orchestrator.ChangedFileContext(
                    file_path="src/router.cpp",
                    old_path="src/router.cpp",
                    diff="@@ -10,0 +14,3 @@\n+if (!ok) {\n+    rollback();\n+}\n",
                    added_lines=[14, 15, 16],
                )
            ],
        ),
    )
    called = {}

    monkeypatch.setattr(
        orchestrator,
        "post_diff_discussion",
        lambda *_args, **_kwargs: pytest.fail("inline publish should not run"),
    )

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)

    await orchestrator._publish_mention_result(context, result)

    assert "请看这里。" in called["reply"][3]
    assert "`src/router.cpp:22`" in called["reply"][3]
    assert "```cpp" in called["reply"][3]
    assert "return retry_count;" in called["reply"][3]


@pytest.mark.asyncio
async def test_publish_mention_result_supports_deleted_line_inline_snippet(monkeypatch):
    result = MentionExecutionResult(
        intent="analysis",
        status="replied",
        reply_markdown="这段被删掉的逻辑值得注意。",
        inline_snippets=[
            MentionInlineSnippet(
                path="src/router.cpp",
                line=9,
                side="old",
                lang="cpp",
                code="legacy_retry();",
            )
        ],
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(
            head_sha="head123",
            changed_files=[
                orchestrator.ChangedFileContext(
                    file_path="src/router.cpp",
                    old_path="src/router.cpp",
                    diff="@@ -9,1 +9,0 @@\n-legacy_retry();\n",
                    added_lines=[],
                )
            ],
        ),
    )
    called = {}

    def fake_diff_discussion(project_id, mr_iid, body, **kwargs):
        called["inline"] = (project_id, mr_iid, body, kwargs)
        return 321

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "post_diff_discussion", fake_diff_discussion)
    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)

    await orchestrator._publish_mention_result(context, result)

    assert called["inline"][3]["new_line"] is None
    assert called["inline"][3]["old_line"] == 9
    assert "legacy_retry();" in called["inline"][2]
    assert "legacy_retry();" not in called["reply"][3]


@pytest.mark.asyncio
async def test_publish_mention_result_supports_unchanged_context_line_inline_snippet(monkeypatch):
    result = MentionExecutionResult(
        intent="analysis",
        status="replied",
        reply_markdown="这段上下文逻辑也需要看。",
        inline_snippets=[
            MentionInlineSnippet(
                path="src/router.cpp",
                line=10,
                side="unchanged",
                lang="cpp",
                code="keep_two();",
            )
        ],
    )
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        discussion_id="disc-1",
        covered_note_ids=[12],
        trigger_note=SimpleNamespace(note_id=12),
        mr_snapshot=SimpleNamespace(
            head_sha="head123",
            changed_files=[
                orchestrator.ChangedFileContext(
                    file_path="src/router.cpp",
                    old_path="src/router.cpp",
                    diff=(
                        "@@ -8,3 +8,4 @@\n"
                        " keep_one();\n"
                        "-legacy_retry();\n"
                        "+retry_once();\n"
                        " keep_two();\n"
                        "+new_guard();\n"
                    ),
                    added_lines=[9, 11],
                )
            ],
        ),
    )
    called = {}

    def fake_diff_discussion(project_id, mr_iid, body, **kwargs):
        called["inline"] = (project_id, mr_iid, body, kwargs)
        return 654

    def fake_reply(project_id, mr_iid, discussion_id, body):
        called["reply"] = (project_id, mr_iid, discussion_id, body)
        return 77

    monkeypatch.setattr(orchestrator, "post_diff_discussion", fake_diff_discussion)
    monkeypatch.setattr(orchestrator, "reply_to_mr_discussion", fake_reply)

    await orchestrator._publish_mention_result(context, result)

    assert called["inline"][3]["new_line"] == 10
    assert called["inline"][3]["old_line"] == 10
    assert "keep_two();" in called["inline"][2]
    assert "keep_two();" not in called["reply"][3]


@pytest.mark.asyncio
async def test_run_mention_skips_duplicate_without_publishing(monkeypatch):
    context = SimpleNamespace(skip_reason="note_already_processed")
    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "_publish_mention_result", lambda *_args, **_kwargs: pytest.fail("duplicate mentions should not be published"))

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=object(),
        note_id=12,
        discussion_id="disc-1",
        note_body="help",
        note_author="developer",
    )

    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_run_mention_publishes_failure_when_worktree_creation_fails(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "explain this change",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "explain this change"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    called = {}

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "can_push_to_branch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cannot create worktree")))

    def fake_publish(_context, result):
        called["result"] = result

    monkeypatch.setattr(orchestrator, "_publish_mention_result", fake_publish)

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="explain this change",
        note_author="developer",
    )

    assert result.status == "failed"
    assert "cannot create worktree" in result.reply_markdown
    assert called["result"].status == "failed"


@pytest.mark.asyncio
async def test_run_mention_skips_stale_expected_head_before_execution(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "explain this change",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "explain this change"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head-new",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: pytest.fail("stale heads should not create worktrees"))

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="explain this change",
        note_author="developer",
        expected_head_sha="head-old",
    )

    assert result.status == "skipped"
    assert result.degraded_reason == "stale_webhook_head_sha"


@pytest.mark.asyncio
async def test_run_mention_fails_when_gitlab_identity_is_unavailable(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please explain this change",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please explain this change"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(
        orchestrator,
        "resolve_bot_identity",
        lambda **_kwargs: SimpleNamespace(identity=None, source="unavailable", error="GitLab unavailable", fetched_at=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "create_temporary_worktree",
        lambda *_args, **_kwargs: pytest.fail("unavailable identity should block mention execution before worktree creation"),
    )

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="please explain this change",
        note_author="developer",
    )

    assert result.status == "failed"
    assert result.degraded_reason == "gitlab_bot_identity_unavailable"
    assert "当前无法解析 GitLab Bot 身份" in result.reply_markdown


@pytest.mark.asyncio
async def test_main_agent_validates_code_changes_outside_the_worktree(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please repair the router bug",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please repair the router bug"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")
    captured = {}

    class _Agent:
        async def ainvoke(self, payload, config):
            captured["payload"] = payload
            captured["config"] = config
            return {
                "messages": [
                    SimpleNamespace(
                        tool_calls=[{"name": "task", "args": {"subagent_type": "review", "description": "review the pending changes"}}]
                    )
                ],
                "structured_response": MentionAgentResponse(
                    reply_markdown="已修复问题。",
                    reply_kind="code_change",
                    used_subagents=["review"],
                ),
            }

    monkeypatch.setattr(orchestrator, "build_mention_agent", lambda **_kwargs: _Agent())
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["src/router.cpp"])
    monkeypatch.setattr(orchestrator, "_commit_and_push", lambda *_args, **_kwargs: "deadbeef")
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)

    result = await orchestrator._run_main_agent(
        context,
        sandbox,
        worktree_dir="/tmp/sandbox/worktrees/run-123",
    )

    assert result.status == "pushed"
    assert result.commit_sha == "deadbeef"
    assert result.used_subagents == ["review"]
    assert result.validation_result is None
    assert captured["config"]["configurable"]["repo_dir"] == "/tmp/sandbox/worktrees/run-123"
    assert captured["config"]["run_name"] == "mention-author team/project!42 note#12 @head123 [1]"
    assert "我已经推送改动到 `feature/router` 分支。" in result.reply_markdown
    assert "`deadbeef`" in result.reply_markdown


@pytest.mark.asyncio
async def test_run_mention_uses_main_agent(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please inspect the router bug",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please inspect the router bug"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    calls = {}

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)

    async def fake_run_loop(_context, _sandbox, *, worktree_dir):
        calls["worktree_dir"] = worktree_dir
        return MentionExecutionResult(
            intent="analysis",
            status="replied",
            reply_markdown="done",
            used_subagents=["dialogs"],
        )

    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/run-123")
    monkeypatch.setattr(orchestrator, "_run_author_reviewer_loop", fake_run_loop)
    monkeypatch.setattr(orchestrator, "_publish_mention_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="please inspect the router bug",
        note_author="developer",
    )

    assert calls["worktree_dir"] == "/tmp/sandbox/worktrees/run-123"
    assert result.used_subagents == ["dialogs"]


@pytest.mark.asyncio
async def test_run_mention_retries_author_until_reviewer_approves(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please explain the change",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please explain the change"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    author_payloads = []
    reviewer_payloads = []
    publish_calls = {}

    class _Author:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            author_payloads.append((payload, config))
            if self.calls == 1:
                return {
                    "messages": [SimpleNamespace(content="draft-1")],
                    "structured_response": MentionAgentResponse(
                        reply_markdown="第一版答复。",
                        reply_kind="analysis",
                        used_subagents=["dialogs"],
                    ),
                }
            return {
                "messages": [SimpleNamespace(content="draft-2")],
                "structured_response": MentionAgentResponse(
                    reply_markdown="第二版答复。",
                    reply_kind="analysis",
                    used_subagents=["dialogs", "repo-analyst"],
                ),
            }

    class _Reviewer:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            reviewer_payloads.append((payload, config))
            if self.calls == 1:
                return {
                    "messages": [SimpleNamespace(content="第一次审核：请补充边界条件说明。")],
                    "structured_response": MentionReviewVerdict(
                        approved=False,
                        feedback_markdown="请补充边界条件说明。",
                    ),
                }
            return {
                "messages": [SimpleNamespace(content="第二次审核：通过。")],
                "structured_response": MentionReviewVerdict(
                    approved=True,
                    feedback_markdown="通过。",
                ),
            }

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/run-123")
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "build_mention_author_agent", lambda **_kwargs: _Author())
    monkeypatch.setattr(orchestrator, "build_mention_reviewer_agent", lambda **_kwargs: _Reviewer())
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_publish_mention_result", lambda _context, result, **_kwargs: publish_calls.setdefault("result", result))

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="please explain the change",
        note_author="developer",
    )

    assert result.status == "replied"
    assert result.reply_markdown == "第二版答复。"
    assert result.review_approved is True
    assert result.review_rounds == 2
    assert publish_calls["result"].review_rounds == 2
    assert len(author_payloads) == 2
    assert len(reviewer_payloads) == 2
    second_author_messages = author_payloads[1][0]["messages"]
    assert any("请补充边界条件说明" in getattr(message, "content", "") for message in second_author_messages)


@pytest.mark.asyncio
async def test_run_mention_records_author_and_reviewer_round_observations(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    reset_controlplane_services()
    tracking = get_tracking_service()
    tracking.record_run(
        {
            "run_id": "runtime-run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "mention",
            "state": "running",
            "batch_size": 1,
            "started_at": "2026-04-20T10:00:00+08:00",
        }
    )
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please explain the change",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please explain the change"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    observations = []

    class _Author:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            return {
                "messages": list(payload["messages"]),
                "structured_response": MentionAgentResponse(
                    reply_markdown=f"第{self.calls}版答复。",
                    reply_kind="analysis",
                    used_subagents=["dialogs"],
                ),
            }

    class _Reviewer:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, payload, config):
            self.calls += 1
            if self.calls == 1:
                return {
                    "messages": list(payload["messages"]),
                    "structured_response": MentionReviewVerdict(
                        approved=False,
                        feedback_markdown="请补充边界条件说明。",
                    ),
                }
            return {
                "messages": list(payload["messages"]),
                "structured_response": MentionReviewVerdict(
                    approved=True,
                    feedback_markdown="通过。",
                ),
            }

    async def fake_record(summary, *, stage_key="scene_execute", details=None, status="running", event_type="observation"):
        observations.append((stage_key, event_type, status, summary, details))

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/run-123")
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "build_mention_author_agent", lambda **_kwargs: _Author())
    monkeypatch.setattr(orchestrator, "build_mention_reviewer_agent", lambda **_kwargs: _Reviewer())
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_publish_mention_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "record_runtime_observation", fake_record)

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="please explain the change",
        note_author="developer",
        runtime_run_id="runtime-run-1",
    )
    assert result.review_rounds == 2
    assert observations == [
        ("scene_execute", "observation", "running", "mention author round started", {"mention_role": "author", "mention_round": 1}),
        (
            "scene_execute",
            "observation",
            "running",
            "mention author round completed",
            {"mention_role": "author", "mention_round": 1, "reply_kind": "analysis", "used_subagents": ["dialogs"]},
        ),
        ("scene_execute", "observation", "running", "mention reviewer round started", {"mention_role": "reviewer", "mention_round": 1}),
        (
            "scene_execute",
            "observation",
            "running",
            "mention reviewer round completed",
            {"mention_role": "reviewer", "mention_round": 1, "approved": False},
        ),
        ("scene_execute", "observation", "running", "mention author round started", {"mention_role": "author", "mention_round": 2}),
        (
            "scene_execute",
            "observation",
            "running",
            "mention author round completed",
            {"mention_role": "author", "mention_round": 2, "reply_kind": "analysis", "used_subagents": ["dialogs"]},
        ),
        ("scene_execute", "observation", "running", "mention reviewer round started", {"mention_role": "reviewer", "mention_round": 2}),
        (
            "scene_execute",
            "observation",
            "running",
            "mention reviewer round completed",
            {"mention_role": "reviewer", "mention_round": 2, "approved": True},
        ),
    ]


@pytest.mark.asyncio
async def test_run_mention_stops_after_ten_rejected_review_rounds_without_pushing_code(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please fix the router bug",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please fix the router bug"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    publish_calls = {}

    class _Author:
        async def ainvoke(self, payload, config):
            return {
                "messages": list(payload["messages"]),
                "structured_response": MentionAgentResponse(
                    reply_markdown="这是最后一版修复说明。",
                    reply_kind="code_change",
                    used_subagents=["dialogs"],
                ),
            }

    class _Reviewer:
        async def ainvoke(self, payload, config):
            return {
                "messages": list(payload["messages"]),
                "structured_response": MentionReviewVerdict(
                    approved=False,
                    feedback_markdown="这版改动仍然不能接受。",
                ),
            }

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/sandbox/worktrees/run-123")
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "build_mention_author_agent", lambda **_kwargs: _Author())
    monkeypatch.setattr(orchestrator, "build_mention_reviewer_agent", lambda **_kwargs: _Reviewer())
    monkeypatch.setattr(orchestrator, "_working_tree_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["src/router.cpp"])
    monkeypatch.setattr(orchestrator, "_commit_and_push", lambda *_args, **_kwargs: pytest.fail("unapproved code changes should not be pushed"))
    monkeypatch.setattr(orchestrator, "_publish_mention_result", lambda _context, result, **_kwargs: publish_calls.setdefault("result", result))

    result = await orchestrator.run_mention(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
        note_id=12,
        discussion_id="disc-1",
        note_body="please fix the router bug",
        note_author="developer",
    )

    assert result.status == "replied"
    assert result.review_approved is False
    assert result.review_rounds == 10
    assert result.commit_sha is None
    assert result.degraded_reason == "review_rejected_after_max_rounds"
    assert "没有推送" in result.reply_markdown
    assert publish_calls["result"].review_rounds == 10


@pytest.mark.asyncio
async def test_main_agent_pushes_code_changes_without_review_subagent(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "please fix the router bug",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "please fix the router bug"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")

    class _Agent:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "structured_response": MentionAgentResponse(
                    reply_markdown="已修改代码。",
                    reply_kind="code_change",
                    used_subagents=[],
                )
            }

    monkeypatch.setattr(orchestrator, "build_mention_agent", lambda **_kwargs: _Agent())
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["src/router.cpp"])
    monkeypatch.setattr(orchestrator, "_commit_and_push", lambda *_args, **_kwargs: "deadbeef")
    monkeypatch.setattr(orchestrator, "_source_branch_is_pushable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)

    result = await orchestrator._run_main_agent(
        context,
        sandbox,
        worktree_dir="/tmp/sandbox/worktrees/run-123",
    )

    assert result.status == "pushed"
    assert result.degraded_reason is None
    assert result.commit_sha == "deadbeef"
    assert "我已经推送改动到 `feature/router` 分支。" in result.reply_markdown


@pytest.mark.asyncio
async def test_main_agent_blocks_push_when_changed_file_limit_is_exceeded(monkeypatch):
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "fix the router bug",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "fix the router bug"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "diff --git a/src/router.cpp b/src/router.cpp\n",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )
    sandbox = SimpleNamespace(root_dir="/tmp/sandbox")

    class _Agent:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "messages": [
                    SimpleNamespace(
                        tool_calls=[{"name": "task", "args": {"subagent_type": "review", "description": "review the pending changes"}}]
                    )
                ],
                "structured_response": MentionAgentResponse(
                    reply_markdown="已完成修改。",
                    reply_kind="code_change",
                    used_subagents=["review"],
                ),
            }

    monkeypatch.setattr(orchestrator, "build_mention_agent", lambda **_kwargs: _Agent())
    monkeypatch.setattr(orchestrator, "_collect_changed_paths", lambda *_args, **_kwargs: ["a.cpp", "b.cpp", "c.cpp", "d.cpp"])
    monkeypatch.setattr(orchestrator, "_commit_and_push", lambda *_args, **_kwargs: "deadbeef")
    monkeypatch.setattr(orchestrator, "_head_is_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "_source_branch_is_pushable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(settings, "MENTION_MAX_CHANGED_FILES", 3)

    result = await orchestrator._run_main_agent(
        context,
        sandbox,
        worktree_dir="/tmp/sandbox/worktrees/run-123",
    )

    assert result.status == "replied"
    assert result.degraded_reason == "changed_file_limit_exceeded"
    assert result.commit_sha is None
    assert "超过本次允许修改的文件数上限" in result.reply_markdown


def test_extract_used_subagents_prefers_actual_task_tool_calls():
    payload = {
        "messages": [
            SimpleNamespace(tool_calls=[{"name": "task", "args": {"subagent_type": "dialogs"}}]),
            SimpleNamespace(tool_calls=[{"name": "task", "args": {"subagent_type": "review"}}]),
        ],
        "structured_response": MentionAgentResponse(
            reply_markdown="done",
            reply_kind="analysis",
            used_subagents=["repo-analyst"],
        ),
    }

    assert orchestrator._extract_used_subagents(payload) == ["dialogs", "review"]


def test_coerce_agent_response_strips_hidden_open_review_markers_from_reply_markdown():
    response = orchestrator._coerce_agent_response(
        {
            "structured_response": {
                "reply_markdown": "分析结果\n<!-- open-review-mention-run: old-run -->\n<!-- open-review-head-sha: old-head -->",
                "reply_kind": "analysis",
                "used_subagents": ["repo-analyst"],
            }
        }
    )

    assert response.reply_markdown == "分析结果"
    assert response.used_subagents == ["repo-analyst"]


def test_working_tree_has_changes_treats_no_output_placeholder_as_clean(monkeypatch):
    sandbox = SimpleNamespace()

    monkeypatch.setattr(
        orchestrator,
        "_sandbox_command",
        lambda *_args, **_kwargs: "<no output>",
    )

    assert orchestrator._working_tree_has_changes(sandbox, "/tmp/worktree") is False


def test_mention_orchestrator_no_longer_exposes_classifier_helpers():
    assert not hasattr(orchestrator, "_classify_intent")
    assert not hasattr(orchestrator, "_classify_intent_with_model")


def test_coerce_agent_response_returns_structured_reply():
    payload = {
        "structured_response": MentionAgentResponse(
            reply_markdown="done",
        )
    }

    response = orchestrator._coerce_agent_response(payload)

    assert response.reply_markdown == "done"


def test_coerce_agent_response_requires_structured_response():
    payload = {
        "messages": [
            SimpleNamespace(content=""),
            SimpleNamespace(content="## MR 变更核查报告\n\n两个文件都是 modified，不是新增文件。"),
        ]
    }

    with pytest.raises(RuntimeError, match="missing structured_response"):
        orchestrator._coerce_agent_response(payload)


def test_coerce_review_verdict_requires_structured_response():
    payload = {
        "messages": [
            SimpleNamespace(content="## 审核结论\n\n**批准**\n\n工作树 diff 未变，候选结果准确。无需修订。"),
        ]
    }

    with pytest.raises(RuntimeError, match="reviewer missing structured_response"):
        orchestrator._coerce_review_verdict(payload)


@pytest.mark.asyncio
async def test_run_mention_raises_termination_before_publish(monkeypatch):
    context = SimpleNamespace(
        project_id="team/project",
        mr_iid=42,
        note_id=12,
        note_body="please fix this",
        note_author="developer",
        covered_note_ids=[12],
        skip_reason=None,
        discussion_id="disc-1",
        mr_snapshot=SimpleNamespace(head_sha="head-123", source_branch="feature/router"),
        run_id="mention-run-1",
    )
    result = MentionExecutionResult(
        intent="reply",
        status="replied",
        reply_markdown="done",
        covered_note_ids=[12],
    )
    calls = {"termination_checks": 0}

    async def fake_raise_if_run_termination_requested(**_kwargs):
        calls["termination_checks"] += 1
        if calls["termination_checks"] == 3:
            raise RunTerminationRequested(
                run_id="runtime-run-1",
                actor_key="team/project!42",
                reason="user_terminated",
            )

    async def fake_loop(*_args, **_kwargs):
        return result

    monkeypatch.setattr(orchestrator, "build_mention_context", lambda **_kwargs: context)
    monkeypatch.setattr(orchestrator, "create_temporary_worktree", lambda *_args, **_kwargs: "/tmp/worktree")
    monkeypatch.setattr(orchestrator, "cleanup_temporary_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_run_author_reviewer_loop", fake_loop)
    monkeypatch.setattr(
        orchestrator,
        "_publish_mention_result",
        lambda *_args, **_kwargs: pytest.fail("terminated mention runs must not publish"),
    )
    monkeypatch.setattr(orchestrator, "raise_if_run_termination_requested", fake_raise_if_run_termination_requested)

    with pytest.raises(RunTerminationRequested, match="user_terminated"):
        await orchestrator.run_mention(
            project_id="team/project",
            mr_iid=42,
            repo_dir="/tmp/repo",
            sandbox=SimpleNamespace(root_dir="/tmp/sandbox"),
            runtime_run_id="runtime-run-1",
            note_id=12,
            discussion_id="disc-1",
            note_body="please fix this",
            note_author="developer",
        )


def test_mention_agent_prompt_requires_review_scope_before_stating_mr_facts():
    context = orchestrator.MentionContext.model_validate(
        {
            "project_id": "team/project",
            "mr_iid": 42,
            "note_id": 12,
            "discussion_id": "disc-1",
            "note_body": "what changed?",
            "note_author": "developer",
            "trigger_note": {"note_id": 12, "discussion_id": "disc-1", "author": "developer", "body": "what changed?"},
            "discussion_messages": [],
            "recent_mr_activity": [],
            "batched_notes": [],
            "covered_note_ids": [12],
            "reply_target": "discussion",
            "run_id": "run-123",
            "mr_snapshot": {
                "project_id": "team/project",
                "mr_iid": 42,
                "title": "Router fix",
                "description": "",
                "author": "developer",
                "url": "http://gitlab/team/project/-/merge_requests/42",
                "source_branch": "feature/router",
                "target_branch": "main",
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
                "repo_dir": "/tmp/repo",
                "diff_range": "origin/main...HEAD",
                "commit_range": "origin/main..HEAD",
                "diff_text": "",
                "changed_files": [],
                "commit_messages": [],
            },
        }
    )

    prompt = build_mention_agent_prompt("/tmp/repo", "/tmp/repo", context)

    assert "Before making factual claims about what this MR changes" in prompt
    assert "call `review_scope` at least once" in prompt
    assert "If `review_scope` and your own inspection disagree" in prompt
