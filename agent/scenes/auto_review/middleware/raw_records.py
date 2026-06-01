"""Raw record persistence middleware for the auto-review scene."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from agent.controlplane import get_tracking_service
from agent.raw_records import jsonable, serialize_messages
from agent.scenes.auto_review.models import ReviewContext


def _configurable(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, dict) else {}


class AutoReviewRawRecordMiddleware(AgentMiddleware):
    """Persist director/specialist raw records after each successful agent invocation."""

    def __init__(
        self,
        *,
        context: ReviewContext,
        runtime_run_id: str | None,
        record_kind: str,
        thread_id: str,
        system_prompt: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.context = context
        self.runtime_run_id = runtime_run_id
        self.record_kind = record_kind
        self.thread_id = thread_id
        self.system_prompt = system_prompt
        self.metadata = dict(metadata or {})
        self._input_messages_json: list[dict[str, Any]] = []
        self._thread_id = thread_id

    def _capture_before_agent(self, state: Any, config: Any) -> None:
        messages = state.get("messages") if isinstance(state, dict) else []
        self._input_messages_json = serialize_messages(messages if isinstance(messages, list) else [])
        configurable = _configurable(config)
        self._thread_id = str(configurable.get("thread_id") or self.thread_id)

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
        metadata = {
            "logical_run_id": self.context.review_run_id,
            "project_id": self.context.project_id,
            "mr_iid": self.context.mr_iid,
            "source_branch": self.context.source_branch,
            "target_branch": self.context.target_branch,
            "base_sha": self.context.base_sha,
            "start_sha": self.context.start_sha,
            "head_sha": self.context.head_sha,
            "diff_range": self.context.diff_range,
            "commit_range": self.context.commit_range,
            "review_mode": self.context.review_mode,
            "previous_review_head_sha": self.context.previous_review_head_sha,
            "repo_dir": self.context.repo_dir,
            **self.metadata,
        }
        get_tracking_service().append_agent_record(
            self.runtime_run_id,
            {
                "record_kind": self.record_kind,
                "thread_id": self._thread_id,
                "system_prompt": self.system_prompt,
                "input_messages_json": jsonable(self._input_messages_json),
                "messages_json": jsonable(
                    serialize_messages(messages if isinstance(messages, list) else []) or self._input_messages_json
                ),
                "result_json": jsonable(structured_response if structured_response is not None else {}),
                "started_at": None,
                "completed_at": None,
                "metadata_json": jsonable(metadata),
            },
        )
        return None
