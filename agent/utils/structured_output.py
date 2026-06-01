"""Shared structured-output helpers for DeepAgent builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from pydantic import BaseModel


class SimpleSubagentResult(BaseModel):
    """Minimal structured output contract for scene-owned subagents."""

    result: str
def require_simple_subagent_result(state: dict[str, Any]) -> SimpleSubagentResult:
    """Extract the required minimal structured subagent output from agent state."""
    structured = state.get("structured_response")
    if structured is None:
        raise RuntimeError("subagent missing structured_response")
    return SimpleSubagentResult.model_validate(structured)


@dataclass
class SimpleStructuredSubagentRunnable:
    """Normalize subagent results to the shared `{result}` contract."""

    runnable: Any
    name: str

    async def ainvoke(self, payload: Any, config: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        result = await self.runnable.ainvoke(payload, config=config, **kwargs)
        structured = require_simple_subagent_result(result)
        return {
            "messages": [AIMessage(content=structured.result, name=self.name)],
            "structured_response": structured,
        }

    def invoke(self, payload: Any, config: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        result = self.runnable.invoke(payload, config=config, **kwargs)
        structured = require_simple_subagent_result(result)
        return {
            "messages": [AIMessage(content=structured.result, name=self.name)],
            "structured_response": structured,
        }


def make_structured_response_format(schema: type[Any]) -> ToolStrategy[Any]:
    """Use tool-based structured output so retries stay inside the framework."""
    return ToolStrategy(schema=schema, handle_errors=True)
