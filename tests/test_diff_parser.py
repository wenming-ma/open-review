"""Tests for diff line position helpers."""

from __future__ import annotations

from agent.utils import diff_parser


def test_resolve_diff_line_position_for_added_deleted_and_context_lines():
    diff_text = (
        "@@ -8,3 +8,4 @@\n"
        " keep_one();\n"
        "-legacy_retry();\n"
        "+retry_once();\n"
        " keep_two();\n"
        "+new_guard();\n"
    )

    added = diff_parser.resolve_diff_line_position(diff_text, side="new", line=9)
    deleted = diff_parser.resolve_diff_line_position(diff_text, side="old", line=9)
    context = diff_parser.resolve_diff_line_position(diff_text, side="unchanged", line=10)

    assert added is not None
    assert added.kind == "addition"
    assert added.old_line is None
    assert added.new_line == 9
    assert added.content == "retry_once();"

    assert deleted is not None
    assert deleted.kind == "deletion"
    assert deleted.old_line == 9
    assert deleted.new_line is None
    assert deleted.content == "legacy_retry();"

    assert context is not None
    assert context.kind == "context"
    assert context.old_line == 10
    assert context.new_line == 10
    assert context.content == "keep_two();"
