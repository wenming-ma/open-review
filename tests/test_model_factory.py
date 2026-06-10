"""Tests for the provider-aware model factory."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from agent.config import settings
from agent.utils import model as model_utils


def test_make_model_uses_openai_provider_configuration(monkeypatch):
    captured = {}

    class _FakeModel:
        pass

    monkeypatch.setattr(settings, "LLM_ACTIVE_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://openai.local/v1")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-secret")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")
    monkeypatch.setattr(settings, "LLM_MODEL_ID", "openai:gpt-4.1-mini")

    def _fake_init_chat_model(model: str, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return _FakeModel()

    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init_chat_model)

    model = model_utils.make_model()

    assert isinstance(model, _FakeModel)
    assert captured["model"] == "openai:gpt-4.1-mini"
    assert captured["base_url"] == "https://openai.local/v1"
    assert captured["api_key"] == "openai-secret"
    assert captured["max_retries"] == 5
    assert captured["timeout"] == 600.0
    assert captured["use_responses_api"] is True


def test_make_model_uses_anthropic_provider_configuration(monkeypatch):
    captured = {}

    class _FakeModel:
        pass

    monkeypatch.setattr(settings, "LLM_ACTIVE_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_BASE_URL", "https://anthropic.local")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setattr(settings, "ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr(settings, "LLM_MODEL_ID", "anthropic:claude-sonnet-4-6")

    def _fake_init_chat_model(model: str, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return _FakeModel()

    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init_chat_model)

    model = model_utils.make_model()

    assert isinstance(model, _FakeModel)
    assert captured["model"] == "anthropic:claude-sonnet-4-6"
    assert captured["base_url"] == "https://anthropic.local"
    assert captured["api_key"] == "anthropic-secret"
    assert captured["max_retries"] == 5
    assert captured["timeout"] == 600.0
    assert "use_responses_api" not in captured


def test_make_model_infers_provider_from_legacy_model_id(monkeypatch):
    captured = {}

    class _FakeModel:
        pass

    monkeypatch.setattr(settings, "LLM_ACTIVE_PROVIDER", "")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://openai.local/v1")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-secret")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "")
    monkeypatch.setattr(settings, "LLM_MODEL_ID", "openai:gpt-4o-mini")

    def _fake_init_chat_model(model: str, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return _FakeModel()

    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init_chat_model)

    model = model_utils.make_model()

    assert isinstance(model, _FakeModel)
    assert captured["model"] == "openai:gpt-4o-mini"
    assert captured["max_retries"] == 5
    assert captured["timeout"] == 600.0
    assert captured["use_responses_api"] is True


def test_make_model_wraps_base_chat_models(monkeypatch):
    class _NullableToolCallModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "nullable-tool-call-model"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            message = AIMessage(content="ok")
            message.tool_calls = None
            return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(settings, "LLM_ACTIVE_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_BASE_URL", "https://anthropic.local")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setattr(settings, "ANTHROPIC_MODEL", "MiniMax-Text-01")
    monkeypatch.setattr(settings, "LLM_MODEL_ID", "anthropic:MiniMax-Text-01")
    monkeypatch.setattr(model_utils, "init_chat_model", lambda *_args, **_kwargs: _NullableToolCallModel())

    model = model_utils.make_model()
    response = model.invoke("hello")

    assert isinstance(model, model_utils.NormalizingChatModel)
    assert response.tool_calls == []


def test_normalizing_chat_model_converts_nullable_tool_calls_to_empty_lists():
    class _NullableToolCallModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "nullable-tool-call-model"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            message = AIMessage(content="ok")
            message.tool_calls = None
            message.invalid_tool_calls = None
            return ChatResult(generations=[ChatGeneration(message=message)])

    wrapped = model_utils.NormalizingChatModel(model=_NullableToolCallModel())

    response = wrapped.invoke("hello")

    assert response.content == "ok"
    assert response.tool_calls == []
    assert response.invalid_tool_calls == []


def test_normalizing_chat_model_wraps_bound_tool_runnables():
    class _NullableToolCallModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "nullable-tool-call-model"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            def _invoke(_value):
                message = AIMessage(content="tool")
                message.tool_calls = None
                message.invalid_tool_calls = None
                return message

            return RunnableLambda(_invoke)

    bound = model_utils.NormalizingChatModel(model=_NullableToolCallModel()).bind_tools([])

    response = bound.invoke("hello")

    assert response.content == "tool"
    assert response.tool_calls == []
    assert response.invalid_tool_calls == []


def test_anthropic_formatter_patch_normalizes_nullable_raw_content():
    class _RawAnthropicResponse:
        def __init__(self, content):
            self.content = content

        def model_dump(self):
            return {"content": self.content}

        def model_copy(self, *, update):
            copied = _RawAnthropicResponse(self.content)
            for key, value in update.items():
                setattr(copied, key, value)
            return copied

    AnthropicLikeModel = type(
        "AnthropicLikeModel",
        (),
        {
            "__module__": "langchain_anthropic.chat_models",
            "_format_output": lambda self, data, **_kwargs: [block for block in data.model_dump()["content"]],
        },
    )
    model = AnthropicLikeModel()

    model_utils._patch_nullable_anthropic_formatter(model)

    assert model._format_output(_RawAnthropicResponse(None)) == []
