"""Parse unified diffs and map line numbers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class DiffHunk:
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    content: str


@dataclass
class DiffLinePosition:
    kind: Literal["addition", "deletion", "context"]
    old_line: int | None
    new_line: int | None
    content: str


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def parse_diff_hunks(diff_text: str, file_path: str) -> list[DiffHunk]:
    """Parse a unified diff string into hunks with line numbers."""
    hunks = []
    matches = list(_HUNK_RE.finditer(diff_text))

    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        content = diff_text[start:end]

        hunks.append(DiffHunk(
            file_path=file_path,
            old_start=int(m.group(1)),
            old_count=int(m.group(2) or 1),
            new_start=int(m.group(3)),
            new_count=int(m.group(4) or 1),
            content=content,
        ))

    return hunks


def added_lines(diff_text: str) -> list[tuple[int, str]]:
    """Extract added lines (prefixed with +) with their new-file line numbers."""
    result = []
    new_line = 0

    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            new_line = int(m.group(3))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass  # deleted line, don't increment new_line counter
        else:
            new_line += 1

    return result


def diff_line_positions(diff_text: str) -> list[DiffLinePosition]:
    """Parse a unified diff into per-line positions for added, deleted, and context lines."""
    positions: list[DiffLinePosition] = []
    old_line = 0
    new_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            old_line = int(m.group(1))
            new_line = int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            positions.append(
                DiffLinePosition(
                    kind="addition",
                    old_line=None,
                    new_line=new_line,
                    content=line[1:],
                )
            )
            new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            positions.append(
                DiffLinePosition(
                    kind="deletion",
                    old_line=old_line,
                    new_line=None,
                    content=line[1:],
                )
            )
            old_line += 1
            continue
        if line.startswith(" "):
            positions.append(
                DiffLinePosition(
                    kind="context",
                    old_line=old_line,
                    new_line=new_line,
                    content=line[1:],
                )
            )
            old_line += 1
            new_line += 1

    return positions


def resolve_diff_line_position(
    diff_text: str,
    *,
    side: Literal["new", "old", "unchanged"],
    line: int,
) -> DiffLinePosition | None:
    """Resolve a requested diff anchor into explicit old/new line numbers."""
    for item in diff_line_positions(diff_text):
        if side == "new" and item.new_line == line:
            return item
        if side == "old" and item.old_line == line:
            return item
        if side == "unchanged" and item.kind == "context" and item.new_line == line:
            return item
    return None
