from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.config import settings
from agent.controlplane import get_tracking_service, reset_controlplane_services
from agent.scenes.auto_review import graph as auto_review_graph
from agent.scenes.auto_review.models import ReviewContext
from agent.scenes.mention import graph as mention_graph
from agent.scenes.mention.models import MRSnapshot, MentionContext, MentionThreadMessage


def _mention_context() -> MentionContext:
    return MentionContext(
        project_id="team/project",
        mr_iid=42,
        note_id=12,
        discussion_id="disc-1",
        note_body="please explain the change",
        note_author="developer",
        trigger_note=MentionThreadMessage(
            note_id=12,
            discussion_id="disc-1",
            author="developer",
            body="please explain the change",
        ),
        run_id="mention-run-1",
        mr_snapshot=MRSnapshot(
            project_id="team/project",
            mr_iid=42,
            title="Explain router change",
            source_branch="feature/router",
            target_branch="main",
            base_sha="base123",
            start_sha="start123",
            head_sha="head123",
            repo_dir="/tmp/repo",
            diff_range="origin/main...HEAD",
            commit_range="origin/main..HEAD",
            diff_text="diff --git a/src/router.cpp b/src/router.cpp\n",
        ),
    )


def _review_context() -> ReviewContext:
    return ReviewContext(
        project_id="team/project",
        mr_iid=42,
        title="Fix router regression",
        source_branch="feature/router-fix",
        target_branch="main",
        base_sha="base123",
        start_sha="start123",
        head_sha="head123",
        repo_dir="/tmp/repo",
        review_run_id="review-run-1",
        review_mode="full",
        diff_range="origin/main...HEAD",
        commit_range="origin/main..HEAD",
        diff_text="diff --git a/src/router.cpp b/src/router.cpp\n",
        diff_fingerprint="fp123",
    )


def test_build_mention_agents_install_raw_record_middleware(monkeypatch):
    captured: list[dict] = []
    context = _mention_context()

    monkeypatch.setattr(mention_graph, "make_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(mention_graph, "sandbox_file_tool_path", lambda *_args, **_kwargs: "/tmp/repo")
    monkeypatch.setattr(
        mention_graph,
        "build_mention_auxiliary_subagent",
        lambda *args, **kwargs: {"name": "dialogs", "description": "dialogs", "runnable": object()},
    )
    monkeypatch.setattr(
        mention_graph,
        "create_deep_agent",
        lambda **kwargs: captured.append(kwargs) or SimpleNamespace(),
    )

    mention_graph.build_mention_author_agent(
        sandbox=SimpleNamespace(),
        repo_dir="/tmp/repo",
        source_branch="feature/router",
        context=context,
        runtime_run_id="runtime-run-1",
    )
    mention_graph.build_mention_reviewer_agent(
        sandbox=SimpleNamespace(),
        repo_dir="/tmp/repo",
        context=context,
        runtime_run_id="runtime-run-1",
    )

    middleware_types = [[type(item).__name__ for item in call["middleware"]] for call in captured]
    assert any("MentionRawRecordMiddleware" in items for items in middleware_types)
    raw_middlewares = [
        item
        for call in captured
        for item in call["middleware"]
        if type(item).__name__ == "MentionRawRecordMiddleware"
    ]
    assert [item.mention_role for item in raw_middlewares] == ["author", "reviewer"]


@pytest.mark.asyncio
async def test_mention_raw_record_middleware_persists_round_metadata(monkeypatch, tmp_path):
    from agent.scenes.mention.middleware import MentionRawRecordMiddleware

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
            "started_at": "2026-04-22T10:00:00+08:00",
        }
    )
    middleware = MentionRawRecordMiddleware(
        context=_mention_context(),
        runtime_run_id="runtime-run-1",
        mention_role="author",
        system_prompt="system prompt",
    )
    input_messages = [{"role": "user", "content": "please explain the change"}]

    await middleware.abefore_agent(
        {"messages": input_messages},
        runtime=None,
        config={"configurable": {"thread_id": "mention:thread:author", "round_index": 2}},
    )
    await middleware.aafter_agent(
        {
            "messages": input_messages,
            "structured_response": {
                "reply_markdown": "analysis",
                "reply_kind": "analysis",
                "used_subagents": ["dialogs"],
            },
        },
        runtime=None,
    )

    run = tracking.list_recent_runs(limit=1)[0]
    record = run["agent_records"][0]
    assert record["record_kind"] == "mention.author"
    assert record["thread_id"] == "mention:thread:author"
    assert record["metadata_json"]["round_index"] == 2
    assert record["metadata_json"]["used_subagents"] == ["dialogs"]


@pytest.mark.asyncio
async def test_mention_raw_record_middleware_persists_reviewer_metadata(monkeypatch, tmp_path):
    from agent.scenes.mention.middleware import MentionRawRecordMiddleware

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
            "started_at": "2026-04-22T10:00:00+08:00",
        }
    )
    middleware = MentionRawRecordMiddleware(
        context=_mention_context(),
        runtime_run_id="runtime-run-1",
        mention_role="reviewer",
        system_prompt="reviewer prompt",
    )
    input_messages = [{"role": "user", "content": "please review"}]

    await middleware.abefore_agent(
        {"messages": input_messages},
        runtime=None,
        config={"configurable": {"thread_id": "mention:thread:reviewer", "round_index": 1}},
    )
    await middleware.aafter_agent(
        {
            "messages": input_messages,
            "structured_response": {
                "approved": True,
                "feedback_markdown": "ok",
            },
        },
        runtime=None,
    )

    run = tracking.list_recent_runs(limit=1)[0]
    record = run["agent_records"][0]
    assert record["record_kind"] == "mention.reviewer"
    assert record["thread_id"] == "mention:thread:reviewer"
    assert record["metadata_json"]["round_index"] == 1
    assert record["metadata_json"]["approved"] is True


def test_build_auto_review_agents_install_raw_record_middleware(monkeypatch):
    captured: list[dict] = []
    context = _review_context()

    monkeypatch.setattr(auto_review_graph, "make_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(auto_review_graph, "sandbox_shell_path", lambda *_args, **_kwargs: "/tmp/repo")
    monkeypatch.setattr(auto_review_graph, "sandbox_file_tool_path", lambda *_args, **_kwargs: "/tmp/repo")
    monkeypatch.setattr(
        auto_review_graph,
        "_build_auto_review_investigation_subagent",
        lambda *args, **kwargs: {"name": "explore", "description": "explore", "runnable": object()},
    )
    monkeypatch.setattr(
        auto_review_graph,
        "_build_git_inspector_agent",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        auto_review_graph,
        "_observed_compiled_subagent",
        lambda **kwargs: {"name": kwargs["name"], "description": kwargs["description"], "runnable": kwargs["runnable"]},
    )
    monkeypatch.setattr(
        auto_review_graph,
        "create_deep_agent",
        lambda **kwargs: captured.append(kwargs) or SimpleNamespace(),
    )

    auto_review_graph.build_auto_review_specialist_agent(
        sandbox=SimpleNamespace(),
        repo_dir="/tmp/repo",
        lane="correctness",
        review_context=context,
        runtime_run_id="runtime-run-1",
    )
    auto_review_graph.build_auto_review_director_harness(
        sandbox=SimpleNamespace(),
        repo_dir="/tmp/repo",
        review_context=context,
        runtime_run_id="runtime-run-1",
    )

    middleware_types = [[type(item).__name__ for item in call["middleware"]] for call in captured]
    assert any("AutoReviewRawRecordMiddleware" in items for items in middleware_types)


@pytest.mark.asyncio
async def test_auto_review_raw_record_middleware_persists_specialist_record(monkeypatch, tmp_path):
    from agent.scenes.auto_review.middleware import AutoReviewRawRecordMiddleware

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
            "started_at": "2026-04-22T10:00:00+08:00",
        }
    )
    middleware = AutoReviewRawRecordMiddleware(
        context=_review_context(),
        runtime_run_id="runtime-run-1",
        record_kind="auto_review.specialist.correctness",
        thread_id="auto_review:thread:specialist:correctness",
        system_prompt="system prompt",
        metadata={"lane": "correctness"},
    )
    input_messages = [{"role": "user", "content": "review the MR"}]

    await middleware.abefore_agent(
        {"messages": input_messages},
        runtime=None,
        config={"configurable": {}},
    )
    await middleware.aafter_agent(
        {
            "messages": input_messages,
            "structured_response": {
                "summary": "checked",
                "candidate_findings": [],
            },
        },
        runtime=None,
    )

    run = tracking.list_recent_runs(limit=1)[0]
    record = run["agent_records"][0]
    assert record["record_kind"] == "auto_review.specialist.correctness"
    assert record["thread_id"] == "auto_review:thread:specialist:correctness"
    assert record["metadata_json"]["lane"] == "correctness"
