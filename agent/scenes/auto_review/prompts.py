"""Prompt builders for the autonomous auto-review workflow."""

from __future__ import annotations

from pathlib import Path

from agent.prompt import EDA_STANDARDS
from agent.rlm import REPO_ANALYST_DESCRIPTION
from agent.scenes.auto_review.selfevolution.prompts import load_prompt_asset_text

SPECIALIST_FOCUS = {
    "correctness": (
        "Find unintended behavior changes, hidden regressions, incorrect assumptions, "
        "broken edge cases, and mismatches between the intended scope and the actual code changes."
    ),
    "reliability": (
        "Find brittle failure handling, resource lifecycle bugs, race conditions, leaks, "
        "state corruption, and stability risks under real failure modes."
    ),
    "contracts": (
        "Find public-header contract breaks, compatibility gaps, API/schema/config mismatches, "
        "missing assertions, and missing tests that hide regressions."
    ),
    "performance-build": (
        "Find build-system breakage, compile/test fallout, silent runtime cost increases, "
        "performance regressions, and toolchain risks."
    ),
    "security": (
        "Find trust-boundary mistakes, unsafe input handling, command/file execution risks, "
        "data exposure, and dangerous defaults."
    ),
}

AUTO_REVIEW_SPECIALIST_DESCRIPTIONS = {
    "correctness": "Investigate functional correctness and regression risk in the MR.",
    "reliability": "Investigate runtime reliability, error handling, concurrency, and lifecycle risks.",
    "contracts": "Investigate API, header, schema, compatibility, and test-contract risks.",
    "performance-build": "Investigate build breakage, compile/test fallout, and performance risks.",
    "security": "Investigate security, trust boundaries, input handling, and dangerous side effects.",
}

AUTO_REVIEW_INVESTIGATION_SUBAGENT_DESCRIPTIONS = {
    "git-inspector": "Inspect MR scope and repository state with direct shell git commands, without drifting into .git internals or temp-script workflows.",
    "trace-impact": "Trace the full affected workflow for the changed code, including callers, callees, state transitions, tests, build/config impact, and adjacent dependent features.",
    "counterexample": "Actively look for evidence that weakens or disproves a suspected issue.",
    "repo-analyst": REPO_ANALYST_DESCRIPTION,
}


def build_auto_review_specialist_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    lane: str,
    authoritative_scope_summary: str | None = None,
) -> str:
    focus = SPECIALIST_FOCUS[lane]
    template = load_prompt_asset_text("specialist-prompt")
    return template.format(
        lane=lane,
        eda_standards=EDA_STANDARDS,
        focus=focus,
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        authoritative_scope_summary=authoritative_scope_summary or "- unavailable",
    )


def build_auto_review_investigation_subagent_prompt(
    repo_dir: str,
    file_tool_repo_dir: str,
    subagent_type: str,
    authoritative_scope_summary: str | None = None,
) -> str:
    description = AUTO_REVIEW_INVESTIGATION_SUBAGENT_DESCRIPTIONS[subagent_type]
    extra_rules = ""
    if subagent_type == "git-inspector":
        extra_rules = f"""
- Your primary job is to inspect MR scope and repository state by running shell `git` commands directly with the `execute` tool.
- The frozen orchestrator scope snapshot and `review_scope` tool are the source of truth for file status. Use them before interpreting caller summaries.
- Start with direct commands such as:
  - `git -C {repo_dir} status --short`
  - `git -C {repo_dir} diff --unified=3 --find-renames origin/master...HEAD`
  - `git -C {repo_dir} log --oneline origin/master..HEAD`
- Do not write temporary helper scripts under `/workspace/tmp` or any other directory just to run git commands.
- Do not read `.git/*`, pack indexes, object files, `packed-refs`, or worktree pointer files unless the caller explicitly asks for low-level git internals.
- If a shell `git` command fails, report the raw command and stderr instead of trying to reconstruct git state through ad-hoc file reads.
"""
    template = load_prompt_asset_text("investigation-subagent-prompt")
    return template.format(
        subagent_type=subagent_type,
        description=description,
        eda_standards=EDA_STANDARDS,
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        authoritative_scope_summary=authoritative_scope_summary or "- unavailable",
        extra_rules=extra_rules,
    )


def get_auto_review_director_prompt() -> str:
    return load_prompt_asset_text("director-prompt").format(eda_standards=EDA_STANDARDS)


AUTO_REVIEW_DIRECTOR_PROMPT = (
    (Path(__file__).resolve().with_name("selfevolution") / "prompts" / "director-prompt.md")
    .read_text(encoding="utf-8")
    .format(eda_standards=EDA_STANDARDS)
)


# Backwards-compatible alias for older imports.
AUTO_REVIEW_CHIEF_REVIEW_PROMPT = AUTO_REVIEW_DIRECTOR_PROMPT
AUTO_REVIEW_REFLECTION_PROMPT = AUTO_REVIEW_DIRECTOR_PROMPT
