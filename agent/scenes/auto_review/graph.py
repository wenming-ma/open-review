"""Agent builders for the autonomous auto-review workflow."""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from agent.config import settings
from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.observability import start_open_review_span
from agent.rlm import REPO_ANALYST_DESCRIPTION, build_repo_analyst_subagent
from agent.runtime.termination import RunTerminationMiddleware
from agent.sandbox.manager import sandbox_file_tool_path, sandbox_host_path, sandbox_shell_path
from agent.scenes.auto_review.middleware import AutoReviewRawRecordMiddleware
from agent.scenes.auto_review.models import ChiefReviewDecision, ReviewContext
from agent.scenes.auto_review.prompts import (
    AUTO_REVIEW_INVESTIGATION_SUBAGENT_DESCRIPTIONS,
    AUTO_REVIEW_SPECIALIST_DESCRIPTIONS,
    build_auto_review_investigation_subagent_prompt,
    build_auto_review_specialist_prompt,
    get_auto_review_director_prompt,
)
from agent.scenes.auto_review.scope import authoritative_scope_summary, review_scope_snapshot
from agent.scenes.auto_review.static_workbench import build_static_workbench_tools
from agent.selfevolution.assets import visible_skill_source_roots
from agent.utils.model import make_model
from agent.utils.structured_output import (
    SimpleStructuredSubagentRunnable,
    SimpleSubagentResult,
    make_structured_response_format,
)

_BLOCKED_GIT_SUBCOMMANDS = (
    "commit",
    "push",
    "checkout",
    "switch",
    "reset",
    "merge",
    "rebase",
    "stash",
    "pull",
    "fetch",
)
_BLOCKED_GIT_RE = re.compile(
    r"(^|[;&|]\s*|\&\&\s*|\|\|\s*)git\b(?P<body>[^;&|]*)",
    re.IGNORECASE,
)


def _result_error(result) -> str | None:
    if isinstance(result, dict):
        value = result.get("error")
        return str(value) if value else None
    value = getattr(result, "error", None)
    return str(value) if value else None


def _blocked_git_command(command: str) -> str | None:
    for match in _BLOCKED_GIT_RE.finditer(command):
        body = match.group("body") or ""
        lowered = body.lower()
        for subcommand in _BLOCKED_GIT_SUBCOMMANDS:
            if re.search(rf"(^|\s){re.escape(subcommand)}(\s|$)", lowered):
                return subcommand
    return None


def _skill_sources(sandbox: SandboxBackendProtocol, repo_dir: str) -> list[str]:
    del repo_dir
    return visible_skill_source_roots("auto_review", sandbox)


def _attach_system_prompt(agent: object, system_prompt: str) -> object:
    try:
        agent.open_review_system_prompt = system_prompt
    except Exception:
        pass
    return agent


def _termination_middleware(
    review_context: ReviewContext | None,
    runtime_run_id: str | None,
) -> list[object]:
    if not runtime_run_id:
        return []
    actor_key = ""
    if review_context is not None:
        actor_key = f"{review_context.project_id}!{review_context.mr_iid}"
    return [
        RunTerminationMiddleware(
            run_id=runtime_run_id,
            actor_key=actor_key,
        )
    ]


class SemanticToolFailure(RuntimeError):
    """Structured semantic failure surfaced as a real tool error."""


@dataclass
class _ObservedSubagentRunnable:
    runnable: Any
    span_name: str
    tags: list[str]
    static_attributes: dict[str, Any]

    @staticmethod
    def _configurable(config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            return {}
        configurable = config.get("configurable")
        return configurable if isinstance(configurable, dict) else {}

    def _attributes(self, config: Any) -> dict[str, Any]:
        configurable = self._configurable(config)
        attributes = dict(self.static_attributes)
        for source_key, attr_key in (
            ("project_id", "open_review.project_id"),
            ("mr_iid", "open_review.mr_iid"),
            ("review_run_id", "open_review.review_run_id"),
        ):
            value = configurable.get(source_key)
            if value is not None:
                attributes[attr_key] = value
        return attributes

    @staticmethod
    def _payload_keys(payload: Any) -> list[str] | None:
        if isinstance(payload, dict):
            return sorted(payload.keys())
        return None

    async def ainvoke(self, payload: Any, config: Any | None = None, **kwargs: Any) -> Any:
        with start_open_review_span(
            self.span_name,
            attributes=self._attributes(config),
            metadata=None,
            tags=self.tags,
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input(payload)
            try:
                result = await self.runnable.ainvoke(payload, config=config, **kwargs)
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "payload_keys": self._payload_keys(payload),
                    },
                )
                raise
            trace_ctx.add_event(
                "invoke_completed",
                {
                    "payload_keys": self._payload_keys(result),
                    "structured_response_present": isinstance(result, dict)
                    and result.get("structured_response") is not None,
                },
            )
            trace_ctx.set_output(result)
            return result

    def invoke(self, payload: Any, config: Any | None = None, **kwargs: Any) -> Any:
        with start_open_review_span(
            self.span_name,
            attributes=self._attributes(config),
            metadata=None,
            tags=self.tags,
            span_kind="agent",
        ) as trace_ctx:
            trace_ctx.set_input(payload)
            try:
                invoke = getattr(self.runnable, "invoke", None)
                if callable(invoke):
                    result = invoke(payload, config=config, **kwargs)
                else:
                    ainvoke = getattr(self.runnable, "ainvoke", None)
                    if callable(ainvoke):
                        result = ainvoke(payload, config=config, **kwargs)
                        if inspect.isawaitable(result):
                            raise RuntimeError("sync invoke is not supported for async-only observed subagents")
                    else:
                        raise AttributeError("wrapped runnable does not implement invoke or ainvoke")
            except Exception as exc:
                trace_ctx.record_exception(exc)
                trace_ctx.set_error_status(str(exc))
                trace_ctx.add_event(
                    "invoke_failed",
                    {
                        "error_type": exc.__class__.__name__,
                        "payload_keys": self._payload_keys(payload),
                    },
                )
                raise
            trace_ctx.add_event(
                "invoke_completed",
                {
                    "payload_keys": self._payload_keys(result),
                    "structured_response_present": isinstance(result, dict)
                    and result.get("structured_response") is not None,
                },
            )
            trace_ctx.set_output(result)
            return result


class AutoReviewExecutionBackend(SandboxBackendProtocol):
    """Writable auto-review backend with git-state mutation guards."""

    def __init__(
        self,
        backend: SandboxBackendProtocol,
        *,
        repo_dir: str,
        review_context: ReviewContext | None = None,
    ) -> None:
        self._backend = backend
        self._repo_dir = repo_dir
        self.review_context = review_context
        self.shell_repo_dir = sandbox_shell_path(backend, repo_dir)
        self.file_tool_repo_dir = sandbox_file_tool_path(backend, repo_dir)
        self.cwd = getattr(backend, "cwd", None)
        self.root_dir = getattr(backend, "root_dir", None)
        self.host_root_dir = getattr(backend, "host_root_dir", None)
        self.tool_error_count = 0
        self.semantic_failure_count = 0
        self.failure_reasons: list[str] = []

    @property
    def id(self) -> str:
        return str(getattr(self._backend, "id", f"auto-review:{id(self)}"))

    def _record_failure(self, kind: str, reason: str) -> None:
        if kind == "tool":
            self.tool_error_count += 1
        else:
            self.semantic_failure_count += 1
        normalized = reason.strip()
        if normalized:
            self.failure_reasons.append(normalized)

    @staticmethod
    def _is_recoverable_semantic_failure(reason: str) -> bool:
        lowered = reason.lower()
        return any(
            marker in lowered
            for marker in (
                "file_not_found",
                "path_not_found",
                "already exists",
                "git_internal_binary_unsupported",
                "git_metadata_use_git_command",
            )
        )

    def _raise_semantic_failure(self, reason: str) -> None:
        if not self._is_recoverable_semantic_failure(reason):
            self._record_failure("semantic", reason)
        raise SemanticToolFailure(reason)

    @staticmethod
    def _gitfile_target(gitfile_path: str) -> str | None:
        try:
            payload = Path(gitfile_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not payload.lower().startswith("gitdir:"):
            return None
        target = payload.partition(":")[2].strip()
        if not target:
            return None
        if os.path.isabs(target):
            return os.path.normpath(target)
        return os.path.normpath(os.path.join(os.path.dirname(gitfile_path), target))

    @staticmethod
    def _git_common_dir(gitdir: str) -> str | None:
        commondir_path = os.path.join(gitdir, "commondir")
        if not os.path.isfile(commondir_path):
            return None
        try:
            payload = Path(commondir_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not payload:
            return None
        if os.path.isabs(payload):
            return os.path.normpath(payload)
        return os.path.normpath(os.path.join(gitdir, payload))

    @staticmethod
    def _short_execute_error(output: str) -> str:
        lines = []
        for raw in output.splitlines():
            line = raw.strip()
            if not line or line.startswith("Exit code:"):
                continue
            if line.startswith("[stderr] "):
                line = line[len("[stderr] ") :]
            lines.append(line)
        return lines[0] if lines else output.strip() or "tool_failed"

    def _resolve_git_metadata_host_path(self, host_path: str) -> tuple[str | None, str | None]:
        if host_path.endswith("/.git") and os.path.isfile(host_path):
            return host_path, None

        marker = "/.git/"
        if marker not in host_path:
            return host_path, None

        repo_or_worktree_root, subpath = host_path.split(marker, 1)
        git_root = os.path.join(repo_or_worktree_root, ".git")

        if os.path.isfile(git_root):
            gitdir = self._gitfile_target(git_root)
            if gitdir is None:
                return host_path, None
            if subpath == "index":
                return None, f"git_internal_binary_unsupported:{subpath}"
            candidate = os.path.join(gitdir, subpath)
            if os.path.exists(candidate):
                return candidate, None
            common_dir = self._git_common_dir(gitdir)
            if common_dir is not None:
                shared = os.path.join(common_dir, subpath)
                if os.path.exists(shared):
                    return shared, None
                if subpath.startswith("refs/") and os.path.exists(os.path.join(common_dir, "packed-refs")):
                    return None, f"git_metadata_use_git_command:{subpath}"
            if subpath.startswith(("refs/", "config", "packed-refs")):
                return None, f"git_metadata_use_git_command:{subpath}"
            return host_path, None

        if os.path.isdir(git_root):
            if os.path.exists(host_path):
                return host_path, None
            if subpath.startswith("refs/") and os.path.exists(os.path.join(git_root, "packed-refs")):
                return None, f"git_metadata_use_git_command:{subpath}"

        return host_path, None

    def _normalize_path(self, path: str | None, *, default_to_repo: bool = False) -> str | None:
        candidate = (path or "").strip()
        if not candidate:
            candidate = self.file_tool_repo_dir if default_to_repo else ""
        if not candidate:
            return None
        if not os.path.isabs(candidate):
            candidate = os.path.join(self._repo_dir, candidate)
        normalized = sandbox_file_tool_path(self, candidate)
        host_path = sandbox_host_path(self, normalized)
        resolved_host_path, error = self._resolve_git_metadata_host_path(host_path)
        if error:
            self._raise_semantic_failure(error)
        if resolved_host_path is None:
            return normalized
        return sandbox_file_tool_path(self, resolved_host_path)

    def _track_result(self, result, *, opname: str):
        error = _result_error(result)
        if error:
            self._raise_semantic_failure(f"{opname}:{error}")
        return result

    def ls(self, path: str) -> LsResult:
        normalized = self._normalize_path(path, default_to_repo=True)
        try:
            result = self._backend.ls(normalized)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"ls:{type(exc).__name__}")
            raise
        return self._track_result(result, opname="ls")

    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        normalized = self._normalize_path(file_path, default_to_repo=True)
        try:
            result = self._backend.read(normalized, offset, limit)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"read:{type(exc).__name__}")
            raise
        return self._track_result(result, opname="read")

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(file_path, offset, limit)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        normalized = self._normalize_path(path, default_to_repo=True)
        try:
            result = self._backend.glob(pattern, normalized)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"glob:{type(exc).__name__}")
            raise
        return self._track_result(result, opname="glob")

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        return self.glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        normalized = self._normalize_path(file_path, default_to_repo=True)
        try:
            result = self._backend.write(normalized, content)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"write:{type(exc).__name__}")
            raise
        return self._track_result(result, opname="write")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        normalized = self._normalize_path(file_path, default_to_repo=True)
        try:
            result = self._backend.edit(normalized, old_string, new_string, replace_all)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"edit:{type(exc).__name__}")
            raise
        return self._track_result(result, opname="edit")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        normalized = [(self._normalize_path(path, default_to_repo=True) or path, data) for path, data in files]
        try:
            result = self._backend.upload_files(normalized)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"upload_files:{type(exc).__name__}")
            raise
        error = next((item.error for item in result if getattr(item, "error", None)), None)
        if error:
            self._raise_semantic_failure(f"upload_files:{error}")
        return result

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        normalized = [self._normalize_path(path, default_to_repo=True) or path for path in paths]
        return self._backend.download_files(normalized)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.download_files(paths)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        normalized = self._normalize_path(path, default_to_repo=True)
        pattern_b64 = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
        path_b64 = base64.b64encode((normalized or "").encode("utf-8")).decode("ascii")
        glob_b64 = base64.b64encode((glob or "").encode("utf-8")).decode("ascii")
        script = textwrap.dedent(
            f"""\
            python3 - <<'PY'
            import base64
            import fnmatch
            import json
            import os
            import sys

            pattern = base64.b64decode("{pattern_b64}").decode("utf-8")
            target = base64.b64decode("{path_b64}").decode("utf-8")
            glob_pattern = base64.b64decode("{glob_b64}").decode("utf-8") or None

            if not target or not os.path.exists(target):
                print("path_not_found", file=sys.stderr)
                raise SystemExit(3)

            def include(path: str, rel: str, name: str) -> bool:
                if not glob_pattern:
                    return True
                return (
                    fnmatch.fnmatch(rel, glob_pattern)
                    or fnmatch.fnmatch(name, glob_pattern)
                    or fnmatch.fnmatch(path, glob_pattern)
                )

            def emit(path: str) -> None:
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        for index, line in enumerate(handle, start=1):
                            if pattern in line:
                                print(
                                    json.dumps(
                                        {{
                                            "path": path,
                                            "line": index,
                                            "text": line.rstrip("\\n"),
                                        }},
                                        ensure_ascii=True,
                                    )
                                )
                except OSError:
                    return

            if os.path.isfile(target):
                emit(target)
            else:
                for root, _dirs, files in os.walk(target):
                    for name in files:
                        full_path = os.path.join(root, name)
                        rel_path = os.path.relpath(full_path, target)
                        if include(full_path, rel_path, name):
                            emit(full_path)
            PY"""
        )
        try:
            result = self._backend.execute(script)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"grep:{type(exc).__name__}")
            raise
        if result.exit_code != 0:
            self._raise_semantic_failure(f"grep:{self._short_execute_error(result.output)}")

        matches = []
        output = result.output.strip()
        if not output:
            return GrepResult(matches=[])
        for line in output.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            matches.append(
                {
                    "path": payload["path"],
                    "line": int(payload["line"]),
                    "text": payload["text"],
                }
            )
        return GrepResult(matches=matches)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self.grep(pattern, path, glob)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        blocked = _blocked_git_command(command)
        if blocked:
            raise PermissionError(f"git_state_mutation_blocked:{blocked}")
        try:
            return self._backend.execute(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("tool", f"execute:{type(exc).__name__}")
            raise

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)


# Backwards-compatible alias for tests and older imports.
AutoReviewLaneBackend = AutoReviewExecutionBackend


@dataclass
class AutoReviewDirectorHarness:
    agent: object
    director_backend: AutoReviewExecutionBackend
    specialist_backends: dict[str, AutoReviewExecutionBackend]
    shell_repo_dir: str
    file_tool_repo_dir: str


def _build_review_scope_tool(review_context: ReviewContext | None):
    def review_scope(file_path: str | None = None) -> dict[str, Any]:
        """Read the authoritative MR scope frozen by the orchestrator.

        Use this as the source of truth for base/head SHAs, diff ranges, changed-file
        status, and per-file diff content. If a caller summary conflicts with this
        snapshot, trust this tool.
        """

        return review_scope_snapshot(review_context, file_path=file_path)

    return review_scope


def _build_static_workbench_tool_map(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    review_context: ReviewContext | None,
) -> dict[str, Any]:
    tools = build_static_workbench_tools(backend, repo_dir, review_context)
    return {tool.__name__: tool for tool in tools}


def _build_git_inspector_agent(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    *,
    review_context: ReviewContext | None = None,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    shell_repo_dir = sandbox_shell_path(backend, repo_dir)
    file_tool_repo_dir = sandbox_file_tool_path(backend, repo_dir)
    review_scope_tool = _build_review_scope_tool(review_context)
    return create_deep_agent(
        model=model,
        system_prompt=build_auto_review_investigation_subagent_prompt(
            repo_dir=shell_repo_dir,
            file_tool_repo_dir=file_tool_repo_dir,
            subagent_type="git-inspector",
            authoritative_scope_summary=authoritative_scope_summary(review_context),
        ),
        tools=[review_scope_tool],
        backend=backend,
        middleware=[
            *_termination_middleware(review_context, runtime_run_id),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(backend, repo_dir),
        response_format=make_structured_response_format(SimpleSubagentResult),
        name="git-inspector",
    )


def _observed_compiled_subagent(
    *,
    name: str,
    description: str,
    runnable: Any,
    parent_role: str,
    lane: str | None = None,
    static_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_attributes: dict[str, Any] = {
        "open_review.parent_role": parent_role,
        "open_review.subagent_type": name,
    }
    if static_attributes:
        merged_attributes.update(static_attributes)
    tags = ["auto_review", "subagent"]
    if lane is not None:
        merged_attributes["open_review.specialist_lane"] = lane
        tags.append(lane)
    return {
        "name": name,
        "description": description,
        "runnable": _ObservedSubagentRunnable(
            runnable=SimpleStructuredSubagentRunnable(runnable=runnable, name=name),
            span_name=f"open_review.auto_review.subagent.{name}",
            tags=tags,
            static_attributes=merged_attributes,
        ),
    }


def _build_auto_review_investigation_subagent(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    subagent_type: str,
    *,
    lane: str | None = None,
    review_context: ReviewContext | None = None,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    if subagent_type == "git-inspector":
        runnable = _build_git_inspector_agent(
            backend,
            repo_dir,
            review_context=review_context,
            model_id=model_id,
            runtime_run_id=runtime_run_id,
        )
    else:
        model = make_model(model_id, temperature=0, max_tokens=16_000)
        shell_repo_dir = sandbox_shell_path(backend, repo_dir)
        file_tool_repo_dir = sandbox_file_tool_path(backend, repo_dir)
        review_scope_tool = _build_review_scope_tool(review_context)
        workbench_tools = list(_build_static_workbench_tool_map(backend, repo_dir, review_context).values())
        runnable = create_deep_agent(
            model=model,
            tools=[review_scope_tool, *workbench_tools],
            system_prompt=build_auto_review_investigation_subagent_prompt(
                repo_dir=shell_repo_dir,
                file_tool_repo_dir=file_tool_repo_dir,
                subagent_type=subagent_type,
                authoritative_scope_summary=authoritative_scope_summary(review_context),
            ),
            backend=backend,
            middleware=[
                *_termination_middleware(review_context, runtime_run_id),
                StructuredOutputRetryMiddleware(),
                ModelRetryMiddleware(),
                ToolErrorMiddleware(),
            ],
            skills=_skill_sources(backend, repo_dir),
            response_format=make_structured_response_format(SimpleSubagentResult),
        )
    return _observed_compiled_subagent(
        name=subagent_type,
        description=AUTO_REVIEW_INVESTIGATION_SUBAGENT_DESCRIPTIONS[subagent_type],
        runnable=runnable,
        parent_role="specialist",
        lane=lane,
    )


def build_auto_review_specialist_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    lane: str,
    model_id: str | None = None,
    review_context: ReviewContext | None = None,
    runtime_run_id: str | None = None,
):
    """Build a writable specialist agent for one review discipline."""
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    effective_model_id = model_id or settings.LLM_MODEL_ID
    shell_repo_dir = sandbox_shell_path(sandbox, repo_dir)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    review_scope_tool = _build_review_scope_tool(review_context)
    workbench_tools = list(_build_static_workbench_tool_map(sandbox, repo_dir, review_context).values())
    subagents = [
        _build_auto_review_investigation_subagent(
            sandbox,
            repo_dir,
            subagent_type,
            lane=lane,
            review_context=review_context,
            model_id=model_id,
            runtime_run_id=runtime_run_id,
        )
        for subagent_type in (
            "git-inspector",
            "trace-impact",
            "counterexample",
        )
    ]
    system_prompt = build_auto_review_specialist_prompt(
        shell_repo_dir,
        file_tool_repo_dir,
        lane,
        authoritative_scope_summary(review_context),
    )
    specialist_middleware: list[object] = []
    if review_context is not None:
        specialist_middleware.append(
            AutoReviewRawRecordMiddleware(
                context=review_context,
                runtime_run_id=runtime_run_id,
                record_kind=f"auto_review.specialist.{lane}",
                thread_id=f"auto_review:{review_context.project_id}!{review_context.mr_iid}:{review_context.review_run_id}:specialist:{lane}",
                system_prompt=system_prompt,
                metadata={"lane": lane},
            )
        )
    agent = create_deep_agent(
        model=model,
        tools=[review_scope_tool, *workbench_tools],
        system_prompt=system_prompt,
        backend=sandbox,
        middleware=[
            *_termination_middleware(review_context, runtime_run_id),
            *specialist_middleware,
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(sandbox, repo_dir),
        subagents=subagents,
        response_format=make_structured_response_format(SimpleSubagentResult),
    )
    wrapped = _ObservedSubagentRunnable(
        runnable=agent,
        span_name=f"open_review.auto_review.specialist.{lane}",
        tags=["auto_review", "specialist", lane],
        static_attributes={
            "open_review.model_id": effective_model_id,
            "open_review.parent_role": "director",
            "open_review.specialist_lane": lane,
        },
    )
    return _attach_system_prompt(wrapped, system_prompt)


def build_auto_review_director_harness(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    model_id: str | None = None,
    review_context: ReviewContext | None = None,
    runtime_run_id: str | None = None,
) -> AutoReviewDirectorHarness:
    """Build the Director agent plus per-specialist backend trackers."""
    effective_model_id = model_id or settings.LLM_MODEL_ID
    director_prompt = get_auto_review_director_prompt()
    director_backend = AutoReviewExecutionBackend(sandbox, repo_dir=repo_dir, review_context=review_context)
    shell_repo_dir = sandbox_shell_path(sandbox, repo_dir)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    review_scope_tool = _build_review_scope_tool(review_context)
    director_workbench_tools = _build_static_workbench_tool_map(
        director_backend,
        repo_dir,
        review_context,
    )
    specialist_backends = {
        lane: AutoReviewExecutionBackend(sandbox, repo_dir=repo_dir, review_context=review_context)
        for lane in AUTO_REVIEW_SPECIALIST_DESCRIPTIONS
    }
    specialist_subagents = [
        _observed_compiled_subagent(
            name=lane,
            description=description,
            runnable=build_auto_review_specialist_agent(
                specialist_backends[lane],
                repo_dir,
                lane,
                model_id=model_id,
                review_context=review_context,
                runtime_run_id=runtime_run_id,
            ),
            parent_role="director",
            static_attributes={"open_review.model_id": effective_model_id},
        )
        for lane, description in AUTO_REVIEW_SPECIALIST_DESCRIPTIONS.items()
    ]
    specialist_subagents.append(
        _observed_compiled_subagent(
            name="git-inspector",
            description=AUTO_REVIEW_INVESTIGATION_SUBAGENT_DESCRIPTIONS["git-inspector"],
            runnable=_build_git_inspector_agent(
                director_backend,
                repo_dir,
                review_context=review_context,
                model_id=model_id,
                runtime_run_id=runtime_run_id,
            ),
            parent_role="director",
            static_attributes={"open_review.model_id": effective_model_id},
        )
    )
    specialist_subagents.append(
        _observed_compiled_subagent(
            name="repo-analyst",
            description=REPO_ANALYST_DESCRIPTION,
            runnable=build_repo_analyst_subagent(
                scene="auto_review",
                backend=director_backend,
                repo_dir=repo_dir,
                file_tool_repo_dir=file_tool_repo_dir,
                shell_repo_dir=shell_repo_dir,
                model_id=model_id,
                context_payload={
                    "project_id": review_context.project_id if review_context is not None else "",
                    "mr_iid": review_context.mr_iid if review_context is not None else None,
                    "review_run_id": review_context.review_run_id if review_context is not None else "",
                    "lane": "director",
                },
                extra_tools={"review_scope": review_scope_tool, **director_workbench_tools},
            )["runnable"],
            parent_role="director",
            static_attributes={"open_review.model_id": effective_model_id},
        )
    )
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    director_agent = create_deep_agent(
        model=model,
        tools=[review_scope_tool, *director_workbench_tools.values()],
        system_prompt=director_prompt,
        backend=director_backend,
        middleware=[
            *_termination_middleware(review_context, runtime_run_id),
            *(
                [
                    AutoReviewRawRecordMiddleware(
                        context=review_context,
                        runtime_run_id=runtime_run_id,
                        record_kind="auto_review.director",
                        thread_id=f"auto_review:{review_context.project_id}!{review_context.mr_iid}:{review_context.review_run_id}:director",
                        system_prompt=director_prompt,
                        metadata={"role": "director"},
                    )
                ]
                if review_context is not None
                else []
            ),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(director_backend, repo_dir),
        response_format=make_structured_response_format(ChiefReviewDecision),
        subagents=specialist_subagents,
    )
    director_agent = _attach_system_prompt(director_agent, director_prompt)
    return AutoReviewDirectorHarness(
        agent=director_agent,
        director_backend=director_backend,
        specialist_backends=specialist_backends,
        shell_repo_dir=shell_repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
    )


def build_auto_review_director_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    model_id: str | None = None,
    review_context: ReviewContext | None = None,
    runtime_run_id: str | None = None,
):
    """Build the Director agent used by the staged auto-review orchestrator."""
    return build_auto_review_director_harness(
        sandbox=sandbox,
        repo_dir=repo_dir,
        model_id=model_id,
        review_context=review_context,
        runtime_run_id=runtime_run_id,
    ).agent


def build_auto_review_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    source_branch: str,
    target_branch: str,
    model_id: str | None = None,
    review_context: ReviewContext | None = None,
    runtime_run_id: str | None = None,
):
    """LangGraph compatibility wrapper returning the Director agent."""
    del source_branch, target_branch
    return build_auto_review_director_agent(
        sandbox=sandbox,
        repo_dir=repo_dir,
        model_id=model_id,
        review_context=review_context,
        runtime_run_id=runtime_run_id,
    )


# Backwards-compatible aliases for older call sites.
build_auto_review_lane_agent = build_auto_review_specialist_agent
build_auto_review_chief_agent = build_auto_review_director_agent
