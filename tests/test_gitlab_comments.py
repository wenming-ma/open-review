"""Tests for GitLab comment helpers."""

from __future__ import annotations

from types import SimpleNamespace

from agent.gitlab import comments


def test_upsert_mr_comment_by_marker_updates_existing_note(monkeypatch):
    saved = {}

    class _Note:
        def __init__(self, note_id: int, body: str) -> None:
            self.id = note_id
            self.body = body

        def save(self) -> None:
            saved["body"] = self.body

    existing = _Note(91, "old body\n<!-- open-review-summary-kind: auto-review -->")

    class _Notes:
        def list(self, *, get_all: bool):
            del get_all
            return [existing]

        def create(self, _payload):
            raise AssertionError("create should not be used when marker exists")

    monkeypatch.setattr(comments, "get_mr", lambda *_args: SimpleNamespace(notes=_Notes()))

    note_id = comments.upsert_mr_comment_by_marker(
        "team/project",
        42,
        "new body\n<!-- open-review-summary-kind: auto-review -->",
        marker_name="open-review-summary-kind",
        marker_value="auto-review",
    )

    assert note_id == 91
    assert saved["body"].startswith("new body")


def test_upsert_mr_comment_by_marker_creates_when_missing(monkeypatch):
    created = {}

    class _Notes:
        def list(self, *, get_all: bool):
            del get_all
            return []

        def create(self, payload):
            created["body"] = payload["body"]
            return SimpleNamespace(id=92)

    monkeypatch.setattr(comments, "get_mr", lambda *_args: SimpleNamespace(notes=_Notes()))

    note_id = comments.upsert_mr_comment_by_marker(
        "team/project",
        42,
        "new body\n<!-- open-review-summary-kind: auto-review -->",
        marker_name="open-review-summary-kind",
        marker_value="auto-review",
    )

    assert note_id == 92
    assert created["body"].startswith("new body")


def test_post_inline_comment_skips_note_fallback_when_disabled(monkeypatch):
    created = {}

    class _Discussions:
        def create(self, _payload):
            raise RuntimeError("position invalid")

    class _Notes:
        def create(self, _payload):
            created["note"] = _payload
            return SimpleNamespace(id=93)

    mr = SimpleNamespace(
        diff_refs={"base_sha": "base", "start_sha": "start", "head_sha": "head"},
        discussions=_Discussions(),
        notes=_Notes(),
    )
    monkeypatch.setattr(comments, "get_mr", lambda *_args: mr)

    note_id = comments.post_inline_comment(
        "team/project",
        42,
        "src/router.cpp",
        14,
        "```cpp\nrollback();\n```",
        fallback_to_note=False,
    )

    assert note_id is None
    assert "note" not in created
