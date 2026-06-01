"""Tests for mention webhook handling."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock

import pytest


@pytest.mark.asyncio
async def test_handle_mention_uses_orchestrator(monkeypatch):
    import agent.webapp as wa

    run_mention = AsyncMock()

    async def fake_setup_sandbox(*_args, **_kwargs):
        return object(), "/tmp/repo"

    monkeypatch.setattr("agent.gitlab.comments.add_eyes_reaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.sandbox.manager.setup_sandbox", fake_setup_sandbox)
    monkeypatch.setattr("agent.scenes.mention.orchestrator.run_mention", run_mention)
    monkeypatch.setattr(wa, "get_bot_username", lambda **_kwargs: "open-review-bot")

    payload = {
        "project": {"path_with_namespace": "team/project"},
        "merge_request": {"iid": 42, "source_branch": "feature/router"},
        "user": {"username": "developer"},
        "object_attributes": {
            "id": 999,
            "discussion_id": "disc-1",
            "note": "@open-review-bot explain this change",
        },
    }

    await wa._handle_mention(payload)

    run_mention.assert_awaited_once_with(
        project_id="team/project",
        mr_iid=42,
        repo_dir="/tmp/repo",
        sandbox=ANY,
        note_id=999,
        discussion_id="disc-1",
        note_body="explain this change",
        note_author="developer",
    )
