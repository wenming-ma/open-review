"""Tests for the SQLite-backed control-plane configuration layer."""

from __future__ import annotations

from agent.config import settings


def test_settings_reads_updated_values_from_controlplane_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "GITLAB_API_URL", "https://gitlab-api.initial.example.com")
    monkeypatch.setattr(settings, "GITLAB_EXTERNAL_URL", "https://gitlab.example.com")

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    assert settings.GITLAB_API_URL == "https://gitlab-api.initial.example.com"
    assert settings.GITLAB_EXTERNAL_URL == "https://gitlab.example.com"
    settings.reset_overrides()

    service.set_values(
        {
            "GITLAB_API_URL": "https://gitlab-api.updated.example.com",
            "GITLAB_EXTERNAL_URL": "https://gitlab.updated.example.com",
            "LLM_ACTIVE_PROVIDER": "openai",
            "OPENAI_BASE_URL": "https://openai.local/v1",
            "OPENAI_MODEL": "gpt-4o-mini",
        },
        actor="test-suite",
    )

    assert settings.GITLAB_API_URL == "https://gitlab-api.updated.example.com"
    assert settings.GITLAB_EXTERNAL_URL == "https://gitlab.updated.example.com"
    assert settings.LLM_ACTIVE_PROVIDER == "openai"
    assert settings.OPENAI_BASE_URL == "https://openai.local/v1"
    assert settings.OPENAI_MODEL == "gpt-4o-mini"
    assert settings.LLM_MODEL_ID == "openai:gpt-4o-mini"


def test_config_service_bootstraps_existing_settings_into_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    monkeypatch.setattr(settings, "GITLAB_API_URL", "https://gitlab-api.bootstrap.example.com")
    monkeypatch.setattr(settings, "GITLAB_EXTERNAL_URL", "https://gitlab.bootstrap.example.com")
    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "open-review-webhook")
    monkeypatch.setattr(settings, "GITLAB_BOT_USERNAME", "open-review-bootstrap")

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    snapshot = service.get_snapshot()

    assert db_path.exists()
    assert snapshot["GITLAB_API_URL"] == "https://gitlab-api.bootstrap.example.com"
    assert snapshot["GITLAB_EXTERNAL_URL"] == "https://gitlab.bootstrap.example.com"
    assert snapshot["GITLAB_WEBHOOK_SECRET"] == "open-review-webhook"
    assert snapshot["GITLAB_BOT_USERNAME"] == "open-review-bootstrap"


def test_settings_default_webhook_secret_is_open_review_webhook():
    assert settings.bootstrap_snapshot().GITLAB_WEBHOOK_SECRET == "open-review-webhook"


def test_config_service_does_not_bootstrap_admin_account_but_generates_session_secret(tmp_path, monkeypatch):
    db_path = tmp_path / "controlplane.db"
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    assert db_path.exists()
    assert service.has_admin_account() is False
    secret = service.get_admin_session_secret()
    assert isinstance(secret, str)
    assert len(secret) >= 32


def test_config_service_persists_gitlab_identity_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    service.set_cached_gitlab_identity(
        {
            "username": "open_review",
            "name": "Open Review Bot",
            "avatar_url": "http://gitlab.local/avatar.png",
            "user_id": 7,
            "fetched_at": "2026-04-10T10:00:00+00:00",
        }
    )

    cached = service.get_cached_gitlab_identity()

    assert cached["username"] == "open_review"
    assert cached["name"] == "Open Review Bot"
    assert cached["avatar_url"] == "http://gitlab.local/avatar.png"
    assert cached["user_id"] == 7


def test_config_service_redacts_sensitive_field_values_for_settings_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "GITLAB_TOKEN", "secret-token")

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    fields = {item["key"]: item for item in service.list_fields()}

    assert fields["GITLAB_TOKEN"]["sensitive"] is True
    assert fields["GITLAB_TOKEN"]["configured"] is True
    assert fields["GITLAB_TOKEN"]["value"] == ""


def test_config_service_normalizes_gitlab_target_projects_from_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "GITLAB_API_URL", "https://gitlab-api.example.com")
    monkeypatch.setattr(settings, "GITLAB_EXTERNAL_URL", "https://gitlab.example.com")

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    service.set_values(
        {
            "GITLAB_TARGET_PROJECTS": [
                "https://gitlab.example.com/root/kicad.git",
                " root/kicad ",
                "https://gitlab.example.com/team/libeda/",
            ],
        },
        actor="test-suite",
    )

    assert service.get_snapshot()["GITLAB_TARGET_PROJECTS"] == ["root/kicad", "team/libeda"]


def test_config_service_bootstraps_llm_provider_fields_from_legacy_model_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(tmp_path / "controlplane.db"))
    monkeypatch.setattr(settings, "LLM_MODEL_ID", "openai:gpt-4.1-mini")
    monkeypatch.setattr(settings, "LLM_ACTIVE_PROVIDER", "")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "")

    from agent.controlplane import get_config_service, reset_controlplane_services

    reset_controlplane_services()
    service = get_config_service()

    snapshot = service.get_snapshot()

    assert snapshot["LLM_ACTIVE_PROVIDER"] == "openai"
    assert snapshot["OPENAI_MODEL"] == "gpt-4.1-mini"
    assert snapshot["LLM_MODEL_ID"] == "openai:gpt-4.1-mini"


def test_daily_audit_self_repo_fields_are_not_exposed_in_config_service():
    from agent.controlplane.service import CONFIG_FIELDS

    field_keys = {field.key for field in CONFIG_FIELDS}

    assert "OPEN_REVIEW_SELF_REPO_PROJECT" not in field_keys
    assert "OPEN_REVIEW_SELF_REPO_DEFAULT_BRANCH" not in field_keys
    assert "SELF_EVOLUTION_CODE_EVOLVER_COMMAND" not in field_keys
