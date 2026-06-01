"""Raw record persistence middleware for the mention scene."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from agent.controlplane import get_tracking_service
from agent.raw_records import jsonable, serialize_messages
from agent.scenes.mention.models import MentionAgentResponse, MentionContext, MentionReviewVerdict


def _configurable(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, dict) else {}


class MentionRawRecordMiddleware(AgentMiddleware):
    """Persist main mention-agent raw records after each agent invocation."""

    def __init__(
        self,
        *,
        context: MentionContext,
        runtime_run_id: str | None,
        mention_role: str,
        system_prompt: str = "",
    ) -> None:
        self.context = context
        self.runtime_run_id = runtime_run_id
        self.mention_role = mention_role
        self.system_prompt = system_prompt
        self._input_messages_json: list[dict[str, Any]] = []
        self._thread_id = self._default_thread_id()
        self._round_index: int | None = None

    def _default_thread_id(self) -> str:
        suffix = "author" if self.mention_role == "author" else "reviewer"
        return f"mention:{self.context.run_id}:{suffix}"

    def _capture_before_agent(self, state: Any, config: Any) -> None:
        messages = state.get("messages") if isinstance(state, dict) else []
        self._input_messages_json = serialize_messages(messages if isinstance(messages, list) else [])
        configurable = _configurable(config)
        self._thread_id = str(configurable.get("thread_id") or self._default_thread_id())
        round_index = configurable.get("round_index")
        self._round_index = int(round_index) if isinstance(round_index, int) else None

    def before_agent(self, state, runtime, config) -> None:  # type: ignore[override]
        del runtime
        self._capture_before_agent(state, config)
        return None

    async def abefore_agent(self, state, runtime, config) -> None:  # type: ignore[override]
        del runtime
        self._capture_before_agent(state, config)
        return None

    async def aafter_agent(self, state, runtime) -> None:
        del runtime
        if not self.runtime_run_id:
            return None
        messages = state.get("messages") if isinstance(state, dict) else []
        structured_response = state.get("structured_response") if isinstance(state, dict) else None
        messages_json = serialize_messages(messages if isinstance(messages, list) else [])
        metadata: dict[str, Any] = {
            "logical_run_id": self.context.run_id,
            "project_id": self.context.project_id,
            "mr_iid": self.context.mr_iid,
            "note_id": self.context.note_id,
            "discussion_id": self.context.discussion_id,
            "source_branch": self.context.mr_snapshot.source_branch,
            "target_branch": self.context.mr_snapshot.target_branch,
            "base_sha": self.context.mr_snapshot.base_sha,
            "start_sha": self.context.mr_snapshot.start_sha,
            "head_sha": self.context.mr_snapshot.head_sha,
            "diff_range": self.context.mr_snapshot.diff_range,
            "commit_range": self.context.mr_snapshot.commit_range,
            "repo_dir": self.context.mr_snapshot.repo_dir,
            "mention_role": self.mention_role,
        }
        if self._round_index is not None:
            metadata["round_index"] = self._round_index
        if self.mention_role == "author":
            try:
                response = MentionAgentResponse.model_validate(structured_response)
            except Exception:
                response = None
            if response is not None:
                metadata["used_subagents"] = list(response.used_subagents)
        elif self.mention_role == "reviewer":
            try:
                verdict = MentionReviewVerdict.model_validate(structured_response)
            except Exception:
                verdict = None
            if verdict is not None:
                metadata["approved"] = verdict.approved

        get_tracking_service().append_agent_record(
            self.runtime_run_id,
            {
                "record_kind": f"mention.{self.mention_role}",
                "thread_id": self._thread_id,
                "system_prompt": self.system_prompt,
                "input_messages_json": jsonable(self._input_messages_json),
                "messages_json": jsonable(messages_json or self._input_messages_json),
                "result_json": jsonable(structured_response if structured_response is not None else {}),
                "started_at": None,
                "completed_at": None,
                "metadata_json": jsonable(metadata),
            },
        )
        return None
