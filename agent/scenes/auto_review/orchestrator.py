"""Structured multi-stage auto-review workflow."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
from contextvars import ContextVar
from typing import Any, TypeVar

from agent.config import settings
from agent.gitlab.comments import (
    MRCommentRecord,
    list_mr_activity,
    post_inline_comment,
    upsert_mr_comment_by_marker,
)
from agent.gitlab.identity import get_bot_username, resolve_bot_identity
from agent.gitlab.mr_info import get_mr_metadata
from agent.observability import build_open_review_trace_name, start_open_review_span
from agent.runtime.termination import raise_if_run_termination_requested
from agent.sandbox.command_runner import run_command
from agent.sandbox.manager import ensure_repo_refs
from agent.scenes.auto_review.graph import (
    AutoReviewDirectorHarness,
    AutoReviewLaneBackend,
    build_auto_review_director_harness,
    build_auto_review_lane_agent,
)
from agent.scenes.auto_review.models import (
    AutoReviewRunResult,
    CandidateFinding,
    ChangedFileContext,
    ChiefReviewDecision,
    EvidenceBundle,
    LaneReviewResult,
    OpenQuestion,
    RankedReview,
    ReviewCommentContext,
    ReviewContext,
    ReviewSeedContext,
    SpecialistReviewReport,
)
from agent.scenes.auto_review.scope import authoritative_scope_summary
from agent.utils.diff_parser import added_lines
from agent.utils.timezone import compact_timestamp

logger = logging.getLogger(__name__)
T = TypeVar("T")
_AUTO_REVIEW_AGENT_CONFIG: ContextVar[dict[str, Any] | None] = ContextVar(
    "open_review_auto_review_agent_config",
    default=None,
)

_REVIEW_LOCKS: dict[str, asyncio.Lock] = {}
_MARKER_RE = re.compile(r"<!--\s*(open-review-[a-z-]+):\s*([^\s>]+)\s*-->")
_MAX_INLINE_EVIDENCE = 3


class DirectorReviewFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        specialist_reports: list[SpecialistReviewReport] | None = None,
    ) -> None:
        super().__init__(message)
        self.specialist_reports = specialist_reports or []


_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".clj",
    ".cljs",
    ".cpp",
    ".cs",
    ".css",
    ".cxx",
    ".dart",
    ".ex",
    ".exs",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".hs",
    ".hxx",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".m",
    ".mm",
    ".php",
    ".pl",
    ".pm",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_DEFAULT_DIFF_PACK_MAX_CHARS = 20_000
_DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt", ".adoc"}
_BUILD_OR_PACKAGE_FILENAMES = {
    "build.gradle",
    "build.gradle.kts",
    "cargo.toml",
    "cmakelists.txt",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "dockerfile",
    "gemfile",
    "go.mod",
    "justfile",
    "makefile",
    "module.bazel",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pom.xml",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "taskfile.yaml",
    "taskfile.yml",
    "tox.ini",
    "uv.lock",
    "workspace",
    "workspace.bazel",
    "yarn.lock",
}
_BUILD_OR_PACKAGE_SUFFIXES = (".bazel", ".bzl", ".cmake", ".gradle", ".gradle.kts", ".mk")
_CONTRACT_EXTENSIONS = {
    ".d.ts",
    ".graphql",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".json",
    ".jsonschema",
    ".openapi",
    ".proto",
    ".schema",
    ".thrift",
    ".toml",
    ".yaml",
    ".yml",
}
_SPECIALIST_REVIEWERS = [
    "correctness",
    "reliability",
    "contracts",
    "performance-build",
    "security",
]


def _review_key(project_id: str, mr_iid: int) -> str:
    return f"{project_id}!{mr_iid}"


def _auto_review_trace_name(context: ReviewContext) -> str:
    return build_open_review_trace_name(
        "auto_review",
        _review_key(context.project_id, context.mr_iid),
        head_sha=context.head_sha,
        run_key=context.review_run_id,
    )


def _accepts_sandbox_kwarg(func) -> bool:
    return _accepts_kwarg(func, "sandbox")


def _accepts_kwarg(func, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return name in signature.parameters


def _call_with_optional_sandbox(func, *args, sandbox=None, **kwargs):
    if sandbox is not None and _accepts_sandbox_kwarg(func):
        return func(*args, sandbox=sandbox, **kwargs)
    return func(*args, **kwargs)


def _load_auto_review_agent_config(project_id: str) -> dict[str, Any]:
    try:
        from agent.controlplane import get_config_service

        return get_config_service().get_project_agent_config(project_id)
    except Exception:
        logger.warning("Could not load auto-review project config for %s", project_id, exc_info=True)
        return {}


def _auto_review_setting(key: str) -> Any:
    config = _AUTO_REVIEW_AGENT_CONFIG.get()
    if config is not None and key in config:
        return config.get(key)
    return getattr(settings, key)


def _git(
    repo_dir: str,
    *args: str,
    text: bool = True,
    sandbox=None,
) -> Any:
    return run_command(
        ["git", "-C", repo_dir, *args],
        text=text,
        sandbox=sandbox,
    )


def _extract_markers(body: str) -> dict[str, str]:
    return dict(_MARKER_RE.findall(body))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _normalize_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _is_bot_author(author: str) -> bool:
    bot_username = get_bot_username()
    return bool(bot_username) and author.strip().lower() == bot_username.lower()


def _severity_value(value: str | None) -> str:
    normalized = (value or "medium").strip().lower()
    return normalized if normalized in _SEVERITY_RANK else "medium"


def _confidence_value(value: str | None) -> str:
    normalized = (value or "medium").strip().lower()
    return normalized if normalized in _CONFIDENCE_RANK else "medium"


def _derive_dedupe_key(finding: CandidateFinding) -> str:
    basis = "|".join(
        [
            finding.file_path or "",
            str(finding.line or 0),
            finding.symbol or "",
            _normalize_slug(finding.category or finding.source_lane or "general"),
            _normalize_slug(finding.summary)[:96],
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _normalize_finding(finding: CandidateFinding) -> CandidateFinding:
    evidence = [_normalize_text(item) for item in finding.evidence if item and item.strip()]
    normalized = finding.model_copy(
        update={
            "source_lane": finding.source_lane.strip(),
            "file_path": finding.file_path.strip() if finding.file_path else None,
            "line": finding.line if finding.line and finding.line > 0 else None,
            "symbol": finding.symbol.strip() if finding.symbol else None,
            "category": _normalize_slug(finding.category or finding.source_lane or "general"),
            "severity": _severity_value(finding.severity),
            "confidence": _confidence_value(finding.confidence),
            "summary": _normalize_text(finding.summary),
            "details": _normalize_text(finding.details),
            "recommended_fix": _normalize_text(finding.recommended_fix)
            if finding.recommended_fix
            else None,
            "evidence": evidence[:5],
        }
    )
    if not normalized.dedupe_key:
        normalized.dedupe_key = _derive_dedupe_key(normalized)
    return normalized


def _normalize_repo_relative_path(file_path: str | None, *, repo_dir: str, sandbox=None) -> str | None:
    value = (file_path or "").strip()
    if not value:
        return None
    prefixes = [repo_dir.rstrip("/")]
    if sandbox is not None:
        from agent.sandbox.manager import sandbox_file_tool_path, sandbox_visible_path

        prefixes.append(sandbox_file_tool_path(sandbox, repo_dir).rstrip("/"))
        prefixes.append(sandbox_visible_path(sandbox, repo_dir).rstrip("/"))
    for prefix in prefixes:
        if not prefix:
            continue
        if value == prefix:
            return None
        if value.startswith(prefix + "/"):
            return value[len(prefix) + 1 :]
    return value.lstrip("/")


def _finding_sort_key(finding: CandidateFinding) -> tuple[int, int, str]:
    return (
        _SEVERITY_RANK[finding.severity],
        _CONFIDENCE_RANK[finding.confidence],
        finding.summary.lower(),
    )


def _build_comment_context(comment: MRCommentRecord) -> ReviewCommentContext:
    markers = _extract_markers(comment.body)
    return ReviewCommentContext(
        note_id=comment.note_id,
        discussion_id=comment.discussion_id,
        author=comment.author,
        body=comment.body,
        created_at=comment.created_at,
        file_path=comment.file_path,
        line=comment.line,
        is_bot=_is_bot_author(comment.author),
        dedupe_keys=[value for name, value in markers.items() if name == "open-review-dedupe"],
        head_sha=markers.get("open-review-head-sha"),
        diff_fingerprint=markers.get("open-review-diff-fingerprint"),
    )


def _ensure_review_refs(
    project_id: str,
    repo_dir: str,
    source_branch: str,
    target_branch: str,
    *,
    sandbox=None,
) -> None:
    kwargs: dict[str, Any] = {
        "project_id": project_id,
        "repo_dir": repo_dir,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "sandbox": sandbox,
    }
    if _accepts_kwarg(ensure_repo_refs, "fetch_depth"):
        kwargs["fetch_depth"] = int(_auto_review_setting("AUTO_REVIEW_FETCH_DEPTH") or 0)
    ensure_repo_refs(**kwargs)


def _git_has_commit(repo_dir: str, sha: str | None, *, sandbox=None) -> bool:
    if not sha:
        return False
    result = _git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}", sandbox=sandbox)
    return result.returncode == 0


def _build_review_ranges(
    repo_dir: str,
    target_branch: str,
    previous_head_sha: str | None,
    *,
    sandbox=None,
) -> tuple[str, str, str]:
    if previous_head_sha and _git_has_commit(repo_dir, previous_head_sha, sandbox=sandbox):
        return "incremental", f"{previous_head_sha}..HEAD", f"{previous_head_sha}..HEAD"
    return "full", f"origin/{target_branch}...HEAD", f"origin/{target_branch}..HEAD"


def _collect_changed_files(
    repo_dir: str,
    diff_range: str,
    *,
    sandbox=None,
) -> list[ChangedFileContext]:
    name_status = _git(
        repo_dir,
        "diff",
        "--name-status",
        "--find-renames",
        "-z",
        diff_range,
        text=False,
        sandbox=sandbox,
    )
    if name_status.returncode != 0:
        raise RuntimeError(name_status.stderr.decode("utf-8", errors="replace"))

    entries = name_status.stdout.decode("utf-8", errors="replace").split("\0")
    if entries and entries[-1] == "":
        entries.pop()

    results: list[ChangedFileContext] = []
    i = 0
    while i < len(entries):
        status = entries[i]
        i += 1
        if not status:
            continue

        if status.startswith("R") or status.startswith("C"):
            old_path = entries[i]
            new_path = entries[i + 1]
            i += 2
            renamed_file = status.startswith("R")
        else:
            old_path = entries[i]
            new_path = entries[i]
            i += 1
            renamed_file = False

        diff_proc = _git(
            repo_dir,
            "diff",
            "--unified=3",
            "--find-renames",
            diff_range,
            "--",
            new_path,
            sandbox=sandbox,
        )
        diff_text = diff_proc.stdout if diff_proc.returncode == 0 else ""
        results.append(
            ChangedFileContext(
                file_path=new_path,
                old_path=old_path,
                diff=diff_text,
                new_file=status.startswith("A"),
                deleted_file=status.startswith("D"),
                renamed_file=renamed_file,
                added_lines=[line for line, _ in added_lines(diff_text)],
            )
        )

    return results


def _collect_commit_messages(repo_dir: str, commit_range: str, *, sandbox=None) -> list[str]:
    result = _git(repo_dir, "log", "--format=%s", commit_range, sandbox=sandbox)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _build_review_run_id(diff_fingerprint: str) -> str:
    timestamp = compact_timestamp()
    return f"{timestamp}-{diff_fingerprint[:8]}"


def _head_is_current(project_id: str, mr_iid: int, expected_head_sha: str | None) -> bool:
    if not expected_head_sha:
        return True
    return get_mr_metadata(project_id, mr_iid).head_sha == expected_head_sha


def _tail_with_optional_limit(items: list[T], limit: int) -> list[T]:
    return items[-limit:] if limit > 0 else items


def _max_published_findings(candidate_count: int | None = None) -> int | None:
    configured = int(_auto_review_setting("AUTO_REVIEW_MAX_PUBLISHED_FINDINGS") or 0)
    if configured <= 0:
        return candidate_count
    if candidate_count is None:
        return configured
    return min(configured, candidate_count)


def _is_doc_like_path(file_path: str) -> bool:
    normalized = file_path.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("docs/"):
        return True
    if normalized.endswith(tuple(_DOC_EXTENSIONS)):
        return True
    return False


def _is_source_like_path(file_path: str) -> bool:
    normalized = file_path.strip().lower()
    if not normalized:
        return False
    if any(part in {".github", "docs", "test", "tests"} for part in normalized.split("/")):
        return False
    return any(normalized.endswith(ext) for ext in _SOURCE_EXTENSIONS)


def _is_build_or_package_path(file_path: str) -> bool:
    normalized = file_path.strip().lower()
    if not normalized:
        return False
    name = normalized.rsplit("/", 1)[-1]
    return name in _BUILD_OR_PACKAGE_FILENAMES or normalized.endswith(_BUILD_OR_PACKAGE_SUFFIXES)


def _is_contract_sensitive_path(file_path: str) -> bool:
    normalized = file_path.strip().lower()
    if not normalized:
        return False
    if normalized.startswith(("api/", "apis/", "schema/", "schemas/", "migrations/")):
        return True
    if "/api/" in normalized or "/schemas/" in normalized or "/migrations/" in normalized:
        return True
    return any(normalized.endswith(ext) for ext in _CONTRACT_EXTENSIONS)


def _route_review_profile(context: ReviewContext) -> str:
    if context.changed_files and all(_is_doc_like_path(item.file_path) for item in context.changed_files):
        return "docs_only"
    return "deep"


def _risk_signals(context: ReviewContext) -> list[str]:
    signals: list[str] = []
    source_like_count = 0
    for item in context.changed_files:
        path = item.file_path.lower()
        if _is_source_like_path(path):
            source_like_count += 1
        if _is_build_or_package_path(path):
            signals.append("build_or_package_system")
        if _is_contract_sensitive_path(path):
            signals.append("public_contract")
        if any(token in path for token in ("migration", "parser", "serialize", "protocol", "schema")):
            signals.append("parser_or_schema")
        if any(token in path for token in ("auth", "credential", "secret", "token", "crypto")):
            signals.append("security_sensitive")
        if any(token in path for token in ("thread", "concurr", "async", "lock", "mutex")):
            signals.append("concurrency")
        if any(token in path for token in ("shell", "exec", "process", "subprocess", "command")):
            signals.append("command_execution")
        if any(token in path for token in ("network", "socket", "http", "tcp", "udp")):
            signals.append("network_boundary")
    if source_like_count >= 5:
        signals.append("broad_code_change")
    return list(dict.fromkeys(signals))


def _build_repo_map(
    context: ReviewContext,
    repo_dir: str,
    *,
    sandbox=None,
) -> str:
    del repo_dir, sandbox
    if not context.changed_files:
        return "- no changed files"

    top_level_dirs: list[str] = []
    public_contracts: list[str] = []
    related_tests: list[str] = []
    for item in context.changed_files:
        parts = item.file_path.split("/", 1)
        top_level_dirs.append(parts[0])
        lowered = item.file_path.lower()
        if _is_contract_sensitive_path(lowered):
            public_contracts.append(item.file_path)
        stem = item.file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if "test" not in lowered and stem:
            related_tests.append(f"tests/**/*{stem}*")

    lines = [
        "## Repository map",
        "Top-level areas touched:",
        *[f"- {name}" for name in list(dict.fromkeys(top_level_dirs))[:10]],
    ]
    if public_contracts:
        lines.extend(["", "Public or contract-sensitive files:", *[f"- {path}" for path in public_contracts[:10]]])
    if related_tests:
        lines.extend(["", "Likely related test globs:", *[f"- {path}" for path in list(dict.fromkeys(related_tests))[:10]]])
    return "\n".join(lines)


def build_evidence_bundle(
    context: ReviewContext,
    *,
    sandbox=None,
) -> EvidenceBundle:
    review_profile = _route_review_profile(context)
    return EvidenceBundle(
        review_profile=review_profile,
        repo_map=_build_repo_map(context, context.repo_dir, sandbox=sandbox),
        compile_check=None,
        risk_signals=_risk_signals(context),
    )


def _seed_comment_summary(comment: ReviewCommentContext) -> str | None:
    cleaned = re.sub(r"<!--.*?-->", " ", comment.body, flags=re.DOTALL)
    normalized = _normalize_text(cleaned)
    if not normalized:
        return None
    return f"@{comment.author}: {normalized[:240]}"


def build_review_seed_context(context: ReviewContext) -> ReviewSeedContext:
    return ReviewSeedContext(
        review_profile=_route_review_profile(context),
        diff_range=context.diff_range,
        commit_range=context.commit_range,
        changed_files=[item.file_path for item in context.changed_files],
        commit_messages=[_normalize_text(item) for item in context.commit_messages if item.strip()],
        recent_human_comments=[
            summary
            for comment in context.recent_human_comments
            if (summary := _seed_comment_summary(comment)) is not None
        ],
        previous_bot_comment_summaries=[
            summary
            for comment in context.previous_bot_comments
            if (summary := _seed_comment_summary(comment)) is not None
        ],
    )


def _build_diff_pack(
    changed_files: list[ChangedFileContext],
    *,
    max_chars: int = _DEFAULT_DIFF_PACK_MAX_CHARS,
) -> tuple[str, bool, list[str]]:
    if not changed_files:
        return "- no changed files", False, []

    sections: list[str] = []
    overflow: list[str] = []
    used = 0
    compressed = False
    for item in changed_files:
        diff_text = item.diff.strip() or "(diff unavailable)"
        section = f"## File: `{item.file_path}`\n{diff_text}\n"
        projected = used + len(section)
        if sections and projected > max_chars:
            overflow.append(item.file_path)
            compressed = True
            continue
        sections.append(section)
        used = projected

    if not sections:
        first = changed_files[0]
        truncated = (first.diff.strip() or "(diff unavailable)")[: max_chars - 80]
        sections.append(f"## File: `{first.file_path}`\n{truncated}\n")
        overflow.extend(item.file_path for item in changed_files[1:])
        compressed = True

    return "\n".join(sections).strip(), compressed, overflow


def build_review_context(
    project_id: str,
    mr_iid: int,
    repo_dir: str,
    *,
    sandbox=None,
) -> ReviewContext:
    """Collect and normalize all inputs needed for an auto-review run."""
    meta = get_mr_metadata(project_id, mr_iid)
    _call_with_optional_sandbox(
        _ensure_review_refs,
        project_id,
        repo_dir,
        meta.source_branch,
        meta.target_branch,
        sandbox=sandbox,
    )

    activity = [_build_comment_context(item) for item in list_mr_activity(project_id, mr_iid)]
    previous_bot_comments = [item for item in activity if item.is_bot]
    recent_human_comments = [item for item in activity if not item.is_bot and not item.body.isspace()]
    recent_human_comments = _tail_with_optional_limit(
        recent_human_comments,
        int(_auto_review_setting("AUTO_REVIEW_HUMAN_COMMENT_LIMIT") or 0),
    )

    previous_review_head_sha = None
    previous_review_diff_fingerprint = None
    previous_bot_dedupe_keys: list[str] = []
    for comment in previous_bot_comments:
        previous_bot_dedupe_keys.extend(comment.dedupe_keys)
        if comment.head_sha:
            previous_review_head_sha = comment.head_sha
            previous_review_diff_fingerprint = comment.diff_fingerprint

    review_mode, diff_range, commit_range = _call_with_optional_sandbox(
        _build_review_ranges,
        repo_dir,
        meta.target_branch,
        previous_review_head_sha,
        sandbox=sandbox,
    )
    diff_result = _git(
        repo_dir,
        "diff",
        "--unified=3",
        "--find-renames",
        diff_range,
        sandbox=sandbox,
    )
    if diff_result.returncode != 0:
        detail = (str(diff_result.stderr or diff_result.stdout or "")).strip()
        raise RuntimeError(f"git diff failed for {diff_range}: {detail[:500] or 'no output'}")
    diff_text = diff_result.stdout
    diff_fingerprint = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

    context = ReviewContext(
        project_id=project_id,
        mr_iid=mr_iid,
        title=meta.title,
        description=meta.description,
        author=meta.author,
        url=meta.url,
        source_branch=meta.source_branch,
        target_branch=meta.target_branch,
        base_sha=meta.base_sha,
        start_sha=meta.start_sha,
        head_sha=meta.head_sha,
        repo_dir=repo_dir,
        review_run_id=_build_review_run_id(diff_fingerprint),
        review_mode=review_mode,
        diff_range=diff_range,
        commit_range=commit_range,
        diff_text=diff_text,
        diff_fingerprint=diff_fingerprint,
        changed_files=_call_with_optional_sandbox(
            _collect_changed_files,
            repo_dir,
            diff_range,
            sandbox=sandbox,
        )
        if diff_text
        else [],
        commit_messages=_call_with_optional_sandbox(
            _collect_commit_messages,
            repo_dir,
            commit_range,
            sandbox=sandbox,
        ),
        previous_review_head_sha=previous_review_head_sha,
        previous_review_diff_fingerprint=previous_review_diff_fingerprint,
        previous_bot_comments=_tail_with_optional_limit(
            previous_bot_comments,
            int(_auto_review_setting("AUTO_REVIEW_COMMENT_HISTORY_LIMIT") or 0),
        ),
        previous_bot_dedupe_keys=previous_bot_dedupe_keys,
        recent_human_comments=recent_human_comments,
    )
    context.diff_pack, context.diff_pack_compressed, context.diff_pack_overflow_files = _build_diff_pack(
        context.changed_files
    )

    if (
        previous_review_head_sha == context.head_sha
        and previous_review_diff_fingerprint
        and previous_review_diff_fingerprint == context.diff_fingerprint
    ):
        context.skip_reason = "head_sha_already_reviewed"
    elif not context.diff_text.strip():
        context.skip_reason = "empty_diff"

    return context


def _human_comment_summary(context: ReviewContext) -> str:
    if not context.recent_human_comments:
        return "- none"
    items = []
    for comment in context.recent_human_comments:
        body = _normalize_text(comment.body)[:240]
        items.append(f"- @{comment.author}: {body}")
    return "\n".join(items)


def _changed_file_summary(context: ReviewContext) -> str:
    if not context.changed_files:
        return "- none"
    lines = []
    for item in context.changed_files[:40]:
        flags = []
        if item.new_file:
            flags.append("new")
        if item.deleted_file:
            flags.append("deleted")
        if item.renamed_file:
            flags.append("renamed")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"- {item.file_path}{suffix}")
    return "\n".join(lines)


def _lane_message(context: ReviewContext, lane: str) -> str:
    return _specialist_message(
        context,
        EvidenceBundle(review_profile="deep", repo_map="", compile_check=None, risk_signals=[]),
        lane,
    )


def _specialist_message(context: ReviewContext, evidence: EvidenceBundle, lane: str) -> str:
    diff_pack_notice = (
        "预构建 Diff 包（已压缩，额外文件见下方列表）："
        if context.diff_pack_compressed
        else "预构建 Diff 包："
    )
    overflow_block = ""
    if context.diff_pack_overflow_files:
        overflow_block = (
            "\n由于上下文预算限制，以下文件未放入预构建包：\n"
            + "\n".join(f"- {item}" for item in context.diff_pack_overflow_files[:20])
        )
    return f"""请从 {lane} lane 审查这个合并请求。

仓库根目录：{context.repo_dir}
审查模式：{context.review_mode}
优先检查的 Diff 范围：{context.diff_range}
MR：!{context.mr_iid} {context.title}
作者：{context.author}
描述：
{context.description or "(empty)"}
Review profile：{evidence.review_profile}

{authoritative_scope_summary(context)}

变更文件：
{_changed_file_summary(context)}

提交信息：
{chr(10).join(f"- {item}" for item in context.commit_messages[:10]) or "- none"}

最近人工评论：
{_human_comment_summary(context)}

风险信号：
{chr(10).join(f"- {item}" for item in evidence.risk_signals) or "- none"}

仓库结构图：
{evidence.repo_map or "- none"}

{diff_pack_notice}
{context.diff_pack}
{overflow_block}

先从这里检查：
`git -C {context.repo_dir} diff --unified=3 --find-renames {context.diff_range}`

规则补充：
- git 事实优先通过 `git` 命令获取，不要通过读取 `.git/HEAD`、`.git/config`、`.git/refs/*` 来重建 MR 范围
- file tools 主要用于源码、测试、构建文件和文本工件，不把 `.git` internals 当作主入口

只返回结构化结果。
"""


async def _run_specialist_review(
    context: ReviewContext,
    evidence: EvidenceBundle,
    lane: str,
    sandbox,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
) -> LaneReviewResult:
    with start_open_review_span(
        f"open_review.auto_review.lane.{lane}",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.review_run_id": context.review_run_id,
            "open_review.lane": lane,
        },
        metadata={"changed_files": [item.file_path for item in context.changed_files]},
        tags=["auto_review", lane],
    ):
        lane_backend = AutoReviewLaneBackend(sandbox, repo_dir=context.repo_dir, review_context=context)
        agent = build_auto_review_lane_agent(
            lane_backend,
            context.repo_dir,
            lane,
            model_id=model_id,
            review_context=context,
        )
        input_messages = [{"role": "user", "content": _specialist_message(context, evidence, lane)}]
        try:
            result = await agent.ainvoke(
                {"messages": input_messages},
                config={
                    "configurable": {
                        "project_id": context.project_id,
                        "mr_iid": context.mr_iid,
                        "repo_dir": lane_backend.shell_repo_dir,
                    }
                },
            )
        except Exception as exc:
            logger.exception("Lane %s failed", lane)
            return LaneReviewResult(
                lane=lane,
                status="error",
                error=str(exc),
                tool_error_count=lane_backend.tool_error_count,
                semantic_failure_count=lane_backend.semantic_failure_count,
                degraded_reason="; ".join(dict.fromkeys(lane_backend.failure_reasons[:3])) or None,
            )

    structured = result.get("structured_response")
    if structured is None:
        return LaneReviewResult(
            lane=lane,
            status="error",
            error="missing structured_response",
            tool_error_count=lane_backend.tool_error_count,
            semantic_failure_count=lane_backend.semantic_failure_count,
            degraded_reason="; ".join(dict.fromkeys(lane_backend.failure_reasons[:3])) or None,
        )

    findings = [
        _normalize_finding(
            item.model_copy(
                update={
                    "source_lane": lane,
                    "file_path": _normalize_repo_relative_path(
                        item.file_path,
                        repo_dir=context.repo_dir,
                        sandbox=sandbox,
                    ),
                }
            )
        )
        for item in structured.findings
    ]
    degraded_reason = "; ".join(dict.fromkeys(lane_backend.failure_reasons[:3])) or None
    status = (
        "degraded"
        if lane_backend.tool_error_count > 0 or lane_backend.semantic_failure_count > 0
        else "ok"
    )
    return LaneReviewResult(
        lane=lane,
        status=status,
        summary=_normalize_text(structured.summary),
        checks_run=structured.checks_run,
        findings=findings,
        tool_error_count=lane_backend.tool_error_count,
        semantic_failure_count=lane_backend.semantic_failure_count,
        degraded_reason=degraded_reason,
    )


async def _run_specialist_reviews(
    context: ReviewContext,
    evidence: EvidenceBundle,
    sandbox,
    model_id: str | None = None,
) -> list[LaneReviewResult]:
    lanes = _SPECIALIST_REVIEWERS
    return await asyncio.gather(
        *[_run_specialist_review(context, evidence, lane, sandbox, model_id=model_id) for lane in lanes]
    )


async def _run_lane_reviews(
    context: ReviewContext,
    sandbox,
    model_id: str | None = None,
) -> list[LaneReviewResult]:
    evidence = build_evidence_bundle(context, sandbox=sandbox)
    return await _run_specialist_reviews(context, evidence, sandbox, model_id=model_id)


def _deterministic_rank_findings(
    findings: list[CandidateFinding],
    *,
    max_findings: int | None,
) -> RankedReview:
    confirmed: list[CandidateFinding] = []
    suspicious: list[CandidateFinding] = []
    for finding in findings:
        normalized = _normalize_finding(finding)
        if _CONFIDENCE_RANK.get(normalized.confidence, 0) >= _CONFIDENCE_RANK["high"]:
            confirmed.append(normalized)
        else:
            suspicious.append(normalized)
    if max_findings is not None and max_findings >= 0:
        confirmed = confirmed[:max_findings]
        suspicious = suspicious[: max(0, max_findings - len(confirmed))]

    summary = "未发现明确问题，以下报告以可疑点和开放问题为主。" if not confirmed else "已完成问题整理，请优先关注已确认问题。"
    return RankedReview(
        recommendation=_resolve_review_recommendation(None, confirmed),
        summary=summary,
        confirmed_findings=confirmed,
        suspicious_findings=suspicious,
        open_questions=[],
        inline_candidates=[],
    )


def _deterministic_chief_review(
    findings: list[CandidateFinding],
    *,
    max_findings: int | None,
) -> ChiefReviewDecision:
    ranked = _deterministic_rank_findings(findings, max_findings=max_findings)
    return ChiefReviewDecision(
        summary=ranked.summary,
        confirmed_findings=ranked.confirmed_findings,
        suspicious_findings=ranked.suspicious_findings,
        open_questions=ranked.open_questions,
    )


def _seed_list_block(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- none"


def _director_message(
    context: ReviewContext,
    seed: ReviewSeedContext,
    *,
    shell_repo_dir: str,
    file_tool_repo_dir: str,
) -> str:
    return f"""本次 Merge Request 自动审查上下文如下。

Shell 仓库根目录：{shell_repo_dir}
文件工具根目录：{file_tool_repo_dir}
MR：!{context.mr_iid} {context.title}
作者：{context.author}
Review profile：{seed.review_profile}
当前审查模式：{context.review_mode}

当前种子信息：
- diff_range: `{seed.diff_range}`
- commit_range: `{seed.commit_range}`
- changed_files_count: `{len(seed.changed_files)}`

{authoritative_scope_summary(context)}

初始 changed files：
{_seed_list_block(seed.changed_files)}

提交信息：
{_seed_list_block(seed.commit_messages[:20])}

最近人工评论：
{_seed_list_block(seed.recent_human_comments[:10])}

之前 bot 评论摘要：
{_seed_list_block(seed.previous_bot_comment_summaries[:10])}

建议起手命令：
- `git -C {shell_repo_dir} status --short`
- `git -C {shell_repo_dir} diff --unified=3 --find-renames {seed.diff_range}`
- `git -C {shell_repo_dir} log --oneline {seed.commit_range}`
"""


def _extract_message_text(message: object) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part for part in parts if part)
    return ""


def _coerce_director_decision(result: dict[str, Any]) -> ChiefReviewDecision:
    structured = result.get("structured_response", result)
    if isinstance(structured, ChiefReviewDecision):
        return structured
    if isinstance(structured, dict):
        return ChiefReviewDecision.model_validate(structured)
    raise RuntimeError("missing structured_response")


def _normalize_specialist_report(
    context: ReviewContext,
    report: SpecialistReviewReport,
    *,
    sandbox,
) -> SpecialistReviewReport:
    lane = report.lane.strip() or "unknown"
    findings = [
        _normalize_finding(
            item.model_copy(
                update={
                    "source_lane": item.source_lane or lane,
                    "file_path": _normalize_repo_relative_path(
                        item.file_path,
                        repo_dir=context.repo_dir,
                        sandbox=sandbox,
                    ),
                }
            )
        )
        for item in report.findings
    ]
    return report.model_copy(
        update={
            "lane": lane,
            "summary": _normalize_text(report.summary),
            "findings": findings,
        }
    )


def _merge_specialist_reports(
    context: ReviewContext,
    seed: ReviewSeedContext,
    reports: list[SpecialistReviewReport],
    harness: AutoReviewDirectorHarness,
    *,
    sandbox,
) -> list[SpecialistReviewReport]:
    del seed, harness
    return [
        _normalize_specialist_report(context, item, sandbox=sandbox)
        for item in reports
        if item.lane
    ]


def _dedupe_findings(findings: list[CandidateFinding]) -> list[CandidateFinding]:
    deduped: dict[str, CandidateFinding] = {}
    for finding in findings:
        normalized = _normalize_finding(finding)
        dedupe_key = normalized.dedupe_key or _derive_dedupe_key(normalized)
        normalized.dedupe_key = dedupe_key
        existing = deduped.get(dedupe_key)
        if existing is None or _finding_sort_key(normalized) > _finding_sort_key(existing):
            deduped[dedupe_key] = normalized
    return sorted(deduped.values(), key=_finding_sort_key, reverse=True)


def _dedupe_open_questions(items: list[OpenQuestion]) -> list[OpenQuestion]:
    deduped: dict[tuple[str | None, str, str], OpenQuestion] = {}
    for item in items:
        key = (item.source_lane, _normalize_text(item.summary), _normalize_text(item.details))
        existing = deduped.get(key)
        if existing is None or len(item.evidence) > len(existing.evidence):
            deduped[key] = item
    return list(deduped.values())


def _is_reliable_inline_candidate(context: ReviewContext, finding: CandidateFinding) -> bool:
    if not finding.file_path or finding.line is None:
        return False
    changed_files = {item.file_path: item for item in context.changed_files}
    changed = changed_files.get(finding.file_path)
    if changed is None:
        return False
    if _CONFIDENCE_RANK.get(finding.confidence, 0) < _CONFIDENCE_RANK["high"]:
        return False
    if changed.added_lines:
        return finding.line in changed.added_lines
    return True


def _finalize_report_summary(summary: str, lane_results: list[LaneReviewResult | SpecialistReviewReport]) -> str:
    del lane_results
    base = summary.strip() or "已完成问题整理，请优先关注已确认问题。"
    return base


def _resolve_review_recommendation(
    recommendation: str | None,
    confirmed_findings: list[CandidateFinding],
) -> str:
    if recommendation:
        return recommendation
    return "建议重新修改" if confirmed_findings else "建议合并"


def _deterministic_director_decision(
    context: ReviewContext,
    specialist_reports: list[SpecialistReviewReport],
) -> ChiefReviewDecision:
    del context
    findings = [finding for report in specialist_reports for finding in report.candidate_findings]
    confirmed: list[CandidateFinding] = []
    suspicious: list[CandidateFinding] = []
    for finding in findings:
        normalized = _normalize_finding(finding)
        if _CONFIDENCE_RANK.get(normalized.confidence, 0) >= _CONFIDENCE_RANK["high"]:
            confirmed.append(normalized)
        else:
            suspicious.append(normalized)
    open_questions = [
        OpenQuestion(source_lane=report.lane, summary=item)
        for report in specialist_reports
        for item in report.open_questions
        if item.strip()
    ]
    return ChiefReviewDecision(
        recommendation=_resolve_review_recommendation(None, confirmed),
        summary="已完成跨 specialist 的调查汇总。",
        specialist_reports=specialist_reports,
        confirmed_findings=_dedupe_findings(confirmed),
        suspicious_findings=_dedupe_findings(suspicious),
        open_questions=_dedupe_open_questions(open_questions),
    )


async def _run_review_director(
    context: ReviewContext,
    seed: ReviewSeedContext,
    sandbox,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
) -> ChiefReviewDecision:
    with start_open_review_span(
        "open_review.auto_review.director",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.review_run_id": context.review_run_id,
            "open_review.review_profile": seed.review_profile,
            "open_review.model_id": model_id or settings.LLM_MODEL_ID,
        },
        metadata={"changed_files": seed.changed_files},
        tags=["auto_review", "director"],
        span_kind="agent",
    ) as trace_ctx:
        harness = build_auto_review_director_harness(
            sandbox=sandbox,
            repo_dir=context.repo_dir,
            model_id=model_id,
            review_context=context,
            runtime_run_id=runtime_run_id,
        )
        director_message = _director_message(
            context,
            seed,
            shell_repo_dir=harness.shell_repo_dir,
            file_tool_repo_dir=harness.file_tool_repo_dir,
        )
        input_messages = [{"role": "user", "content": director_message}]
        trace_ctx.set_input({"messages": input_messages})
        try:
            result = await harness.agent.ainvoke(
                {"messages": input_messages},
                config={
                    "run_name": _auto_review_trace_name(context),
                    "configurable": {
                        "project_id": context.project_id,
                        "mr_iid": context.mr_iid,
                        "repo_dir": harness.shell_repo_dir,
                        "review_run_id": context.review_run_id,
                    }
                },
            )
            trace_ctx.add_event(
                "invoke_completed",
                {
                    "payload_keys": sorted(result.keys()) if isinstance(result, dict) else None,
                    "structured_response_present": isinstance(result, dict)
                    and result.get("structured_response") is not None,
                },
            )
            decision = _coerce_director_decision(result)
            specialist_reports = _merge_specialist_reports(
                context,
                seed,
                decision.specialist_reports,
                harness,
                sandbox=sandbox,
            )
            final_decision = decision.model_copy(update={"specialist_reports": specialist_reports})
            if (
                not final_decision.summary.strip()
                and not final_decision.confirmed_findings
                and not final_decision.suspicious_findings
                and not final_decision.open_questions
            ):
                failure = DirectorReviewFailure(
                    "empty director decision",
                    specialist_reports=specialist_reports,
                )
                trace_ctx.record_exception(failure)
                trace_ctx.set_error_status(str(failure))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": failure.__class__.__name__,
                        "payload_keys": sorted(result.keys()) if isinstance(result, dict) else None,
                        "structured_response_present": isinstance(result, dict)
                        and result.get("structured_response") is not None,
                    },
                )
                logger.error("Director returned an empty shell result; failing closed")
                raise failure
            trace_ctx.set_output(final_decision.model_dump(mode="json"))
            return final_decision
        except DirectorReviewFailure:
            raise
        except Exception as exc:
            trace_ctx.record_exception(exc)
            trace_ctx.set_error_status(str(exc))
            if "result" in locals() and isinstance(result, dict):
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "payload_keys": sorted(result.keys()),
                        "structured_response_present": result.get("structured_response") is not None,
                    },
                )
            else:
                trace_ctx.add_event("invoke_failed", {"error_type": exc.__class__.__name__})
            logger.exception("Director review failed; failing closed")
            specialist_reports = _merge_specialist_reports(
                context,
                seed,
                [],
                harness,
                sandbox=sandbox,
            )
            raise DirectorReviewFailure(
                str(exc),
                specialist_reports=specialist_reports,
            ) from exc


def _finalize_director_decision(
    context: ReviewContext,
    decision: ChiefReviewDecision,
) -> RankedReview:
    previous_keys = set(context.previous_bot_dedupe_keys)
    confirmed = [
        item
        for item in _dedupe_findings(decision.confirmed_findings)
        if (item.dedupe_key or _derive_dedupe_key(item)) not in previous_keys
    ]
    suspicious = [
        item
        for item in _dedupe_findings(decision.suspicious_findings)
        if (item.dedupe_key or _derive_dedupe_key(item))
        not in {finding.dedupe_key or _derive_dedupe_key(finding) for finding in confirmed}
        and (item.dedupe_key or _derive_dedupe_key(item)) not in previous_keys
    ]
    open_questions = _dedupe_open_questions(decision.open_questions)
    max_findings = _max_published_findings(len(confirmed) + len(suspicious))
    if max_findings is not None and max_findings >= 0:
        confirmed = confirmed[:max_findings]
        suspicious = suspicious[: max(0, max_findings - len(confirmed))]
    inline_candidates = [
        finding for finding in confirmed if _is_reliable_inline_candidate(context, finding)
    ]
    return RankedReview(
        recommendation=_resolve_review_recommendation(decision.recommendation, confirmed),
        summary=_finalize_report_summary(decision.summary, decision.specialist_reports),
        confirmed_findings=confirmed,
        suspicious_findings=suspicious,
        open_questions=open_questions,
        inline_candidates=inline_candidates,
    )


async def _run_chief_review(
    context: ReviewContext,
    evidence: EvidenceBundle,
    lane_results: list[LaneReviewResult],
    sandbox,
    model_id: str | None = None,
) -> ChiefReviewDecision:
    del evidence, sandbox, model_id
    with start_open_review_span(
        "open_review.auto_review.chief_review",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.review_run_id": context.review_run_id,
        },
        tags=["auto_review", "chief_review"],
    ):
        findings = [finding for lane in lane_results for finding in lane.findings]
        return _deterministic_chief_review(
            findings,
            max_findings=_max_published_findings(len(findings)),
        )


def _finalize_chief_review(
    context: ReviewContext,
    decision: ChiefReviewDecision,
    lane_results: list[LaneReviewResult],
) -> RankedReview:
    provisional = RankedReview(
        recommendation=decision.recommendation,
        summary=decision.summary,
        confirmed_findings=decision.confirmed_findings,
        suspicious_findings=decision.suspicious_findings,
        open_questions=decision.open_questions,
        inline_candidates=[],
    )
    return _finalize_ranked_review(context, provisional, lane_results)


async def _reflect_and_rank(
    context: ReviewContext,
    lane_results: list[LaneReviewResult],
    model_id: str | None = None,
) -> RankedReview:
    del model_id
    with start_open_review_span(
        "open_review.auto_review.reflect",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.review_run_id": context.review_run_id,
        },
        tags=["auto_review", "reflect"],
    ):
        findings = [finding for lane in lane_results for finding in lane.findings]
        return _deterministic_rank_findings(
            findings,
            max_findings=_max_published_findings(len(findings)),
        )


def _finalize_ranked_review(
    context: ReviewContext,
    ranked: RankedReview,
    lane_results: list[LaneReviewResult],
) -> RankedReview:
    previous_keys = set(context.previous_bot_dedupe_keys)
    confirmed = [
        item
        for item in _dedupe_findings(ranked.confirmed_findings)
        if (item.dedupe_key or _derive_dedupe_key(item)) not in previous_keys
    ]
    confirmed_keys = {item.dedupe_key or _derive_dedupe_key(item) for item in confirmed}
    suspicious = [
        item
        for item in _dedupe_findings(ranked.suspicious_findings)
        if (item.dedupe_key or _derive_dedupe_key(item)) not in confirmed_keys
        and (item.dedupe_key or _derive_dedupe_key(item)) not in previous_keys
    ]
    open_questions = _dedupe_open_questions(ranked.open_questions)
    max_findings = _max_published_findings(len(confirmed) + len(suspicious))
    if max_findings is not None and max_findings >= 0:
        confirmed = confirmed[:max_findings]
        suspicious = suspicious[: max(0, max_findings - len(confirmed))]
    inline_candidates = [item for item in confirmed if _is_reliable_inline_candidate(context, item)]
    return RankedReview(
        recommendation=_resolve_review_recommendation(ranked.recommendation, confirmed),
        summary=_finalize_report_summary(ranked.summary, lane_results),
        confirmed_findings=confirmed,
        suspicious_findings=suspicious,
        open_questions=open_questions,
        inline_candidates=inline_candidates,
    )


def _format_inline_comment(finding: CandidateFinding, review_run_id: str) -> str:
    severity = finding.severity.upper()
    evidence_lines = "\n".join(f"- {item}" for item in finding.evidence[:_MAX_INLINE_EVIDENCE]) or "- 未记录证据。"
    body = [
        f"**{severity}** {finding.summary}",
        "",
        finding.details,
        "",
        "**证据**",
        evidence_lines,
    ]
    if finding.recommended_fix:
        body.extend(["", "**建议修复**", finding.recommended_fix])
    body.extend(
        [
            "",
            f"<!-- open-review-review-run: {review_run_id} -->",
            f"<!-- open-review-dedupe: {finding.dedupe_key} -->",
        ]
    )
    return "\n".join(body)


def _format_summary_comment(
    context: ReviewContext,
    ranked: RankedReview,
    lane_results: list[LaneReviewResult],
) -> str:
    del lane_results
    recommendation = _resolve_review_recommendation(
        ranked.recommendation,
        ranked.confirmed_findings,
    )

    lines = [
        "## Open Review 自动审查",
        "",
        f"审查结论：**{recommendation}**",
        "",
        f"- 模式：`{context.review_mode}`",
        f"- 范围：`{len(context.changed_files)}` 个文件",
        f"- 已确认问题：`{len(ranked.confirmed_findings)}`",
        f"- 可疑问题：`{len(ranked.suspicious_findings)}`",
        f"- 开放问题：`{len(ranked.open_questions)}`",
        f"- Inline 评论：`{len(ranked.inline_candidates)}`",
        "",
        ranked.summary,
    ]

    if ranked.confirmed_findings:
        lines.extend(["", "### 发现的问题"])
        for finding in ranked.confirmed_findings:
            lines.append(f"- {finding.summary}")

    if ranked.suspicious_findings:
        lines.extend(["", "### 仍需确认"])
        for finding in ranked.suspicious_findings:
            lines.append(f"- {finding.summary}")

    lines.extend(
        [
            "",
            f"<!-- open-review-review-run: {context.review_run_id} -->",
            "<!-- open-review-summary-kind: auto-review -->",
            f"<!-- open-review-head-sha: {context.head_sha} -->",
            f"<!-- open-review-diff-fingerprint: {context.diff_fingerprint} -->",
        ]
    )
    return "\n".join(lines)


async def _publish_review(
    context: ReviewContext,
    ranked: RankedReview,
    lane_results: list[LaneReviewResult],
    publish_service=None,
) -> None:
    with start_open_review_span(
        "open_review.auto_review.publish",
        attributes={
            "open_review.project_id": context.project_id,
            "open_review.mr_iid": context.mr_iid,
            "open_review.review_run_id": context.review_run_id,
            "open_review.confirmed_findings_count": len(ranked.confirmed_findings),
            "open_review.suspicious_findings_count": len(ranked.suspicious_findings),
            "open_review.open_questions_count": len(ranked.open_questions),
            "open_review.inline_comments_count": len(ranked.inline_candidates),
        },
        tags=["auto_review", "publish"],
    ):
        for finding in ranked.inline_candidates:
            if finding.file_path and finding.line:
                body = _format_inline_comment(finding, context.review_run_id)
                if publish_service is not None:
                    await publish_service.publish_inline_comment(
                        op_key=f"inline:{context.head_sha}:{finding.dedupe_key}:{finding.file_path}:{finding.line}",
                        publisher=lambda finding=finding, body=body: post_inline_comment(
                            context.project_id,
                            context.mr_iid,
                            finding.file_path,
                            finding.line,
                            body,
                        ),
                        record={
                            "object_kind": "inline_comment",
                            "mr_iid": context.mr_iid,
                            "file_path": finding.file_path,
                            "line": finding.line,
                            "body_snapshot": body,
                            "marker_map": {
                                "open-review-review-run": context.review_run_id,
                                "open-review-dedupe": finding.dedupe_key,
                                "open-review-head-sha": context.head_sha,
                            },
                        },
                    )
                else:
                    post_inline_comment(
                        context.project_id,
                        context.mr_iid,
                        finding.file_path,
                        finding.line,
                        body,
                    )

        body = _format_summary_comment(context, ranked, lane_results)
        if publish_service is not None:
            await publish_service.publish_mr_note(
                op_key=f"summary:auto-review:{context.head_sha}",
                publisher=lambda: upsert_mr_comment_by_marker(
                    context.project_id,
                    context.mr_iid,
                    body,
                    marker_name="open-review-summary-kind",
                    marker_value="auto-review",
                ),
                record={
                    "object_kind": "mr_note",
                    "mr_iid": context.mr_iid,
                    "body_snapshot": body,
                    "marker_map": {
                        "open-review-review-run": context.review_run_id,
                        "open-review-summary-kind": "auto-review",
                        "open-review-head-sha": context.head_sha,
                        "open-review-diff-fingerprint": context.diff_fingerprint,
                    },
                },
            )
            return

        upsert_mr_comment_by_marker(
            context.project_id,
            context.mr_iid,
            body,
            marker_name="open-review-summary-kind",
            marker_value="auto-review",
        )


async def run_auto_review(
    *,
    project_id: str,
    mr_iid: int,
    repo_dir: str,
    sandbox,
    model_id: str | None = None,
    expected_head_sha: str | None = None,
    publish_service=None,
    runtime_run_id: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> AutoReviewRunResult:
    """Run the staged auto-review workflow for a merge request."""
    config = dict(agent_config or _load_auto_review_agent_config(project_id))
    token = _AUTO_REVIEW_AGENT_CONFIG.set(config)
    try:
        return await _run_auto_review_inner(
            project_id=project_id,
            mr_iid=mr_iid,
            repo_dir=repo_dir,
            sandbox=sandbox,
            model_id=model_id,
            expected_head_sha=expected_head_sha,
            publish_service=publish_service,
            runtime_run_id=runtime_run_id,
        )
    finally:
        _AUTO_REVIEW_AGENT_CONFIG.reset(token)


async def _run_auto_review_inner(
    *,
    project_id: str,
    mr_iid: int,
    repo_dir: str,
    sandbox,
    model_id: str | None = None,
    expected_head_sha: str | None = None,
    publish_service=None,
    runtime_run_id: str | None = None,
) -> AutoReviewRunResult:
    """Run the staged auto-review workflow for a merge request."""
    lock = _REVIEW_LOCKS.setdefault(_review_key(project_id, mr_iid), asyncio.Lock())
    async with lock:
        await raise_if_run_termination_requested(
            run_id=runtime_run_id,
            actor_key=_review_key(project_id, mr_iid),
        )
        context = _call_with_optional_sandbox(
            build_review_context,
            project_id,
            mr_iid,
            repo_dir,
            sandbox=sandbox,
        )
        with start_open_review_span(
            _auto_review_trace_name(context),
            session_id=_review_key(project_id, mr_iid),
            user_id=context.author,
            attributes={
                "open_review.project_id": project_id,
                "open_review.mr_iid": mr_iid,
                "open_review.review_run_id": context.review_run_id,
                "open_review.review_mode": context.review_mode,
                "open_review.head_sha": context.head_sha,
            },
            metadata={
                "changed_files": [item.file_path for item in context.changed_files],
                "diff_range": context.diff_range,
            },
            tags=["auto_review", context.review_mode],
        ):
            if context.skip_reason:
                return AutoReviewRunResult(
                    status="skipped",
                    reason=context.skip_reason,
                    review_run_id=context.review_run_id,
                    review_mode=context.review_mode,
                    compressed_review=context.diff_pack_compressed,
                )
            if expected_head_sha and context.head_sha != expected_head_sha:
                return AutoReviewRunResult(
                    status="skipped",
                    reason="stale_webhook_head_sha",
                    review_run_id=context.review_run_id,
                    review_mode=context.review_mode,
                    compressed_review=context.diff_pack_compressed,
                )
            identity = resolve_bot_identity()
            if identity.identity is None:
                return AutoReviewRunResult(
                    status="failed",
                    reason=f"当前无法解析 GitLab Bot 身份：{identity.error or 'unknown error'}",
                    review_run_id=context.review_run_id,
                    review_mode=context.review_mode,
                    compressed_review=context.diff_pack_compressed,
                )
            seed = build_review_seed_context(context)
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=_review_key(project_id, mr_iid),
            )
            director_kwargs = {
                "context": context,
                "seed": seed,
                "sandbox": sandbox,
                "model_id": model_id,
            }
            if runtime_run_id is not None:
                director_kwargs["runtime_run_id"] = runtime_run_id
            try:
                director_decision = await _run_review_director(**director_kwargs)
            except DirectorReviewFailure as exc:
                return AutoReviewRunResult(
                    status="failed",
                    reason=str(exc),
                    review_run_id=context.review_run_id,
                    review_mode=context.review_mode,
                    compressed_review=context.diff_pack_compressed,
                )
            ranked = _finalize_director_decision(context, director_decision)
            specialist_reports = director_decision.specialist_reports
            if not _head_is_current(project_id, mr_iid, context.head_sha):
                return AutoReviewRunResult(
                    status="skipped",
                    reason="head_sha_changed_during_review",
                    review_run_id=context.review_run_id,
                    review_mode=context.review_mode,
                    compressed_review=context.diff_pack_compressed,
                )
            await raise_if_run_termination_requested(
                run_id=runtime_run_id,
                actor_key=_review_key(project_id, mr_iid),
            )
            if publish_service is None:
                publish_result = _publish_review(context, ranked, specialist_reports)
            else:
                publish_result = _publish_review(
                    context,
                    ranked,
                    specialist_reports,
                    publish_service=publish_service,
                )
            if inspect.isawaitable(publish_result):
                await publish_result
            return AutoReviewRunResult(
                status="published",
                review_run_id=context.review_run_id,
                review_mode=context.review_mode,
                recommendation=ranked.recommendation,
                compressed_review=context.diff_pack_compressed,
                confirmed_findings_count=len(ranked.confirmed_findings),
                suspicious_findings_count=len(ranked.suspicious_findings),
                open_questions_count=len(ranked.open_questions),
                inline_comments_count=len(ranked.inline_candidates),
            )
