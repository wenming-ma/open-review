"""Provider-aware LLM model factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ConfigDict

from agent.config import settings

_DEFAULT_ACTIVE_PROVIDER = "openai"
_DEFAULT_OPENAI_MODEL = "gpt-5.4"
_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


@dataclass(frozen=True)
class ResolvedLLMConfig:
    provider: str
    model: str
    model_id: str
    base_url: str
    api_key: str


def _list_or_empty(value: Any) -> list:
    return value if isinstance(value, list) else []


def _normalize_message_content(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list):
        return [_normalize_content_block(item) for item in value if item is not None]
    return value


def _normalize_content_block(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if normalized.get("content") is None and "content" in normalized:
        normalized["content"] = ""
    if normalized.get("text") is None and "text" in normalized:
        normalized["text"] = ""
    if normalized.get("input") is None and "input" in normalized:
        normalized["input"] = {}
    return normalized


def _normalize_anthropic_raw_content(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        return [{"type": "text", "text": str(value)}]
    blocks: list[Any] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, dict):
            block = _normalize_content_block(item)
            if block.get("type") is None and "text" in block:
                block["type"] = "text"
            blocks.append(block)
        else:
            blocks.append({"type": "text", "text": str(item)})
    return blocks


def _normalize_anthropic_raw_response(value: Any) -> Any:
    try:
        data = value.model_dump()
    except Exception:
        return value
    if "content" not in data:
        return value
    normalized_content = _normalize_anthropic_raw_content(data.get("content"))
    if normalized_content == data.get("content"):
        return value
    if hasattr(value, "model_copy"):
        try:
            return value.model_copy(update={"content": normalized_content})
        except Exception:
            pass
    try:
        object.__setattr__(value, "content", normalized_content)
    except Exception:
        return value
    return value


def _patch_nullable_anthropic_formatter(value: Any) -> Any:
    module = getattr(value.__class__, "__module__", "")
    if not module.startswith("langchain_anthropic"):
        return value
    if not callable(getattr(value, "_format_output", None)):
        return value
    if getattr(value, "_open_review_nullable_anthropic_formatter_patch", False):
        return value

    original_format_output = value._format_output

    def _format_output_with_nullable_content(data: Any, **kwargs: Any):
        return original_format_output(_normalize_anthropic_raw_response(data), **kwargs)

    try:
        object.__setattr__(value, "_format_output", _format_output_with_nullable_content)
        object.__setattr__(value, "_open_review_nullable_anthropic_formatter_patch", True)
    except Exception:
        return value
    return value


def normalize_model_output(value: Any) -> Any:
    """Normalize provider-compatible chat outputs before LangChain reuses them.

    Some Anthropic-compatible providers return nullable list fields such as
    ``tool_calls``. LangChain's Anthropic formatter expects those fields to be
    actual lists on follow-up turns, so normalize them at the model boundary.
    """
    if isinstance(value, (AIMessage, AIMessageChunk)):
        value.content = _normalize_message_content(getattr(value, "content", None))
        value.tool_calls = _list_or_empty(getattr(value, "tool_calls", None))
        value.invalid_tool_calls = _list_or_empty(getattr(value, "invalid_tool_calls", None))
        if isinstance(value, AIMessageChunk):
            value.tool_call_chunks = _list_or_empty(getattr(value, "tool_call_chunks", None))
        if isinstance(getattr(value, "additional_kwargs", None), dict):
            if value.additional_kwargs.get("tool_calls") is None:
                value.additional_kwargs.pop("tool_calls", None)
        return value
    if isinstance(value, (ChatGeneration, ChatGenerationChunk)):
        normalize_model_output(value.message)
        return value
    if isinstance(value, ChatResult):
        for generation in value.generations:
            normalize_model_output(generation)
        return value
    if isinstance(value, dict):
        raw = value.get("raw")
        if raw is not None:
            normalize_model_output(raw)
        return value
    return value


class NormalizingChatModel(BaseChatModel):
    """Chat model wrapper that sanitizes nullable provider-compatible outputs."""

    model: BaseChatModel

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return f"open_review_normalized_{getattr(self.model, '_llm_type', 'chat_model')}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": repr(self.model)}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return normalize_model_output(
            self.model._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return normalize_model_output(
            await self.model._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self.model._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield normalize_model_output(chunk)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        async for chunk in self.model._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield normalize_model_output(chunk)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        bound = self.model.bind_tools(tools, tool_choice=tool_choice, **kwargs)
        return _normalize_runnable(bound)

    def with_structured_output(self, schema, *, include_raw=False, **kwargs):
        bound = self.model.with_structured_output(schema, include_raw=include_raw, **kwargs)
        return _normalize_runnable(bound)


def _normalize_runnable(value: Any) -> Any:
    if isinstance(value, BaseChatModel):
        return NormalizingChatModel(model=value)
    if isinstance(value, Runnable):
        return value | RunnableLambda(normalize_model_output)
    return value


def _normalize_provider(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"openai", "anthropic"}:
        return normalized
    return ""


def split_model_id(model_id: str | None) -> tuple[str, str]:
    raw = (model_id or "").strip()
    if not raw:
        return "", ""
    if ":" in raw:
        provider, model = raw.split(":", 1)
        return _normalize_provider(provider), model.strip()
    return "", raw


def coerce_llm_settings(values: dict[str, Any]) -> dict[str, Any]:
    data = dict(values)
    data.setdefault("OPENAI_BASE_URL", _DEFAULT_OPENAI_BASE_URL)
    data.setdefault("ANTHROPIC_BASE_URL", _DEFAULT_ANTHROPIC_BASE_URL)

    legacy_provider, legacy_model = split_model_id(str(data.get("LLM_MODEL_ID", "")))
    active_provider = _normalize_provider(str(data.get("LLM_ACTIVE_PROVIDER", ""))) or legacy_provider or _DEFAULT_ACTIVE_PROVIDER

    data["LLM_ACTIVE_PROVIDER"] = active_provider

    if not data.get("OPENAI_MODEL") and legacy_provider == "openai" and legacy_model:
        data["OPENAI_MODEL"] = legacy_model
    if not data.get("ANTHROPIC_MODEL") and legacy_provider == "anthropic" and legacy_model:
        data["ANTHROPIC_MODEL"] = legacy_model

    data.setdefault("OPENAI_MODEL", _DEFAULT_OPENAI_MODEL)
    data.setdefault("ANTHROPIC_MODEL", _DEFAULT_ANTHROPIC_MODEL)

    active_model = data["OPENAI_MODEL"] if active_provider == "openai" else data["ANTHROPIC_MODEL"]
    data["LLM_MODEL_ID"] = f"{active_provider}:{active_model}"
    return data


def resolve_llm_config(snapshot: dict[str, Any], model_id: str | None = None) -> ResolvedLLMConfig:
    data = coerce_llm_settings(snapshot)
    override_provider, override_model = split_model_id(model_id)
    if model_id and not override_provider:
        override_provider = data["LLM_ACTIVE_PROVIDER"]
        override_model = model_id.strip()
    if override_provider and override_model:
        data["LLM_ACTIVE_PROVIDER"] = override_provider
        if override_provider == "openai":
            data["OPENAI_MODEL"] = override_model
        else:
            data["ANTHROPIC_MODEL"] = override_model
    data = coerce_llm_settings(data)

    provider = data["LLM_ACTIVE_PROVIDER"]
    if provider == "openai":
        model = data["OPENAI_MODEL"]
        return ResolvedLLMConfig(
            provider=provider,
            model=model,
            model_id=f"{provider}:{model}",
            base_url=data.get("OPENAI_BASE_URL", _DEFAULT_OPENAI_BASE_URL),
            api_key=data.get("OPENAI_API_KEY", ""),
        )
    model = data["ANTHROPIC_MODEL"]
    return ResolvedLLMConfig(
        provider=provider,
        model=model,
        model_id=f"{provider}:{model}",
        base_url=data.get("ANTHROPIC_BASE_URL", _DEFAULT_ANTHROPIC_BASE_URL),
        api_key=data.get("ANTHROPIC_API_KEY", ""),
    )


def make_model_from_snapshot(
    snapshot: dict[str, Any],
    model_id: str | None = None,
    *,
    temperature: float = 0,
    max_tokens: int = 16_000,
) -> BaseChatModel:
    """Create a chat model from an explicit settings snapshot."""
    resolved = resolve_llm_config(snapshot, model_id=model_id)
    init_kwargs: dict[str, Any] = {
        "api_key": resolved.api_key or None,
        "base_url": resolved.base_url or None,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "max_retries": 5,
    }
    if resolved.provider == "openai":
        init_kwargs["use_responses_api"] = True
    raw_model = init_chat_model(model=resolved.model_id, **init_kwargs)
    if resolved.provider == "anthropic":
        raw_model = _patch_nullable_anthropic_formatter(raw_model)
    return _normalize_runnable(raw_model)


def make_model(
    model_id: str | None = None,
    *,
    temperature: float = 0,
    max_tokens: int = 16_000,
) -> BaseChatModel:
    """Create a chat model from runtime provider settings or an explicit ``provider:model`` override."""
    snapshot = settings.current_snapshot().model_dump()
    return make_model_from_snapshot(
        snapshot,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def extract_model_response_text(value: Any) -> str:
    """Best-effort text extraction for heterogeneous chat-model responses."""
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = getattr(value, "content", value)
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [extract_model_response_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return extract_model_response_text(content.get("text"))
        for key in ("text", "output_text", "content", "thinking"):
            if key in content:
                extracted = extract_model_response_text(content.get(key))
                if extracted:
                    return extracted
        return ""
    return str(content).strip()
