from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from os.path import basename
from typing import Any

from langchain_core.messages import AIMessage

from agent.observability import start_open_review_span
from agent.utils.model import (
    extract_model_response_text,
    make_model_from_snapshot,
    resolve_llm_config,
)
from agent.utils.structured_output import SimpleSubagentResult

REPO_ANALYST_NAME = "repo-analyst"
REPO_ANALYST_DESCRIPTION = (
    "Repository-scale analysis agent powered by an RLM runtime. Use it for whole-repo exploration, "
    "feature location, long-context synthesis, and impact-chain analysis. Call with JSON containing "
    "`question` plus optional `file_paths` and `keywords`; matching file content is injected as REPL variables."
)

_BLOCKED_GIT_COMMANDS = (
    "push",
    "commit",
    "merge",
    "rebase",
    "cherry-pick",
    "am",
    "reset",
    "checkout",
    "switch",
    "stash",
    "pull",
    "fetch",
)
_SHELL_SEPARATORS = {"&&", "||", ";", "|"}
_SHELL_SEGMENT_SEPARATORS = {"&&", "||", ";", "|"}
_SHELL_REDIRECT_TOKENS = {">", ">>", ">|", "<>", "<", "<<", "<<<", "&>", "&>>"}
_GIT_OPTION_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_READ_ONLY_SHELL_COMMANDS = {
    "[",
    "basename",
    "cat",
    "cut",
    "dirname",
    "du",
    "false",
    "file",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "readlink",
    "realpath",
    "rg",
    "sort",
    "stat",
    "tail",
    "test",
    "tree",
    "true",
    "uniq",
    "wc",
}
_READ_ONLY_GIT_COMMANDS = {
    "blame",
    "cat-file",
    "describe",
    "diff",
    "for-each-ref",
    "grep",
    "log",
    "ls-files",
    "merge-base",
    "name-rev",
    "rev-list",
    "rev-parse",
    "show",
    "status",
}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_RLM_FILE_READ_LIMIT = 500_000
_RLM_KEYWORD_TOP_FILES = 5
_RLM_MAX_DEPTH = 5
_RLM_MAX_ITERATIONS = 64
_RLM_MAX_TIMEOUT_SECONDS = None
_RLM_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_RLM_DEFAULT_ANSWER_PROMPT = "Please provide a final answer to the user's question based on the information provided."
_OPEN_REVIEW_RLM_DEFAULT_ANSWER_PROMPT = (
    "Provide the final answer to the user's question based on the information already gathered. "
    "Write directly in natural language. Do not use REPL blocks, code fences, FINAL(...), "
    "FINAL_VAR(...), or placeholder variable names."
)
_OPEN_REVIEW_RLM_SYSTEM_PROMPT_SUFFIX = """

Open Review repo-analyst rules:
- This repository task already injects high-value variables. Start by inspecting `diffs`,
  `changed_files_content`, `requested_files_content`, `keyword_top_files_content`,
  `keyword_counts`, and `corpus_manifest`; do not spend repeated turns only printing context shape.
- For code review and impact analysis, you may answer once you have inspected the relevant injected
  diff and file-content variables plus any targeted repository helper output you need.
- Do not output bare placeholder text such as `final_answer` or `FINAL(final_answer)`.
  If you use `FINAL_VAR(final_answer)`, first create `final_answer` in a REPL step.
- Your final answer must contain substantive evidence from the repository context.
"""


@dataclass(frozen=True)
class RepoAnalystQuerySpec:
    question: str
    file_paths: list[str]
    keywords: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "file_paths": list(self.file_paths),
            "keywords": list(self.keywords),
        }


class ReadOnlyRepoAnalysisBackend:
    """Read-only wrapper that keeps shell access while blocking common branch mutations."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.cwd = getattr(backend, "cwd", None)
        self.root_dir = getattr(backend, "root_dir", None)
        self.host_root_dir = getattr(backend, "host_root_dir", None)

    @property
    def id(self) -> str:
        return str(getattr(self._backend, "id", f"repo-analyst:{id(self)}"))

    def ls(self, path: str):
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 4000):
        return self._backend.read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None):
        return self._backend.grep(pattern, path, glob)

    def glob(self, pattern: str, path: str = "/"):
        return self._backend.glob(pattern, path)

    def write(self, file_path: str, content: str):
        del content
        return {"error": "read_only_backend", "path": file_path}

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        del old_string, new_string, replace_all
        return {"error": "read_only_backend", "path": file_path, "occurrences": 0}

    def upload_files(self, files: list[tuple[str, bytes]]):
        return [{"path": path, "error": "permission_denied"} for path, _ in files]

    def download_files(self, paths: list[str]):
        return self._backend.download_files(paths)

    def _blocked_command(self, command: str) -> str | None:
        tokens = _shell_tokens(command)
        for index, token in enumerate(tokens):
            if basename(token).lower() != "git":
                continue
            args = tokens[index + 1 :]
            subcommand = _first_git_subcommand(args)
            if subcommand in _BLOCKED_GIT_COMMANDS:
                return f"git {subcommand}"
        return None

    def _read_only_violation(self, command: str) -> str | None:
        return _read_only_shell_violation(command)

    def execute(self, command: str, *, timeout: int | None = None):
        blocked = self._blocked_command(command)
        if blocked:
            from deepagents.backends.protocol import ExecuteResponse

            return ExecuteResponse(
                output=(
                    "Error: branch-state-changing git commands are blocked in the repo-analyst backend: "
                    f"`{blocked}`."
                ),
                exit_code=126,
                truncated=False,
            )
        violation = self._read_only_violation(command)
        if violation:
            from deepagents.backends.protocol import ExecuteResponse

            return ExecuteResponse(
                output=(
                    "Error: repo-analyst shell access is read-only. "
                    f"Blocked command: {violation}."
                ),
                exit_code=126,
                truncated=False,
            )
        return self._backend.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None):
        return self.execute(command, timeout=timeout)


def _coerce_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or content)
    return str(content)


def _first_git_subcommand(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        token = args[index]
        if token in _SHELL_SEPARATORS:
            return None
        if token in _GIT_OPTION_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_OPTION_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower()
    return None


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEGMENT_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_shell_wrappers(segment: list[str]) -> list[str]:
    index = 0
    while index < len(segment):
        token = segment[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        if token == "env":
            index += 1
            continue
        if token == "command":
            index += 1
            continue
        if token == "timeout":
            index += 1
            if index < len(segment) and segment[index].startswith("-"):
                index += 1
            if index < len(segment):
                index += 1
            continue
        break
    return segment[index:]


def _git_read_only_violation(segment: list[str]) -> str | None:
    subcommand = _first_git_subcommand(segment[1:])
    if not subcommand:
        return "git without a read-only subcommand"
    if subcommand in _BLOCKED_GIT_COMMANDS:
        return f"git {subcommand}"
    if subcommand not in _READ_ONLY_GIT_COMMANDS:
        return f"git {subcommand}"
    return None


def _read_only_shell_violation(command: str) -> str | None:
    if "$(" in command or "`" in command:
        return "shell command substitution"
    tokens = _shell_tokens(command)
    if not tokens:
        return None
    for token in tokens:
        if token in _SHELL_REDIRECT_TOKENS or token.endswith(">"):
            return f"shell redirection `{token}`"
        if token == "&":
            return "background shell execution"
    for raw_segment in _command_segments(tokens):
        segment = _strip_shell_wrappers(raw_segment)
        if not segment:
            continue
        command_name = basename(segment[0]).lower()
        if command_name == "cd":
            continue
        if command_name not in _READ_ONLY_SHELL_COMMANDS:
            return command_name
        if command_name == "git":
            violation = _git_read_only_violation(segment)
            if violation:
                return violation
        if command_name == "sed" and any(arg == "-i" or arg.startswith("-i") or arg == "--in-place" for arg in segment[1:]):
            return "sed in-place edit"
        if command_name == "find" and any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for arg in segment[1:]):
            return "find mutation action"
    return None


def _extract_instruction(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            for item in reversed(messages):
                role = (
                    item.get("role")
                    if isinstance(item, dict)
                    else (getattr(item, "role", None) or getattr(item, "type", None))
                )
                if role not in {"user", "human"}:
                    continue
                content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
                text = _coerce_content_to_text(content).strip()
                if text:
                    return text
        if "input" in payload:
            return _coerce_content_to_text(payload["input"]).strip()
        return _coerce_content_to_text(payload).strip()
    if isinstance(payload, list):
        return "\n".join(_coerce_content_to_text(item) for item in payload if item).strip()
    return str(payload).strip()


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    items: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _query_spec_from_mapping(data: dict[str, Any], fallback_question: str = "") -> RepoAnalystQuerySpec | None:
    nested = data.get("query_spec")
    if isinstance(nested, dict):
        nested_spec = _query_spec_from_mapping(nested, fallback_question=fallback_question)
        if nested_spec is not None:
            return nested_spec

    question = (
        data.get("question")
        or data.get("query")
        or data.get("task")
        or data.get("instruction")
        or fallback_question
    )
    question_text = _coerce_content_to_text(question).strip()
    paths = data.get("file_paths", data.get("paths", data.get("files")))
    keywords = data.get("keywords", data.get("symbols", data.get("terms")))

    if not question_text and paths is None and keywords is None:
        return None
    return RepoAnalystQuerySpec(
        question=question_text,
        file_paths=_coerce_string_list(paths),
        keywords=_coerce_string_list(keywords),
    )


def _extract_query_spec(payload: Any) -> RepoAnalystQuerySpec:
    if isinstance(payload, dict):
        direct = _query_spec_from_mapping(payload)
        if direct is not None and direct.question:
            return direct

        instruction = _extract_instruction(payload)
        try:
            parsed = json.loads(instruction)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            from_text = _query_spec_from_mapping(parsed, fallback_question=instruction)
            if from_text is not None and from_text.question:
                return from_text
        return RepoAnalystQuerySpec(question=instruction, file_paths=[], keywords=[])

    instruction = _extract_instruction(payload)
    return RepoAnalystQuerySpec(question=instruction, file_paths=[], keywords=[])


def _resolve_repo_path(repo_root: str, path: str | None) -> str:
    candidate = (path or "").strip()
    if not candidate or candidate == ".":
        return repo_root
    if os.path.isabs(candidate):
        return candidate
    return f"{repo_root.rstrip('/')}/{candidate.lstrip('./')}"


def _import_rlm_class():
    try:
        from rlm import RLM

        return RLM
    except ModuleNotFoundError as exc:
        if exc.name != "rlm":
            raise
    raise RuntimeError(
        "The `rlm` package is not importable. Install the `rlms` Python package."
    )


def _open_review_rlm_system_prompt() -> str:
    from rlm.utils.prompts import RLM_SYSTEM_PROMPT

    return RLM_SYSTEM_PROMPT + _OPEN_REVIEW_RLM_SYSTEM_PROMPT_SUFFIX


def _result_to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {
            key: _result_to_jsonable(val)
            for key, val in value.__dict__.items()
            if not key.startswith("_")
        }
    return value


def _make_repo_tool_map(
    *,
    backend: ReadOnlyRepoAnalysisBackend,
    file_tool_repo_dir: str,
    shell_repo_dir: str,
    extra_tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def repo_ls(path: str = ".") -> Any:
        """List files or directories relative to the repository root."""
        return _result_to_jsonable(backend.ls(_resolve_repo_path(file_tool_repo_dir, path)))

    def repo_read(path: str, offset: int = 0, limit: int = 4000) -> Any:
        """Read a repository file relative to the repository root."""
        return _result_to_jsonable(backend.read(_resolve_repo_path(file_tool_repo_dir, path), offset, limit))

    def repo_grep(pattern: str, path: str = ".", glob: str | None = None) -> Any:
        """Search repository text with ripgrep-style semantics."""
        return _result_to_jsonable(backend.grep(pattern, _resolve_repo_path(file_tool_repo_dir, path), glob))

    def repo_glob(pattern: str, path: str = ".") -> Any:
        """Glob repository paths relative to the repository root."""
        return _result_to_jsonable(backend.glob(pattern, _resolve_repo_path(file_tool_repo_dir, path)))

    def run_shell_readonly(command: str, timeout: int = 60) -> dict[str, Any]:
        """Run a read-only shell command inside the repository working tree."""
        prefixed = f"cd {shlex.quote(shell_repo_dir)} && {command}"
        result = backend.execute(prefixed, timeout=timeout)
        return {
            "command": prefixed,
            "exit_code": getattr(result, "exit_code", 1),
            "output": getattr(result, "output", ""),
            "truncated": bool(getattr(result, "truncated", False)),
        }

    def git_status() -> dict[str, Any]:
        """Show repository status for the current worktree."""
        return run_shell_readonly("git status --short --branch", timeout=30)

    def git_diff(rev_range: str = "", path: str | None = None) -> dict[str, Any]:
        """Show a git diff for the current repository or a specific path."""
        parts = ["git", "diff", "--unified=3", "--find-renames"]
        if rev_range.strip():
            parts.append(rev_range.strip())
        if path and path.strip():
            parts.extend(["--", shlex.quote(path.strip())])
        return run_shell_readonly(" ".join(parts), timeout=60)

    def git_log(rev_range: str = "", max_count: int = 50) -> dict[str, Any]:
        """Show recent git log entries."""
        parts = ["git", "log", f"--max-count={max_count}", "--stat", "--oneline"]
        if rev_range.strip():
            parts.append(rev_range.strip())
        return run_shell_readonly(" ".join(parts), timeout=60)

    tools: dict[str, Any] = {
        "repo_ls": repo_ls,
        "repo_read": repo_read,
        "repo_grep": repo_grep,
        "repo_glob": repo_glob,
        "run_shell_readonly": run_shell_readonly,
        "git_status": git_status,
        "git_diff": git_diff,
        "git_log": git_log,
    }
    if extra_tools:
        tools.update(extra_tools)
    return tools


def _make_repo_tools(
    *,
    backend: ReadOnlyRepoAnalysisBackend,
    file_tool_repo_dir: str,
    shell_repo_dir: str,
    extra_tools: dict[str, Any] | None = None,
) -> list[Any]:
    return list(
        _make_repo_tool_map(
            backend=backend,
            file_tool_repo_dir=file_tool_repo_dir,
            shell_repo_dir=shell_repo_dir,
            extra_tools=extra_tools,
        ).values()
    )


def _repo_relative_path(repo_root: str, path: str | None) -> str:
    value = (path or "").strip()
    if not value:
        return ""
    root = repo_root.rstrip("/")
    if value == root:
        return "."
    if value.startswith(root + "/"):
        return value[len(root) + 1 :]
    return value.lstrip("./")


def _read_result_content(result: Any) -> tuple[str, str | None]:
    data = _result_to_jsonable(result)
    if isinstance(data, dict):
        error = data.get("error")
        if error:
            return "", str(error)
        file_data = data.get("file_data")
        if isinstance(file_data, dict):
            content = file_data.get("content")
            return str(content or ""), None
        if "content" in data:
            return str(data.get("content") or ""), None
    return "", "content_unavailable"


def _grep_result_matches(result: Any) -> list[dict[str, Any]]:
    data = _result_to_jsonable(result)
    if not isinstance(data, dict) or data.get("error"):
        return []
    matches = data.get("matches")
    if not isinstance(matches, list):
        return []
    return [match for match in matches if isinstance(match, dict)]


def _review_scope_snapshot(tool: Any | None, file_path: str | None = None) -> dict[str, Any]:
    if not callable(tool):
        return {}
    try:
        value = tool(file_path=file_path)
    except TypeError:
        if file_path is not None:
            try:
                value = tool(file_path)
            except TypeError:
                return {}
        else:
            value = tool()
    except Exception as exc:
        return {"error": str(exc)}
    data = _result_to_jsonable(value)
    return data if isinstance(data, dict) else {}


def _changed_paths_from_scope(scope: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in scope.get("changed_files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("file_path") or "").strip()
        if path:
            paths.append(path)
    return _coerce_string_list(paths)


def _file_record(path: str, content: str, source: str, error: str | None = None) -> dict[str, Any]:
    return {
        "path": path,
        "source": source,
        "content": content,
        "chars": len(content),
        "truncated": len(content) >= _RLM_FILE_READ_LIMIT,
        "error": error,
    }


def _build_rlm_variable_payload(
    *,
    query_spec: RepoAnalystQuerySpec,
    context: dict[str, Any],
    tools: dict[str, Any],
    file_tool_repo_dir: str,
) -> dict[str, Any]:
    repo_read = tools["repo_read"]
    repo_grep = tools["repo_grep"]
    review_scope = tools.get("review_scope")
    scope = _review_scope_snapshot(review_scope)
    changed_paths = _changed_paths_from_scope(scope)

    read_cache: dict[str, dict[str, Any]] = {}
    file_sources: dict[str, set[str]] = {}

    def read_file(path: str, source: str) -> dict[str, Any]:
        relative = _repo_relative_path(file_tool_repo_dir, path)
        if not relative:
            return _file_record(path, "", source, error="empty_path")
        file_sources.setdefault(relative, set()).add(source)
        if relative not in read_cache:
            content, error = _read_result_content(repo_read(relative, offset=0, limit=_RLM_FILE_READ_LIMIT))
            read_cache[relative] = _file_record(relative, content, source, error=error)
        else:
            existing = read_cache[relative]
            existing["source"] = ",".join(sorted(file_sources[relative]))
        return read_cache[relative]

    changed_files_content = {
        path: read_file(path, "changed_file") for path in changed_paths
    }
    requested_files_content = {
        path: read_file(path, "requested_file") for path in query_spec.file_paths
    }

    diffs: dict[str, Any] = {}
    for path in changed_paths:
        file_scope = _review_scope_snapshot(review_scope, path)
        if file_scope:
            diffs[path] = {
                "diff": file_scope.get("diff", ""),
                "added_lines": file_scope.get("added_lines", []),
                "status": file_scope.get("status"),
                "old_path": file_scope.get("old_path"),
            }

    keyword_counts: dict[str, dict[str, int]] = {}
    keyword_top_files_content: dict[str, dict[str, dict[str, Any]]] = {}
    for keyword in query_spec.keywords:
        matches = _grep_result_matches(repo_grep(keyword, path="."))
        counts = Counter(
            _repo_relative_path(file_tool_repo_dir, str(match.get("path") or ""))
            for match in matches
        )
        counts.pop("", None)
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        keyword_counts[keyword] = dict(ordered)
        top_records: dict[str, dict[str, Any]] = {}
        for path, _count in ordered[:_RLM_KEYWORD_TOP_FILES]:
            top_records[path] = read_file(path, f"keyword:{keyword}")
        keyword_top_files_content[keyword] = top_records

    all_selected_files_text_parts = []
    selected_manifest = []
    for path, record in read_cache.items():
        selected_manifest.append(
            {
                "path": path,
                "sources": sorted(file_sources.get(path, {record.get("source", "")})),
                "chars": record.get("chars", 0),
                "truncated": record.get("truncated", False),
                "error": record.get("error"),
            }
        )
        all_selected_files_text_parts.append(
            f"\n\n===== FILE: {path} =====\n{record.get('content', '')}"
        )

    corpus_manifest = {
        "task": query_spec.question,
        "scene": context.get("scene"),
        "repo_root": context.get("repo_root") or file_tool_repo_dir,
        "shell_repo_root": context.get("shell_repo_root"),
        "query_spec": query_spec.to_dict(),
        "scope": scope,
        "variables": [
            "task",
            "diffs",
            "changed_files_content",
            "requested_files_content",
            "keyword_top_files_content",
            "keyword_counts",
            "all_selected_files_text",
            "corpus_manifest",
        ],
        "selected_files": selected_manifest,
        "instructions": (
            "Use SHOW_VARS() first, inspect these REPL variables programmatically, "
            "chunk large text with llm_query_batched(), and use rlm_query()/rlm_query_batched() "
            "for subtasks that need their own iterative reasoning."
        ),
    }

    return {
        "task": query_spec.question,
        "diffs": diffs,
        "changed_files_content": changed_files_content,
        "requested_files_content": requested_files_content,
        "keyword_top_files_content": keyword_top_files_content,
        "keyword_counts": keyword_counts,
        "all_selected_files_text": "".join(all_selected_files_text_parts).strip(),
        "corpus_manifest": corpus_manifest,
    }


def _custom_tool_entry(tool: Any, description: str) -> dict[str, Any]:
    return {"tool": tool, "description": description}


def _build_rlm_custom_tools(variables: dict[str, Any], tools: dict[str, Any]) -> dict[str, Any]:
    custom_tools: dict[str, Any] = {
        "task": _custom_tool_entry(variables["task"], "The repository analysis question."),
        "diffs": _custom_tool_entry(variables["diffs"], "MR diff data keyed by changed file path."),
        "changed_files_content": _custom_tool_entry(
            variables["changed_files_content"], "Content records for files changed by the current scope."
        ),
        "requested_files_content": _custom_tool_entry(
            variables["requested_files_content"], "Content records for caller-requested file paths."
        ),
        "keyword_top_files_content": _custom_tool_entry(
            variables["keyword_top_files_content"],
            "For each keyword, content records for the files with the most matches.",
        ),
        "keyword_counts": _custom_tool_entry(
            variables["keyword_counts"], "Keyword hit counts by file path."
        ),
        "all_selected_files_text": _custom_tool_entry(
            variables["all_selected_files_text"],
            "All selected file contents concatenated with file path separators.",
        ),
        "corpus_manifest": _custom_tool_entry(
            variables["corpus_manifest"],
            "Manifest describing the task, available variables, selected files, and scope.",
        ),
    }
    for name, tool in tools.items():
        custom_tools[name] = _custom_tool_entry(
            tool,
            "Read-only repository helper available inside the RLM REPL.",
        )
    return custom_tools


def _is_placeholder_final_answer_text(text: str) -> bool:
    normalized = text.strip().strip("`").strip()
    if normalized == "final_answer":
        return True
    return normalized == "FINAL(final_answer)"


def _environment_final_answer_variable(environment: Any) -> str | None:
    locals_map = getattr(environment, "locals", None)
    if not isinstance(locals_map, dict):
        return None
    if "final_answer" not in locals_map:
        return None
    answer = str(locals_map["final_answer"]).strip()
    if not answer or _is_placeholder_final_answer_text(answer):
        return None
    return str(locals_map["final_answer"])


class _OpenReviewLangChainRLMClient:
    """RLM client adapter that reuses Open Review's configured LangChain model factory."""

    def __init__(
        self,
        *,
        snapshot: dict[str, Any],
        model_id: str | None = None,
        model_name: str | None = None,
        max_tokens: int = 32768,
        temperature: float = 0,
        **kwargs: Any,
    ) -> None:
        del kwargs
        from rlm.core.types import ModelUsageSummary, UsageSummary

        self.snapshot = dict(snapshot)
        self.model_name = model_name or model_id or ""
        self.model_id = model_id or model_name
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._usage_summary_type = UsageSummary
        self._model_usage_summary_type = ModelUsageSummary
        self._models: dict[str, Any] = {}
        self._model_call_counts: dict[str, int] = {}
        self._model_input_tokens: dict[str, int] = {}
        self._model_output_tokens: dict[str, int] = {}
        self._last_input_tokens = 0
        self._last_output_tokens = 0

    def _model_for(self, model: str | None = None) -> Any:
        model_id = model or self.model_id
        cache_key = model_id or "__default__"
        if cache_key not in self._models:
            self._models[cache_key] = make_model_from_snapshot(
                self.snapshot,
                model_id=model_id,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        return self._models[cache_key]

    @staticmethod
    def _prepare_messages(prompt: Any) -> tuple[list[dict[str, Any]], str | None]:
        system = None
        if isinstance(prompt, str):
            return [{"role": "user", "content": prompt}], system
        if not isinstance(prompt, list) or not all(isinstance(item, dict) for item in prompt):
            raise ValueError(f"Invalid prompt type: {type(prompt)}")

        messages: list[dict[str, Any]] = []
        for item in prompt:
            if item.get("role") == "system":
                content = item.get("content")
                system = str(content) if content is not None else None
                continue
            messages.append(dict(item))
        if messages and messages[-1].get("role") == "assistant":
            content = str(messages[-1].get("content") or "").strip()
            if content == _RLM_DEFAULT_ANSWER_PROMPT:
                messages[-1] = {"role": "user", "content": _OPEN_REVIEW_RLM_DEFAULT_ANSWER_PROMPT}
        return messages, system

    @staticmethod
    def _response_text(response: Any) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "thinking":
                    continue
                extracted = extract_model_response_text(item)
                if extracted:
                    parts.append(extracted)
            return "\n".join(parts).strip()
        return extract_model_response_text(response)

    @staticmethod
    def _is_placeholder_final_answer(text: str) -> bool:
        return _is_placeholder_final_answer_text(text)

    @classmethod
    def _needs_response_retry(cls, text: str) -> bool:
        return not text.strip() or cls._is_placeholder_final_answer(text)

    @staticmethod
    def _placeholder_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            *messages,
            {
                "role": "user",
                "content": (
                    "Your previous response was only the placeholder `final_answer`. "
                    "That is invalid. Provide the actual substantive final answer now, grounded in the "
                    "available context and evidence. Do not output bare `final_answer` or "
                    "`FINAL(final_answer)` literally. If you use `FINAL_VAR(final_answer)`, first create "
                    "`final_answer` in a REPL step."
                ),
            },
        ]

    @staticmethod
    def _usage_tokens(response: Any) -> tuple[int, int]:
        usage_metadata = getattr(response, "usage_metadata", None)
        if isinstance(usage_metadata, dict):
            return (
                int(usage_metadata.get("input_tokens", 0) or 0),
                int(usage_metadata.get("output_tokens", 0) or 0),
            )
        response_metadata = getattr(response, "response_metadata", None)
        if isinstance(response_metadata, dict):
            token_usage = response_metadata.get("token_usage")
            if isinstance(token_usage, dict):
                return (
                    int(token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0),
                    int(token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0),
                )
            usage = response_metadata.get("usage")
            if isinstance(usage, dict):
                return (
                    int(usage.get("input_tokens", 0) or 0),
                    int(usage.get("output_tokens", 0) or 0),
                )
        usage = getattr(response, "usage", None)
        return (
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )

    @staticmethod
    def _langchain_messages(messages: list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
        if system:
            return [{"role": "system", "content": system}, *messages]
        return messages

    def _track_usage(self, response: Any, model: str) -> None:
        input_tokens, output_tokens = self._usage_tokens(response)
        self._model_call_counts[model] = self._model_call_counts.get(model, 0) + 1
        self._model_input_tokens[model] = self._model_input_tokens.get(model, 0) + input_tokens
        self._model_output_tokens[model] = self._model_output_tokens.get(model, 0) + output_tokens
        self._last_input_tokens = input_tokens
        self._last_output_tokens = output_tokens

    def completion(self, prompt: Any, model: str | None = None) -> str:
        messages, system = self._prepare_messages(prompt)
        selected_model = model or self.model_name
        model_instance = self._model_for(model)
        langchain_messages = self._langchain_messages(messages, system)
        response = model_instance.invoke(langchain_messages)
        self._track_usage(response, selected_model)
        text = self._response_text(response)
        if self._needs_response_retry(text):
            response = model_instance.invoke(
                self._langchain_messages(self._placeholder_retry_messages(messages), system)
            )
            self._track_usage(response, selected_model)
            text = self._response_text(response)
        return text

    async def acompletion(self, prompt: Any, model: str | None = None) -> str:
        messages, system = self._prepare_messages(prompt)
        selected_model = model or self.model_name
        model_instance = self._model_for(model)
        langchain_messages = self._langchain_messages(messages, system)
        response = await model_instance.ainvoke(langchain_messages)
        self._track_usage(response, selected_model)
        text = self._response_text(response)
        if self._needs_response_retry(text):
            response = await model_instance.ainvoke(
                self._langchain_messages(self._placeholder_retry_messages(messages), system)
            )
            self._track_usage(response, selected_model)
            text = self._response_text(response)
        return text

    def get_usage_summary(self):
        model_summaries = {}
        for model, calls in self._model_call_counts.items():
            model_summaries[model] = self._model_usage_summary_type(
                total_calls=calls,
                total_input_tokens=self._model_input_tokens.get(model, 0),
                total_output_tokens=self._model_output_tokens.get(model, 0),
            )
        return self._usage_summary_type(model_usage_summaries=model_summaries)

    def get_last_usage(self):
        return self._model_usage_summary_type(
            total_calls=1,
            total_input_tokens=self._last_input_tokens,
            total_output_tokens=self._last_output_tokens,
        )


@contextmanager
def _patched_rlm_get_client():
    import rlm.core.rlm as rlm_core

    original_get_client = rlm_core.get_client
    original_find_final_answer = rlm_core.find_final_answer

    def get_client(backend: str, backend_kwargs: dict[str, Any]):
        if backend == "open_review_langchain":
            return _OpenReviewLangChainRLMClient(**backend_kwargs)
        return original_get_client(backend, backend_kwargs)

    def find_final_answer(text: str, environment: Any | None = None) -> str | None:
        answer = original_find_final_answer(text, environment=environment)
        if isinstance(answer, str) and _is_placeholder_final_answer_text(answer):
            return _environment_final_answer_variable(environment)
        return answer

    rlm_core.get_client = get_client
    rlm_core.find_final_answer = find_final_answer
    try:
        yield
    finally:
        rlm_core.get_client = original_get_client
        rlm_core.find_final_answer = original_find_final_answer


def _build_rlm_backend_config(resolved: Any) -> tuple[str, dict[str, Any]]:
    if resolved.provider == "anthropic" and (resolved.base_url or "").rstrip("/") not in {
        "",
        _RLM_DEFAULT_ANTHROPIC_BASE_URL,
    }:
        model_id = str(getattr(resolved, "model_id", "") or f"anthropic:{resolved.model}")
        backend_kwargs: dict[str, Any] = {
            "model_name": model_id,
            "model_id": model_id,
            "snapshot": {
                "LLM_ACTIVE_PROVIDER": "anthropic",
                "LLM_MODEL_ID": model_id,
                "ANTHROPIC_MODEL": resolved.model,
                "ANTHROPIC_API_KEY": resolved.api_key,
                "ANTHROPIC_BASE_URL": resolved.base_url,
            },
        }
        return "open_review_langchain", backend_kwargs

    backend_kwargs = {"model_name": resolved.model}
    if resolved.api_key:
        backend_kwargs["api_key"] = resolved.api_key
    if resolved.provider == "openai" and resolved.base_url:
        backend_kwargs["base_url"] = resolved.base_url
    return resolved.provider, backend_kwargs


@contextmanager
def _configured_provider_env(model_id: str | None):
    from agent.config import settings

    snapshot = settings.current_snapshot().model_dump()
    resolved = resolve_llm_config(snapshot, model_id=model_id)
    previous = {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL"),
    }

    if resolved.provider == "openai":
        if resolved.api_key:
            os.environ["OPENAI_API_KEY"] = resolved.api_key
        if resolved.base_url:
            os.environ["OPENAI_BASE_URL"] = resolved.base_url
    elif resolved.provider == "anthropic":
        if resolved.api_key:
            os.environ["ANTHROPIC_API_KEY"] = resolved.api_key
        if resolved.base_url:
            os.environ["ANTHROPIC_BASE_URL"] = resolved.base_url

    try:
        yield resolved.model_id, resolved
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class RepoAnalystRLMRunner:
    scene: str
    backend: ReadOnlyRepoAnalysisBackend
    repo_dir: str
    file_tool_repo_dir: str
    shell_repo_dir: str
    model_id: str | None = None
    context_payload: dict[str, Any] | None = None
    extra_tools: dict[str, Any] | None = None
    max_tool_calls: int = 80

    def _build_context_payload(self) -> dict[str, Any]:
        payload = {
            "scene": self.scene,
            "repo_root": self.file_tool_repo_dir,
            "shell_repo_root": self.shell_repo_dir,
        }
        if self.context_payload:
            payload.update(self.context_payload)
        return payload

    def run(self, *, instruction: str, context: dict[str, Any], config: dict | None = None) -> SimpleSubagentResult:
        del config
        tools = _make_repo_tool_map(
            backend=self.backend,
            file_tool_repo_dir=self.file_tool_repo_dir,
            shell_repo_dir=self.shell_repo_dir,
            extra_tools=self.extra_tools,
        )
        query_spec = _query_spec_from_mapping(context, fallback_question=instruction) or RepoAnalystQuerySpec(
            question=instruction,
            file_paths=[],
            keywords=[],
        )
        variables = _build_rlm_variable_payload(
            query_spec=query_spec,
            context=context,
            tools=tools,
            file_tool_repo_dir=self.file_tool_repo_dir,
        )
        custom_tools = _build_rlm_custom_tools(variables, tools)

        with _configured_provider_env(self.model_id) as (_resolved_model_id, resolved):
            RLM = _import_rlm_class()
            backend, backend_kwargs = _build_rlm_backend_config(resolved)

            with _patched_rlm_get_client():
                rlm = RLM(
                    backend=backend,
                    backend_kwargs=backend_kwargs,
                    environment="local",
                    max_depth=_RLM_MAX_DEPTH,
                    max_iterations=_RLM_MAX_ITERATIONS,
                    max_timeout=_RLM_MAX_TIMEOUT_SECONDS,
                    custom_system_prompt=_open_review_rlm_system_prompt(),
                    custom_tools=custom_tools,
                )
                result = rlm.completion(variables["corpus_manifest"], root_prompt=query_spec.question)
        return SimpleSubagentResult(result=str(getattr(result, "response", result)))

    async def arun(
        self,
        *,
        instruction: str,
        context: dict[str, Any],
        config: dict | None = None,
    ) -> SimpleSubagentResult:
        return await asyncio.to_thread(self.run, instruction=instruction, context=context, config=config)


@dataclass
class RepoAnalystSubagentRunnable:
    runner: Any
    name: str = REPO_ANALYST_NAME

    @staticmethod
    def _configurable(config: dict | None) -> dict[str, Any]:
        if not isinstance(config, dict):
            return {}
        configurable = config.get("configurable")
        return configurable if isinstance(configurable, dict) else {}

    def _scene(self, context: dict[str, Any]) -> str:
        value = getattr(self.runner, "scene", None) or context.get("scene") or "repo"
        return str(value).strip() or "repo"

    def _span_attributes(self, context: dict[str, Any], config: dict | None) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "open_review.rlm": True,
            "open_review.scene": self._scene(context),
            "open_review.subagent_type": self.name,
        }
        model_id = getattr(self.runner, "model_id", None)
        if model_id:
            attributes["open_review.model_id"] = str(model_id)
        for source_key, attr_key in (
            ("project_id", "open_review.project_id"),
            ("mr_iid", "open_review.mr_iid"),
            ("run_id", "open_review.run_id"),
            ("review_run_id", "open_review.review_run_id"),
            ("stage", "open_review.stage"),
            ("lane", "open_review.lane"),
        ):
            value = context.get(source_key)
            if value is not None:
                attributes[attr_key] = value
        configurable = self._configurable(config)
        thread_id = configurable.get("thread_id")
        if thread_id:
            attributes["open_review.thread_id"] = str(thread_id)
        return attributes

    @staticmethod
    def _to_state(response: Any, name: str) -> tuple[SimpleSubagentResult, dict[str, Any]]:
        structured = (
            response
            if isinstance(response, SimpleSubagentResult)
            else SimpleSubagentResult.model_validate({"result": str(response)})
        )
        state = {
            "messages": [AIMessage(content=structured.result, name=name)],
            "structured_response": structured,
        }
        return structured, state

    async def ainvoke(self, payload: Any, config: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        query_spec = _extract_query_spec(payload)
        instruction = query_spec.question
        context = self.runner._build_context_payload() if hasattr(self.runner, "_build_context_payload") else {}
        context["query_spec"] = query_spec.to_dict()
        with start_open_review_span(
            f"open_review.{self._scene(context)}.subagent.{self.name}",
            attributes=self._span_attributes(context, config),
            metadata=None,
            tags=[self._scene(context), "subagent", self.name, "rlm"],
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input({"instruction": instruction, "context": context})
            try:
                response = await self.runner.arun(instruction=instruction, context=context, config=config)
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "instruction_present": bool(instruction),
                    },
                )
                raise
            structured, state = self._to_state(response, self.name)
            trace_ctx.add_event("invoke_completed", {"result_present": bool(structured.result)})
            trace_ctx.set_output(structured.model_dump(mode="json"))
            return state

    def invoke(self, payload: Any, config: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        query_spec = _extract_query_spec(payload)
        instruction = query_spec.question
        context = self.runner._build_context_payload() if hasattr(self.runner, "_build_context_payload") else {}
        context["query_spec"] = query_spec.to_dict()
        with start_open_review_span(
            f"open_review.{self._scene(context)}.subagent.{self.name}",
            attributes=self._span_attributes(context, config),
            metadata=None,
            tags=[self._scene(context), "subagent", self.name, "rlm"],
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input({"instruction": instruction, "context": context})
            try:
                response = self.runner.run(instruction=instruction, context=context, config=config)
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "instruction_present": bool(instruction),
                    },
                )
                raise
            structured, state = self._to_state(response, self.name)
            trace_ctx.add_event("invoke_completed", {"result_present": bool(structured.result)})
            trace_ctx.set_output(structured.model_dump(mode="json"))
            return state


def build_repo_analyst_subagent(
    *,
    scene: str,
    backend: Any,
    repo_dir: str,
    file_tool_repo_dir: str,
    shell_repo_dir: str,
    model_id: str | None = None,
    description: str = REPO_ANALYST_DESCRIPTION,
    context_payload: dict[str, Any] | None = None,
    extra_tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    read_only_backend = ReadOnlyRepoAnalysisBackend(backend)
    runner = RepoAnalystRLMRunner(
        scene=scene,
        backend=read_only_backend,
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        shell_repo_dir=shell_repo_dir,
        model_id=model_id,
        context_payload=context_payload,
        extra_tools=extra_tools,
    )
    return {
        "name": REPO_ANALYST_NAME,
        "description": description,
        "runnable": RepoAnalystSubagentRunnable(runner=runner),
    }
