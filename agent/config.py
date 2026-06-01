"""Central configuration with SQLite-backed runtime overrides."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, PrivateAttr

OPEN_REVIEW_STATE_ROOT = Path("/var/lib/open-review")
CONTROLPLANE_DB_PATH = OPEN_REVIEW_STATE_ROOT / "controlplane.db"
PROJECT_CACHE_ROOT = OPEN_REVIEW_STATE_ROOT / "project-cache"
LOCAL_SANDBOX_ROOT = OPEN_REVIEW_STATE_ROOT / "sandboxes"
RUNTIME_ROOT = OPEN_REVIEW_STATE_ROOT / "runtime"


def ensure_state_layout() -> None:
    for path in (OPEN_REVIEW_STATE_ROOT, PROJECT_CACHE_ROOT, LOCAL_SANDBOX_ROOT, RUNTIME_ROOT):
        path.mkdir(parents=True, exist_ok=True)


class Settings(BaseModel):
    _fixed_path_overrides: dict[str, Any] = PrivateAttr(default_factory=dict)
    # -- GitLab --
    GITLAB_API_URL: str = "https://gitlab.example.com"
    GITLAB_EXTERNAL_URL: str = "https://gitlab.example.com"
    GITLAB_TOKEN: str = ""
    GITLAB_WEBHOOK_SECRET: str = "open-review-webhook"
    GITLAB_BOT_USERNAME: str = "open-review-bot"  # deprecated; the runtime bot username is derived from GITLAB_TOKEN
    GITLAB_SSL_VERIFY: bool = True
    GITLAB_TARGET_PROJECTS: list[str] = []
    OPEN_REVIEW_EXTERNAL_URL: str = ""

    # -- LLM --
    LLM_MODEL_ID: str = ""
    LLM_ACTIVE_PROVIDER: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = ""

    # -- Sandbox --
    SANDBOX_TYPE: str = "local"  # "local" or "docker"
    DOCKER_IMAGE: str = "open-review/sandbox:0.1.0"

    # -- Durable runtime --
    WORKER_CONCURRENCY: int = 10
    MR_ACTOR_LEASE_SECONDS: int = 900
    RUN_HEARTBEAT_SECONDS: float = 30
    RUNTIME_MAX_EVENT_ATTEMPTS: int = 3
    RUNTIME_PUBLISH_CLAIM_TTL_SECONDS: int = 900
    MENTION_BATCH_WINDOW_SECONDS: int = 15
    MENTION_MAX_CHANGED_FILES: int = 0
    MENTION_SELF_EVOLUTION_ENABLED: bool = False
    MENTION_SELF_EVOLUTION_INTERVAL_DAYS: int = 7
    MENTION_SELF_EVOLUTION_TIME_LOCAL: str = "02:00"
    DAILY_AUDIT_ENABLED: bool = False
    DAILY_AUDIT_TIMEZONE: str = "Asia/Shanghai"
    DAILY_AUDIT_START_TIME_LOCAL: str = "02:00"
    DAILY_AUDIT_MAX_DURATION_MINUTES: int = 90
    DAILY_AUDIT_ENABLE_AUTOFIX: bool = True
    DAILY_AUDIT_MAX_CHANGED_FILES: int = 5
    DAILY_AUDIT_MAX_CHANGED_LINES: int = 200
    DAILY_AUDIT_ROLLING_ISSUE_TITLE: str = "Open Review 日常审计问题汇总"
    DAILY_AUDIT_SELF_EVOLUTION_ENABLED: bool = True
    DAILY_AUDIT_SELF_EVOLUTION_INTERVAL_DAYS: int = 7
    DAILY_AUDIT_SELF_EVOLUTION_TIME_LOCAL: str = "03:00"
    DAILY_AUDIT_EVOLUTION_MIN_RUNS: int = 14
    DAILY_AUDIT_EVOLUTION_MIN_FRESH_RUNS: int = 7
    DAILY_AUDIT_EVOLUTION_COOLDOWN_HOURS: int = 24
    AUTO_REVIEW_SELF_EVOLUTION_ENABLED: bool = False
    AUTO_REVIEW_SELF_EVOLUTION_INTERVAL_DAYS: int = 7
    AUTO_REVIEW_SELF_EVOLUTION_TIME_LOCAL: str = "02:30"
    OPEN_REVIEW_DOCKER_NETWORK: str = "open-review-net"
    OPEN_REVIEW_IMAGE: str = "open-review:0.1.0"
    OPEN_REVIEW_WORKER_CONTAINER_NAME: str = "open-review-worker"
    OPEN_REVIEW_PHOENIX_CONTAINER_NAME: str = "open-review-phoenix"
    OPEN_REVIEW_PHOENIX_DB_CONTAINER_NAME: str = "open-review-phoenix-db"
    OPEN_REVIEW_PHOENIX_IMAGE: str = "open-review/phoenix:14.2.1"
    OPEN_REVIEW_POSTGRES_IMAGE: str = "postgres:16-alpine"

    # -- Review --
    AUTO_REVIEW_MAX_PUBLISHED_FINDINGS: int = 0
    AUTO_REVIEW_COMMENT_HISTORY_LIMIT: int = 0
    AUTO_REVIEW_HUMAN_COMMENT_LIMIT: int = 0
    AUTO_REVIEW_FETCH_DEPTH: int = 200

    # -- Optional Phoenix tracing --
    PHOENIX_TRACING_ENABLED: bool = False
    PHOENIX_COLLECTOR_ENDPOINT: str = ""
    PHOENIX_API_KEY: str = ""
    PHOENIX_PROJECT_NAME: str = "open-review"
    PHOENIX_UI_BASE_URL: str = ""

    model_config = ConfigDict(extra="ignore")

    @property
    def OPEN_REVIEW_DB_PATH(self) -> str:
        return str(self._fixed_path_overrides.get("OPEN_REVIEW_DB_PATH", CONTROLPLANE_DB_PATH))

    @property
    def PROJECT_CACHE_ROOT(self) -> str:
        return str(self._fixed_path_overrides.get("PROJECT_CACHE_ROOT", PROJECT_CACHE_ROOT))

    @property
    def LOCAL_SANDBOX_ROOT_DIR(self) -> str:
        return str(self._fixed_path_overrides.get("LOCAL_SANDBOX_ROOT_DIR", LOCAL_SANDBOX_ROOT))

    @property
    def OPEN_REVIEW_RUNTIME_ROOT(self) -> str:
        return str(self._fixed_path_overrides.get("OPEN_REVIEW_RUNTIME_ROOT", RUNTIME_ROOT))


class SettingsProxy:
    """A dynamic settings facade that can read runtime overrides from SQLite."""

    def __init__(self) -> None:
        object.__setattr__(self, "_base", Settings())
        object.__setattr__(self, "_overrides", {})
        object.__setattr__(self, "_fixed_overrides", {})
        object.__setattr__(self, "_local", threading.local())

    def bootstrap_snapshot(self) -> Settings:
        data = self._base.model_dump()
        data.update(self._overrides)
        snapshot = Settings.model_validate(data)
        snapshot._fixed_path_overrides = dict(self._fixed_overrides)
        return snapshot

    def reset_overrides(self) -> None:
        self._overrides.clear()
        self._fixed_overrides.clear()

    def _runtime_values(self) -> dict[str, Any]:
        local = self._local
        if getattr(local, "resolving", False):
            return {}

        try:
            local.resolving = True
            from agent.controlplane import get_config_service

            return get_config_service().get_snapshot()
        except Exception:
            return {}
        finally:
            local.resolving = False

    def current_snapshot(self) -> Settings:
        data = self._base.model_dump()
        data.update(self._runtime_values())
        data.update(self._overrides)
        snapshot = Settings.model_validate(data)
        snapshot._fixed_path_overrides = dict(self._fixed_overrides)
        return snapshot

    def __getattr__(self, name: str) -> Any:
        if name in Settings.model_fields:
            return getattr(self.current_snapshot(), name)
        if name in {"OPEN_REVIEW_DB_PATH", "PROJECT_CACHE_ROOT", "LOCAL_SANDBOX_ROOT_DIR", "OPEN_REVIEW_RUNTIME_ROOT"}:
            override = self._fixed_overrides.get(name)
            if override is not None:
                return override
            return getattr(self.current_snapshot(), name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if name in Settings.model_fields:
            self._overrides[name] = value
            return
        if name in {"OPEN_REVIEW_DB_PATH", "PROJECT_CACHE_ROOT", "LOCAL_SANDBOX_ROOT_DIR", "OPEN_REVIEW_RUNTIME_ROOT"}:
            self._fixed_overrides[name] = value
            return
        object.__setattr__(self, name, value)


settings = SettingsProxy()
