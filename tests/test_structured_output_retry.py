from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from agent.middleware.structured_output_retry import (
    StructuredOutputRetryMiddleware,
    StructuredResponseRetryExhausted,
)
from agent.utils.structured_output import make_structured_response_format


class _ResponseSchema(BaseModel):
    status: str


def _request(*, response_format=None) -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=[HumanMessage(content="do the thing")],
        response_format=response_format,
        runtime=SimpleNamespace(context={}),
    )


@pytest.mark.asyncio
async def test_structured_output_retry_middleware_retries_plaintext_final_response_once():
    middleware = StructuredOutputRetryMiddleware(max_retries=1)
    seen_requests = []

    async def handler(request: ModelRequest):
        seen_requests.append(request)
        if len(seen_requests) == 1:
            return ModelResponse(result=[AIMessage(content="plain text approval")], structured_response=None)
        return ModelResponse(
            result=[AIMessage(content="structured")],
            structured_response=_ResponseSchema(status="ok"),
        )

    response = await middleware.awrap_model_call(
        _request(response_format=make_structured_response_format(_ResponseSchema)),
        handler,
    )

    assert response.structured_response == _ResponseSchema(status="ok")
    assert len(seen_requests) == 2
    retry_request = seen_requests[-1]
    assert isinstance(retry_request.messages[-1], HumanMessage)
    assert "required structured output" in retry_request.messages[-1].content


@pytest.mark.asyncio
async def test_structured_output_retry_middleware_skips_tool_interaction_responses():
    middleware = StructuredOutputRetryMiddleware(max_retries=1)
    call_count = 0

    async def handler(_request: ModelRequest):
        nonlocal call_count
        call_count += 1
        return ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute", "args": {"command": "git diff"}, "id": "tool-1", "type": "tool_call"}],
                )
            ],
            structured_response=None,
        )

    response = await middleware.awrap_model_call(
        _request(response_format=make_structured_response_format(_ResponseSchema)),
        handler,
    )

    assert call_count == 1
    assert response.structured_response is None


@pytest.mark.asyncio
async def test_structured_output_retry_middleware_exhausts_after_configured_retries():
    middleware = StructuredOutputRetryMiddleware(max_retries=1)
    call_count = 0

    async def handler(_request: ModelRequest):
        nonlocal call_count
        call_count += 1
        return ModelResponse(result=[AIMessage(content="still plain text")], structured_response=None)

    with pytest.raises(StructuredResponseRetryExhausted, match="structured output retry exhausted"):
        await middleware.awrap_model_call(
            _request(response_format=make_structured_response_format(_ResponseSchema)),
            handler,
        )

    assert call_count == 2


@pytest.mark.asyncio
async def test_structured_output_retry_middleware_normalizes_raw_schema_to_tool_strategy():
    middleware = StructuredOutputRetryMiddleware(max_retries=0)
    seen = {}

    async def handler(request: ModelRequest):
        seen["response_format"] = request.response_format
        return ModelResponse(
            result=[ToolMessage(content="tool retry in progress", tool_call_id="tool-1")],
            structured_response=None,
        )

    await middleware.awrap_model_call(
        _request(response_format=_ResponseSchema),
        handler,
    )

    assert type(seen["response_format"]).__name__ == "ToolStrategy"
