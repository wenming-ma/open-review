"""Structured output retry middleware for DeepAgent builders."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.observability import current_trace_identifiers
from agent.runtime.journal_observer import schedule_runtime_observation

logger = logging.getLogger(__name__)


class StructuredResponseRetryExhausted(RuntimeError):
    """Structured output could not be recovered after retrying."""


def _schema_names(response_format: Any) -> list[str]:
    if isinstance(response_format, ToolStrategy):
        return [spec.name for spec in response_format.schema_specs]
    name = getattr(response_format, "__name__", None)
    if isinstance(name, str) and name:
        return [name]
    title = response_format.get("title") if isinstance(response_format, dict) else None
    if isinstance(title, str) and title:
        return [title]
    return ["structured_response"]


def _normalize_response_format(response_format: Any) -> Any:
    if response_format is None:
        return None
    if isinstance(response_format, ToolStrategy):
        if response_format.handle_errors is True:
            return response_format
        return ToolStrategy(
            schema=response_format.schema,
            tool_message_content=response_format.tool_message_content,
            handle_errors=True,
        )
    if isinstance(response_format, type | dict):
        return ToolStrategy(schema=response_format, handle_errors=True)
    return response_format


def _contains_tool_interaction(messages: list[Any]) -> bool:
    for message in messages:
        if isinstance(message, ToolMessage):
            return True
        if isinstance(message, AIMessage) and message.tool_calls:
            return True
    return False


def _retry_prompt(*, schema_names: list[str], attempt: int, max_retries: int) -> str:
    expected = ", ".join(schema_names)
    return (
        "Your previous reply did not produce the required structured output.\n"
        f"Expected schema: {expected}\n"
        f"Retry attempt: {attempt} of {max_retries}\n"
        "Return ONLY a valid structured response matching the schema. "
        "Do not answer in plain text."
    )


def _error_summary(response: ModelResponse[Any]) -> str:
    last = response.result[-1] if response.result else None
    if isinstance(last, ToolMessage):
        return str(last.content)
    if isinstance(last, AIMessage):
        parts: list[str] = []
        content = last.content
        if isinstance(content, str):
            parts.append(content.strip())
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        if parts:
            return parts[-1]
    return "missing structured_response"


def _record_retry_event(name: str, *, details: dict[str, Any]) -> None:
    current_trace_identifiers().add_event(name, details)
    schedule_runtime_observation(name, details=details)


class StructuredOutputRetryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Retry once the model finishes without producing a structured response."""

    def __init__(self, *, max_retries: int = 10) -> None:
        self._max_retries = max_retries

    def _should_retry(
        self,
        *,
        request: ModelRequest[Any],
        response: ModelResponse[Any],
    ) -> bool:
        if request.response_format is None:
            return False
        if response.structured_response is not None:
            return False
        if _contains_tool_interaction(response.result):
            return False
        return bool(response.result)

    def _retry_request(
        self,
        *,
        request: ModelRequest[Any],
        response: ModelResponse[Any],
        attempt: int,
    ) -> ModelRequest[Any]:
        schema_names = _schema_names(request.response_format)
        details = {
            "schema_names": json.dumps(schema_names, ensure_ascii=False),
            "attempt": attempt,
            "max_retries": self._max_retries,
            "missing_structured_response": True,
            "error_summary": _error_summary(response),
        }
        _record_retry_event("structured_retry_requested", details=details)
        return request.override(
            response_format=_normalize_response_format(request.response_format),
            messages=[
                *request.messages,
                *response.result,
                HumanMessage(
                    content=_retry_prompt(
                        schema_names=schema_names,
                        attempt=attempt,
                        max_retries=self._max_retries,
                    )
                ),
            ],
        )

    def _exhausted_error(self, *, request: ModelRequest[Any], response: ModelResponse[Any]) -> StructuredResponseRetryExhausted:
        schema_names = _schema_names(request.response_format)
        details = {
            "schema_names": json.dumps(schema_names, ensure_ascii=False),
            "attempt": self._max_retries,
            "max_retries": self._max_retries,
            "missing_structured_response": True,
            "error_summary": _error_summary(response),
        }
        _record_retry_event("structured_retry_exhausted", details=details)
        return StructuredResponseRetryExhausted(
            f"structured output retry exhausted for {', '.join(schema_names)}"
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        current_request = request.override(
            response_format=_normalize_response_format(request.response_format)
        )
        response = handler(current_request)
        attempt = 0
        while self._should_retry(request=current_request, response=response):
            if attempt >= self._max_retries:
                raise self._exhausted_error(request=current_request, response=response)
            attempt += 1
            current_request = self._retry_request(
                request=current_request,
                response=response,
                attempt=attempt,
            )
            response = handler(current_request)
        if attempt > 0 and response.structured_response is not None:
            _record_retry_event(
                "structured_retry_succeeded",
                details={
                    "attempt": attempt,
                    "max_retries": self._max_retries,
                    "schema_names": json.dumps(_schema_names(current_request.response_format), ensure_ascii=False),
                },
            )
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        current_request = request.override(
            response_format=_normalize_response_format(request.response_format)
        )
        response = await handler(current_request)
        attempt = 0
        while self._should_retry(request=current_request, response=response):
            if attempt >= self._max_retries:
                raise self._exhausted_error(request=current_request, response=response)
            attempt += 1
            current_request = self._retry_request(
                request=current_request,
                response=response,
                attempt=attempt,
            )
            response = await handler(current_request)
        if attempt > 0 and response.structured_response is not None:
            _record_retry_event(
                "structured_retry_succeeded",
                details={
                    "attempt": attempt,
                    "max_retries": self._max_retries,
                    "schema_names": json.dumps(_schema_names(current_request.response_format), ensure_ascii=False),
                },
            )
        return response
