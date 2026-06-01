"""Agent builders for the agent-driven mention workflow."""

from __future__ import annotations

import re

from deepagents import create_deep_agent
from deepagents.backends.protocol import (
    BackendProtocol,
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

from agent.middleware import (
    ModelRetryMiddleware,
    StructuredOutputRetryMiddleware,
    ToolErrorMiddleware,
)
from agent.rlm import build_repo_analyst_subagent
from agent.runtime.termination import RunTerminationMiddleware
from agent.sandbox.manager import sandbox_file_tool_path, sandbox_shell_path
from agent.scenes.mention.middleware import MentionRawRecordMiddleware
from agent.scenes.mention.models import (
    MentionAgentResponse,
    MentionContext,
    MentionReviewVerdict,
    MentionSubagentType,
)
from agent.scenes.mention.prompts import (
    build_mention_author_prompt,
    build_mention_auxiliary_prompt,
    build_mention_reviewer_prompt,
    describe_mention_subagent,
)
from agent.scenes.mention.scope import review_scope_snapshot
from agent.selfevolution.assets import visible_skill_source_roots
from agent.utils.model import make_model
from agent.utils.structured_output import (
    SimpleStructuredSubagentRunnable,
    SimpleSubagentResult,
    make_structured_response_format,
)


class _FileToolBackend(BackendProtocol):
    """Expose filesystem tools without shell execution to mention agents."""

    def __init__(self, backend: BackendProtocol, *, allow_writes: bool) -> None:
        self._backend = backend
        self._allow_writes = allow_writes
        self.cwd = getattr(backend, "cwd", None)
        self.root_dir = getattr(backend, "root_dir", None)
        self.host_root_dir = getattr(backend, "host_root_dir", None)

    def ls(self, path: str) -> LsResult:
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend.read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self._backend.grep(pattern, path, glob)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        return self._backend.glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._allow_writes:
            del content
            return WriteResult(error="read_only_backend", path=file_path)
        return self._backend.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if not self._allow_writes:
            del old_string, new_string, replace_all
            return EditResult(error="read_only_backend", path=file_path, occurrences=0)
        return self._backend.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if not self._allow_writes:
            return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]
        return self._backend.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend.download_files(paths)


_BLOCKED_GIT_COMMANDS = (
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+push\b", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+commit\b", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*|\b(?:and|then)\b\s+)git\s+(?:merge|rebase|cherry-pick|am|reset|checkout|switch|stash)\b", re.IGNORECASE),
)


class _MentionAgentBackend(_FileToolBackend, SandboxBackendProtocol):
    """Allow shell access for inspection/testing while blocking branch-state mutations."""

    @property
    def id(self) -> str:
        return str(getattr(self._backend, "id", f"mention:{id(self)}"))

    def _blocked_command(self, command: str) -> str | None:
        normalized = command.strip()
        for pattern in _BLOCKED_GIT_COMMANDS:
            match = pattern.search(normalized)
            if match:
                return match.group(0).strip()
        return None

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        blocked = self._blocked_command(command)
        if blocked:
            return ExecuteResponse(
                output=(
                    "Error: branch-state-changing git commands are blocked in the primary Mention Agent "
                    f"backend: `{blocked}`. Use the orchestrator path for commit/push."
                ),
                exit_code=126,
                truncated=False,
            )
        return self._backend.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)


def _skill_sources(sandbox: SandboxBackendProtocol, repo_dir: str) -> list[str]:
    del repo_dir
    return visible_skill_source_roots("mention", sandbox)


def _attach_system_prompt(agent: object, system_prompt: str) -> object:
    try:
        agent.open_review_system_prompt = system_prompt
    except Exception:
        pass
    return agent


def _build_review_scope_tool(context: MentionContext):
    def review_scope(file_path: str | None = None) -> dict[str, object]:
        """Read the authoritative MR scope frozen by the mention orchestrator.

        Use this as the source of truth for base/head SHAs, diff ranges, changed-file
        status, and per-file diff content. If caller summaries conflict with this
        snapshot, trust this tool.
        """

        return review_scope_snapshot(context, file_path=file_path)

    return review_scope


def _termination_middleware(context: MentionContext, runtime_run_id: str | None) -> list[object]:
    if not runtime_run_id:
        return []
    return [
        RunTerminationMiddleware(
            run_id=runtime_run_id,
            actor_key=f"{context.project_id}!{context.mr_iid}",
        )
    ]


def build_mention_auxiliary_subagent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    source_branch: str,
    subagent_type: MentionSubagentType,
    context: MentionContext,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    """Build a read-only compiled auxiliary subagent for the main Mention Agent."""
    del source_branch
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    review_scope_tool = _build_review_scope_tool(context)
    backend = _FileToolBackend(sandbox, allow_writes=False)
    runnable = create_deep_agent(
        model=model,
        tools=[review_scope_tool],
        system_prompt=build_mention_auxiliary_prompt(
            repo_dir=repo_dir,
            file_tool_repo_dir=file_tool_repo_dir,
            subagent_type=subagent_type,
            context=context,
        ),
        backend=backend,
        middleware=[
            *_termination_middleware(context, runtime_run_id),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(backend, repo_dir),
        response_format=make_structured_response_format(SimpleSubagentResult),
    )
    return {
        "name": subagent_type,
        "description": describe_mention_subagent(subagent_type),
        "runnable": SimpleStructuredSubagentRunnable(runnable=runnable, name=subagent_type),
    }


def build_mention_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    source_branch: str,
    context: MentionContext,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    return build_mention_author_agent(
        sandbox=sandbox,
        repo_dir=repo_dir,
        source_branch=source_branch,
        context=context,
        model_id=model_id,
        runtime_run_id=runtime_run_id,
    )


def build_mention_author_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    source_branch: str,
    context: MentionContext,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    """Build the writable primary Mention Agent and register its auxiliary subagents."""
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    try:
        shell_repo_dir = sandbox_shell_path(sandbox, repo_dir)
    except AttributeError:
        shell_repo_dir = repo_dir
    review_scope_tool = _build_review_scope_tool(context)
    subagents = [
        build_mention_auxiliary_subagent(
            sandbox=sandbox,
            repo_dir=repo_dir,
            source_branch=source_branch,
            subagent_type=subagent_type,
            context=context,
            model_id=model_id,
            runtime_run_id=runtime_run_id,
        )
        for subagent_type in ("dialogs",)
    ]
    repo_analyst_backend = _MentionAgentBackend(sandbox, allow_writes=False)
    subagents.append(
        build_repo_analyst_subagent(
            scene="mention",
            backend=repo_analyst_backend,
            repo_dir=repo_dir,
            file_tool_repo_dir=file_tool_repo_dir,
            shell_repo_dir=shell_repo_dir,
            model_id=model_id,
            context_payload={
                "project_id": context.project_id,
                "mr_iid": context.mr_iid,
                "run_id": context.run_id,
                "reply_target": context.reply_target,
            },
            extra_tools={"review_scope": review_scope_tool},
        )
    )
    system_prompt = build_mention_author_prompt(
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        context=context,
    )
    backend = _MentionAgentBackend(sandbox, allow_writes=True)
    agent = create_deep_agent(
        model=model,
        tools=[review_scope_tool],
        system_prompt=system_prompt,
        backend=backend,
        middleware=[
            *_termination_middleware(context, runtime_run_id),
            MentionRawRecordMiddleware(
                context=context,
                runtime_run_id=runtime_run_id,
                mention_role="author",
                system_prompt=system_prompt,
            ),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(backend, repo_dir),
        response_format=make_structured_response_format(MentionAgentResponse),
        subagents=subagents,
    )
    return _attach_system_prompt(agent, system_prompt)


def build_mention_reviewer_agent(
    sandbox: SandboxBackendProtocol,
    repo_dir: str,
    context: MentionContext,
    model_id: str | None = None,
    runtime_run_id: str | None = None,
):
    """Build the read-only reviewer agent used to gate final mention outputs."""
    model = make_model(model_id, temperature=0, max_tokens=16_000)
    file_tool_repo_dir = sandbox_file_tool_path(sandbox, repo_dir)
    review_scope_tool = _build_review_scope_tool(context)
    system_prompt = build_mention_reviewer_prompt(
        repo_dir=repo_dir,
        file_tool_repo_dir=file_tool_repo_dir,
        context=context,
    )
    backend = _MentionAgentBackend(sandbox, allow_writes=False)
    agent = create_deep_agent(
        model=model,
        tools=[review_scope_tool],
        system_prompt=system_prompt,
        backend=backend,
        middleware=[
            *_termination_middleware(context, runtime_run_id),
            MentionRawRecordMiddleware(
                context=context,
                runtime_run_id=runtime_run_id,
                mention_role="reviewer",
                system_prompt=system_prompt,
            ),
            StructuredOutputRetryMiddleware(),
            ModelRetryMiddleware(),
            ToolErrorMiddleware(),
        ],
        skills=_skill_sources(backend, repo_dir),
        response_format=make_structured_response_format(MentionReviewVerdict),
    )
    return _attach_system_prompt(agent, system_prompt)
