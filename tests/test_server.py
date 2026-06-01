"""Tests for the LangGraph compatibility entry point."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import server


@pytest.mark.asyncio
async def test_get_agent_builds_auto_review_agent(monkeypatch):
    metadata = SimpleNamespace(source_branch="feature/refactor", target_branch="main")
    sandbox = object()
    built_agent = object()

    get_mr_metadata_calls = []
    generate_thread_id_calls = []
    setup_sandbox_mock = AsyncMock(return_value=(sandbox, "/tmp/sandbox/repo"))
    build_agent_calls = []

    def fake_get_mr_metadata(project_id, mr_iid):
        get_mr_metadata_calls.append((project_id, mr_iid))
        return metadata

    def fake_generate_thread_id(project_id, mr_iid):
        generate_thread_id_calls.append((project_id, mr_iid))
        return "generated-thread"

    def fake_build_auto_review_agent(**kwargs):
        build_agent_calls.append(kwargs)
        return built_agent

    monkeypatch.setattr(server, "get_mr_metadata", fake_get_mr_metadata)
    monkeypatch.setattr(server, "generate_thread_id", fake_generate_thread_id)
    monkeypatch.setattr(server, "setup_sandbox", setup_sandbox_mock)
    monkeypatch.setattr(server, "build_auto_review_agent", fake_build_auto_review_agent)

    result = await server.get_agent(
        {"configurable": {"project_id": "team/project", "mr_iid": 42, "model_id": "openai:test"}}
    )

    assert result is built_agent
    assert get_mr_metadata_calls == [("team/project", 42)]
    assert generate_thread_id_calls == [("team/project", 42)]
    setup_sandbox_mock.assert_awaited_once_with("generated-thread", "team/project", "feature/refactor")
    assert build_agent_calls == [
        {
            "sandbox": sandbox,
            "repo_dir": "/tmp/sandbox/repo",
            "source_branch": "feature/refactor",
            "target_branch": "main",
            "model_id": "openai:test",
        }
    ]


@pytest.mark.asyncio
async def test_get_agent_uses_provided_thread_id(monkeypatch):
    metadata = SimpleNamespace(source_branch="feature/refactor", target_branch="main")
    setup_sandbox_mock = AsyncMock(return_value=(object(), "/tmp/sandbox/repo"))

    monkeypatch.setattr(server, "get_mr_metadata", lambda *_args: metadata)
    monkeypatch.setattr(server, "setup_sandbox", setup_sandbox_mock)
    monkeypatch.setattr(server, "build_auto_review_agent", lambda **_kwargs: object())

    def fail_generate_thread_id(*_args):
        raise AssertionError("generate_thread_id should not be called when thread_id is provided")

    monkeypatch.setattr(server, "generate_thread_id", fail_generate_thread_id)

    await server.get_agent(
        {"configurable": {"project_id": "team/project", "mr_iid": 42, "thread_id": "known-thread"}}
    )

    setup_sandbox_mock.assert_awaited_once_with("known-thread", "team/project", "feature/refactor")


@pytest.mark.asyncio
async def test_get_agent_requires_project_and_mr():
    with pytest.raises(ValueError, match="project_id and configurable.mr_iid are required"):
        await server.get_agent({"configurable": {"project_id": "team/project"}})
