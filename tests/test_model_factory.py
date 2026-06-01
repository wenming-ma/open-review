"""Tests for the provider-aware model factory."""

from __future__ import annotations

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
    assert captured["use_responses_api"] is True

