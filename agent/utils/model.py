"""Provider-aware LLM model factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

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
    return init_chat_model(model=resolved.model_id, **init_kwargs)


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
