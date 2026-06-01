"""Control-plane services for configuration, auth, and run tracking."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import ensure_writable_directory, settings
from agent.utils.gitlab_project_targets import normalize_gitlab_project_targets
from agent.utils.model import coerce_llm_settings
from agent.utils.timezone import iso_now

_CONFIG_SERVICE: ControlPlaneService | None = None


def _now() -> str:
    return iso_now()


def _encode_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _decode_value(raw: str) -> Any:
    return json.loads(raw)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    salt_encoded = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256$120000${salt_encoded}${digest_encoded}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_raw, digest_raw = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
    expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return secrets.compare_digest(actual, expected)


@dataclass(frozen=True)
class ConfigFieldSpec:
    key: str
    group: str
    label: str
    description: str
    kind: str
    sensitive: bool = False
    visible: bool = True


CONFIG_FIELDS: tuple[ConfigFieldSpec, ...] = (
    ConfigFieldSpec("GITLAB_API_URL", "GitLab", "GitLab API 地址", "Open Review 访问 GitLab API 与 git remote 时使用的地址。", "text"),
    ConfigFieldSpec("GITLAB_EXTERNAL_URL", "GitLab", "GitLab 外部地址", "浏览器访问 GitLab 时使用的外部地址。", "text"),
    ConfigFieldSpec("GITLAB_TOKEN", "GitLab", "GitLab Token", "专用 bot 账号使用的 API Token。", "password", True),
    ConfigFieldSpec(
        "GITLAB_WEBHOOK_SECRET",
        "GitLab",
        "Webhook 密钥",
        "用于校验 GitLab Webhook 的共享密钥。",
        "password",
        True,
    ),
    ConfigFieldSpec(
        "GITLAB_BOT_USERNAME",
        "GitLab",
        "机器人用户名",
        "已废弃。bot 用户名现在根据 GITLAB_TOKEN 自动解析。",
        "text",
        visible=False,
    ),
    ConfigFieldSpec("GITLAB_SSL_VERIFY", "GitLab", "校验证书", "是否启用 TLS 证书校验。", "bool"),
    ConfigFieldSpec(
        "GITLAB_TARGET_PROJECTS",
        "GitLab",
        "GitLab Projects",
        "需要自动配置 webhook 的项目列表；一行一个 project path 或 project id，例如 group/project。",
        "multiline",
    ),
    ConfigFieldSpec("OPEN_REVIEW_EXTERNAL_URL", "GitLab", "Open Review 外部地址", "GitLab 访问 Open Review Webhook 与后台时使用的外部地址。", "text"),
    ConfigFieldSpec("LLM_MODEL_ID", "LLM", "兼容模型 ID", "兼容字段，不在后台直接编辑。", "text", visible=False),
    ConfigFieldSpec("LLM_ACTIVE_PROVIDER", "LLM", "当前 Provider", "当前生效的模型服务提供方。", "text"),
    ConfigFieldSpec("OPENAI_BASE_URL", "LLM", "OpenAI Base URL", "OpenAI 兼容接口的基础地址。", "text"),
    ConfigFieldSpec("OPENAI_API_KEY", "LLM", "OpenAI API Key", "OpenAI 兼容接口的访问密钥。", "password", True),
    ConfigFieldSpec("OPENAI_MODEL", "LLM", "OpenAI 模型", "OpenAI 兼容接口当前使用的模型。", "text"),
    ConfigFieldSpec("ANTHROPIC_BASE_URL", "LLM", "Anthropic Base URL", "Anthropic 兼容接口的基础地址。", "text"),
    ConfigFieldSpec("ANTHROPIC_API_KEY", "LLM", "Anthropic API Key", "Anthropic 兼容接口的访问密钥。", "password", True),
    ConfigFieldSpec("ANTHROPIC_MODEL", "LLM", "Anthropic 模型", "Anthropic 兼容接口当前使用的模型。", "text"),
    ConfigFieldSpec("SANDBOX_TYPE", "Sandbox", "沙箱类型", "执行沙箱后端。", "text"),
    ConfigFieldSpec("DOCKER_IMAGE", "Sandbox", "Docker 镜像", "Docker 沙箱镜像名。", "text"),
    ConfigFieldSpec("WORKER_CONCURRENCY", "Runtime", "工作并发数", "每个 worker 最多并行处理的 actor 数。", "int"),
    ConfigFieldSpec("MR_ACTOR_LEASE_SECONDS", "Runtime", "租约秒数", "Actor 租约 TTL。", "int"),
    ConfigFieldSpec("RUN_HEARTBEAT_SECONDS", "Runtime", "心跳秒数", "租约续期心跳间隔。", "int"),
    ConfigFieldSpec(
        "MENTION_BATCH_WINDOW_SECONDS",
        "Agent",
        "@ 提及合批窗口",
        "同一讨论串 mention 的合批窗口。",
        "int",
    ),
    ConfigFieldSpec(
        "MENTION_SELF_EVOLUTION_ENABLED",
        "Agent",
        "启用 Mention 自我演进",
        "是否启用 Mention agent 的自我演进。",
        "bool",
    ),
    ConfigFieldSpec(
        "MENTION_SELF_EVOLUTION_INTERVAL_DAYS",
        "Agent",
        "Mention 演进间隔天数",
        "Mention 自我演进按固定日历时间每隔多少天运行一次。",
        "int",
    ),
    ConfigFieldSpec(
        "MENTION_SELF_EVOLUTION_TIME_LOCAL",
        "Agent",
        "Mention 演进时间",
        "Mention 自我演进在北京时间的固定触发时间，格式 HH:MM。",
        "text",
    ),
    ConfigFieldSpec("DAILY_AUDIT_ENABLED", "Agent", "启用日常审计", "是否启用每日定时审计 agent。", "bool"),
    ConfigFieldSpec(
        "DAILY_AUDIT_TIMEZONE",
        "Agent",
        "时区",
        "已固定为 Asia/Shanghai（北京时间）；保留旧字段仅为兼容。",
        "text",
        visible=False,
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_START_TIME_LOCAL",
        "Agent",
        "开始时间",
        "每日定时审计在本地时区中的开始时间，格式 HH:MM。",
        "text",
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_MAX_DURATION_MINUTES",
        "Agent",
        "最长执行分钟数",
        "单次日常审计允许占用的最长时间预算。",
        "int",
        visible=False,
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_ENABLE_AUTOFIX",
        "Agent",
        "允许自动修复",
        "高置信且低风险时是否允许自动建分支并提 MR。",
        "bool",
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_ROLLING_ISSUE_TITLE",
        "Agent",
        "Issue 标题前缀",
        "报告型日常审计结果新建 GitLab issue 时使用的标题前缀。",
        "text",
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_SELF_EVOLUTION_ENABLED",
        "Agent",
        "启用 Daily Audit 自我演进",
        "是否启用 Daily Audit agent 的自我演进。",
        "bool",
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_SELF_EVOLUTION_INTERVAL_DAYS",
        "Agent",
        "Daily Audit 演进间隔天数",
        "Daily Audit 自我演进按固定日历时间每隔多少天运行一次。",
        "int",
    ),
    ConfigFieldSpec(
        "DAILY_AUDIT_SELF_EVOLUTION_TIME_LOCAL",
        "Agent",
        "Daily Audit 演进时间",
        "Daily Audit 自我演进在北京时间的固定触发时间，格式 HH:MM。",
        "text",
    ),
    ConfigFieldSpec(
        "AUTO_REVIEW_SELF_EVOLUTION_ENABLED",
        "Agent",
        "启用 Auto Review 自我演进",
        "是否启用 Auto Review agent 的自我演进。",
        "bool",
    ),
    ConfigFieldSpec(
        "AUTO_REVIEW_SELF_EVOLUTION_INTERVAL_DAYS",
        "Agent",
        "Auto Review 演进间隔天数",
        "Auto Review 自我演进按固定日历时间每隔多少天运行一次。",
        "int",
    ),
    ConfigFieldSpec(
        "AUTO_REVIEW_SELF_EVOLUTION_TIME_LOCAL",
        "Agent",
        "Auto Review 演进时间",
        "Auto Review 自我演进在北京时间的固定触发时间，格式 HH:MM。",
        "text",
    ),
    ConfigFieldSpec(
        "AUTO_REVIEW_COMMENT_HISTORY_LIMIT",
        "Agent",
        "机器人评论历史上限",
        "回溯检查的机器人历史评论数。",
        "int",
        visible=False,
    ),
    ConfigFieldSpec(
        "AUTO_REVIEW_HUMAN_COMMENT_LIMIT",
        "Agent",
        "人工评论上限",
        "注入上下文的最近人工评论数。",
        "int",
        visible=False,
    ),
    ConfigFieldSpec("AUTO_REVIEW_FETCH_DEPTH", "Agent", "Fetch 深度", "审查引用分支的 Git fetch 深度。", "int", visible=False),
    ConfigFieldSpec(
        "PHOENIX_TRACING_ENABLED",
        "Phoenix",
        "启用 Phoenix Tracing",
        "启用可选的本地 Phoenix tracing。",
        "bool",
    ),
    ConfigFieldSpec(
        "PHOENIX_COLLECTOR_ENDPOINT",
        "Phoenix",
        "Collector 地址",
        "Phoenix OTLP / collector 地址。",
        "text",
    ),
    ConfigFieldSpec(
        "PHOENIX_API_KEY",
        "Phoenix",
        "API Key",
        "Phoenix 可选 tracing 使用的 API key。",
        "password",
        True,
    ),
    ConfigFieldSpec(
        "PHOENIX_PROJECT_NAME",
        "Phoenix",
        "项目名称",
        "Phoenix 中显示的逻辑项目名。",
        "text",
    ),
    ConfigFieldSpec(
        "PHOENIX_UI_BASE_URL",
        "Phoenix",
        "UI Base URL",
        "Phoenix 页面深链使用的浏览器基础地址。",
        "text",
    ),
)

FIELD_BY_KEY = {item.key: item for item in CONFIG_FIELDS}
_ADMIN_SESSION_SECRET_KEY = "admin_session_secret"


class ControlPlaneService:
    def __init__(self) -> None:
        self.db_path = Path(settings.bootstrap_snapshot().OPEN_REVIEW_DB_PATH)
        ensure_writable_directory(self.db_path.parent)
        self._initialize()
        self._bootstrap_config_if_needed()
        self._bootstrap_internal_state_if_needed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS config_entries (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_account (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS internal_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracked_runs (
                    run_id TEXT PRIMARY KEY,
                    execution_key TEXT,
                    actor_key TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    mr_iid INTEGER,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    error TEXT,
                    head_sha TEXT,
                    note_id INTEGER,
                    discussion_id TEXT,
                    batch_size INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    review_run_id TEXT,
                    review_mode TEXT,
                    compressed_review INTEGER NOT NULL DEFAULT 0,
                    published_findings_count INTEGER NOT NULL DEFAULT 0,
                    suppressed_findings_count INTEGER NOT NULL DEFAULT 0,
                    confirmed_findings_count INTEGER NOT NULL DEFAULT 0,
                    suspicious_findings_count INTEGER NOT NULL DEFAULT 0,
                    open_questions_count INTEGER NOT NULL DEFAULT 0,
                    inline_comments_count INTEGER NOT NULL DEFAULT 0,
                    mention_intent TEXT,
                    mention_status TEXT,
                    mention_degraded_reason TEXT,
                    changed_files_count INTEGER NOT NULL DEFAULT 0,
                    commit_sha TEXT,
                    covered_note_ids_json TEXT NOT NULL DEFAULT '[]',
                    trigger_events_json TEXT NOT NULL DEFAULT '[]',
                    agent_records_json TEXT NOT NULL DEFAULT '[]',
                    published_objects_json TEXT NOT NULL DEFAULT '[]',
                    feedback_events_json TEXT NOT NULL DEFAULT '[]',
                    related_run_ids_json TEXT NOT NULL DEFAULT '[]',
                    published_issue_iid INTEGER,
                    published_merge_request_iid INTEGER,
                    trace_id TEXT,
                    trace_url TEXT,
                    session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS gitlab_identity_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS self_evolution_schedule_state (
                    agent_type TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    last_scheduled_date TEXT,
                    last_manual_triggered_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(agent_type, project_id)
                );
                """
            )
            columns = {
                row["name"]: row
                for row in conn.execute("PRAGMA table_info(tracked_runs)").fetchall()
            }
            if "execution_key" not in columns:
                conn.execute("ALTER TABLE tracked_runs ADD COLUMN execution_key TEXT")
            if "confirmed_findings_count" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN confirmed_findings_count INTEGER NOT NULL DEFAULT 0"
                )
            if "suspicious_findings_count" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN suspicious_findings_count INTEGER NOT NULL DEFAULT 0"
                )
            if "open_questions_count" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN open_questions_count INTEGER NOT NULL DEFAULT 0"
                )
            if "inline_comments_count" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN inline_comments_count INTEGER NOT NULL DEFAULT 0"
                )
            if "trigger_events_json" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN trigger_events_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "agent_records_json" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN agent_records_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "published_objects_json" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN published_objects_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "feedback_events_json" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN feedback_events_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "related_run_ids_json" not in columns:
                conn.execute(
                    "ALTER TABLE tracked_runs ADD COLUMN related_run_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "published_issue_iid" not in columns:
                conn.execute("ALTER TABLE tracked_runs ADD COLUMN published_issue_iid INTEGER")
            if "published_merge_request_iid" not in columns:
                conn.execute("ALTER TABLE tracked_runs ADD COLUMN published_merge_request_iid INTEGER")
            columns = {
                row["name"]: row
                for row in conn.execute("PRAGMA table_info(tracked_runs)").fetchall()
            }
            mr_iid_column = columns.get("mr_iid")
            if (
                "failed_lanes_json" in columns
                or (mr_iid_column is not None and int(mr_iid_column["notnull"]) == 1)
            ):
                conn.executescript(
                    """
                    ALTER TABLE tracked_runs RENAME TO tracked_runs_old;
                    CREATE TABLE tracked_runs (
                        run_id TEXT PRIMARY KEY,
                        execution_key TEXT,
                        actor_key TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        mr_iid INTEGER,
                        event_type TEXT NOT NULL,
                        state TEXT NOT NULL,
                        reason TEXT,
                        error TEXT,
                        head_sha TEXT,
                        note_id INTEGER,
                        discussion_id TEXT,
                        batch_size INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        review_run_id TEXT,
                        review_mode TEXT,
                        compressed_review INTEGER NOT NULL DEFAULT 0,
                        published_findings_count INTEGER NOT NULL DEFAULT 0,
                        suppressed_findings_count INTEGER NOT NULL DEFAULT 0,
                        confirmed_findings_count INTEGER NOT NULL DEFAULT 0,
                        suspicious_findings_count INTEGER NOT NULL DEFAULT 0,
                        open_questions_count INTEGER NOT NULL DEFAULT 0,
                        inline_comments_count INTEGER NOT NULL DEFAULT 0,
                        mention_intent TEXT,
                        mention_status TEXT,
                        mention_degraded_reason TEXT,
                        changed_files_count INTEGER NOT NULL DEFAULT 0,
                        commit_sha TEXT,
                        covered_note_ids_json TEXT NOT NULL DEFAULT '[]',
                        trigger_events_json TEXT NOT NULL DEFAULT '[]',
                        agent_records_json TEXT NOT NULL DEFAULT '[]',
                        published_objects_json TEXT NOT NULL DEFAULT '[]',
                        feedback_events_json TEXT NOT NULL DEFAULT '[]',
                        related_run_ids_json TEXT NOT NULL DEFAULT '[]',
                        published_issue_iid INTEGER,
                        published_merge_request_iid INTEGER,
                        trace_id TEXT,
                        trace_url TEXT,
                        session_id TEXT
                    );
                    INSERT INTO tracked_runs(
                        run_id, execution_key, actor_key, project_id, mr_iid, event_type, state, reason, error,
                        head_sha, note_id, discussion_id, batch_size, started_at, completed_at,
                        review_run_id, review_mode, compressed_review, published_findings_count,
                        suppressed_findings_count, confirmed_findings_count, suspicious_findings_count,
                        open_questions_count, inline_comments_count, mention_intent, mention_status,
                        mention_degraded_reason, changed_files_count, commit_sha, covered_note_ids_json,
                        trigger_events_json, agent_records_json, published_objects_json, feedback_events_json,
                        related_run_ids_json, published_issue_iid, published_merge_request_iid,
                        trace_id, trace_url, session_id
                    )
                    SELECT
                        run_id, execution_key, actor_key, project_id, mr_iid, event_type, state, reason, error,
                        head_sha, note_id, discussion_id, batch_size, started_at, completed_at,
                        review_run_id, review_mode, compressed_review, published_findings_count,
                        suppressed_findings_count, confirmed_findings_count, suspicious_findings_count,
                        open_questions_count, inline_comments_count, mention_intent, mention_status,
                        mention_degraded_reason, changed_files_count, commit_sha, covered_note_ids_json,
                        '[]', '[]', '[]', '[]', '[]', NULL, NULL,
                        trace_id, trace_url, session_id
                    FROM tracked_runs_old;
                    DROP TABLE tracked_runs_old;
                    """
                )

    def _bootstrap_config_if_needed(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM config_entries").fetchall()
            existing_keys = {row["key"] for row in rows}
            snapshot = settings.bootstrap_snapshot().model_dump()
            snapshot.update({row["key"]: _decode_value(row["value_json"]) for row in rows})
            snapshot = coerce_llm_settings(snapshot)
            now = _now()
            for field in CONFIG_FIELDS:
                if field.key in existing_keys:
                    continue
                conn.execute(
                    """
                    INSERT INTO config_entries(key, value_json, updated_at, updated_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    (field.key, _encode_value(snapshot[field.key]), now, "bootstrap"),
                )

    def _bootstrap_internal_state_if_needed(self) -> None:
        if self._get_internal_state_value(_ADMIN_SESSION_SECRET_KEY) is not None:
            return
        self._set_internal_state_value(
            _ADMIN_SESSION_SECRET_KEY,
            secrets.token_urlsafe(48),
        )

    def _get_internal_state_value(self, key: str) -> Any | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM internal_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return _decode_value(row["value_json"])

    def _set_internal_state_value(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO internal_state(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, _encode_value(value), _now()),
            )

    def list_fields(self) -> list[dict[str, Any]]:
        snapshot = self.get_snapshot()
        items = []
        for field in CONFIG_FIELDS:
            if not field.visible:
                continue
            value = snapshot.get(field.key)
            rendered_value = "" if field.sensitive else value
            items.append(
                {
                    "key": field.key,
                    "group": field.group,
                    "label": field.label,
                    "description": field.description,
                    "kind": field.kind,
                    "sensitive": field.sensitive,
                    "configured": bool(value) if field.sensitive else True,
                    "value": rendered_value,
                }
            )
        return items

    def get_snapshot(self) -> dict[str, Any]:
        data = settings.bootstrap_snapshot().model_dump()
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM config_entries").fetchall()
        for row in rows:
            data[row["key"]] = _decode_value(row["value_json"])
        return coerce_llm_settings(data)

    def has_admin_account(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM admin_account WHERE username = 'admin'"
            ).fetchone()
        return row is not None

    def create_initial_admin(self, password: str) -> None:
        if not str(password or "").strip():
            raise ValueError("管理员密码不能为空。")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM admin_account WHERE username = 'admin'"
            ).fetchone()
            if existing is not None:
                raise ValueError("管理员账号已初始化。")
            conn.execute(
                "INSERT INTO admin_account(username, password_hash, updated_at) VALUES (?, ?, ?)",
                ("admin", _hash_password(password), _now()),
            )

    def get_admin_session_secret(self) -> str:
        secret = self._get_internal_state_value(_ADMIN_SESSION_SECRET_KEY)
        if isinstance(secret, str) and secret.strip():
            return secret
        secret = secrets.token_urlsafe(48)
        self._set_internal_state_value(_ADMIN_SESSION_SECRET_KEY, secret)
        return secret

    def set_values(self, values: dict[str, Any], *, actor: str) -> None:
        if not values:
            return
        now = _now()
        typed_values = self._coerce_updates(values)
        if typed_values:
            merged = self.get_snapshot()
            merged.update(typed_values)
            resolved = coerce_llm_settings(merged)
            if any(
                key in typed_values
                for key in {
                    "LLM_MODEL_ID",
                    "LLM_ACTIVE_PROVIDER",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_KEY",
                    "OPENAI_MODEL",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_MODEL",
                }
            ):
                for key in (
                    "LLM_MODEL_ID",
                    "LLM_ACTIVE_PROVIDER",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_KEY",
                    "OPENAI_MODEL",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_MODEL",
                ):
                    typed_values[key] = resolved[key]
        with self._connect() as conn:
            for key, value in typed_values.items():
                conn.execute(
                    """
                    INSERT INTO config_entries(key, value_json, updated_at, updated_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by
                    """,
                    (key, _encode_value(value), now, actor),
                )
                conn.execute(
                    "INSERT INTO config_revisions(key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                    (key, _encode_value(value), now, actor),
                )

    def _coerce_updates(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.get_snapshot()
        result: dict[str, Any] = {}
        for key, raw in values.items():
            field = FIELD_BY_KEY.get(key)
            if not field:
                continue
            if field.sensitive and (raw is None or raw == ""):
                continue
            if field.kind == "bool":
                result[key] = str(raw).lower() in {"1", "true", "on", "yes"}
            elif field.kind == "int":
                result[key] = int(raw)
            elif field.kind == "multiline":
                if isinstance(raw, list):
                    result[key] = [str(item).strip() for item in raw if str(item).strip()]
                else:
                    result[key] = [item.strip() for item in str(raw).splitlines() if item.strip()]
            else:
                result[key] = raw if raw is not None else current.get(key)
        if "GITLAB_TARGET_PROJECTS" in result:
            result["GITLAB_TARGET_PROJECTS"] = normalize_gitlab_project_targets(
                result["GITLAB_TARGET_PROJECTS"],
                api_url=str(result.get("GITLAB_API_URL", current.get("GITLAB_API_URL", ""))),
                external_url=str(result.get("GITLAB_EXTERNAL_URL", current.get("GITLAB_EXTERNAL_URL", ""))),
            )
        return result

    def verify_admin_password(self, password: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM admin_account WHERE username = 'admin'"
            ).fetchone()
        return bool(row and _verify_password(password, row["password_hash"]))

    def set_admin_password(self, password: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE admin_account SET password_hash = ?, updated_at = ? WHERE username = 'admin'",
                (_hash_password(password), _now()),
            )

    def get_cached_gitlab_identity(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM gitlab_identity_cache WHERE cache_key = 'current'"
            ).fetchone()
        if not row:
            return None
        return _decode_value(row["payload_json"])

    def set_cached_gitlab_identity(self, payload: dict[str, Any]) -> None:
        data = dict(payload)
        fetched_at = str(data.get("fetched_at") or _now())
        data["fetched_at"] = fetched_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gitlab_identity_cache(cache_key, payload_json, fetched_at)
                VALUES ('current', ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    fetched_at=excluded.fetched_at
                """,
                (_encode_value(data), fetched_at),
            )

    def get_self_evolution_schedule_state(self, agent_type: str, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT agent_type, project_id, last_scheduled_date, last_manual_triggered_at, updated_at
                FROM self_evolution_schedule_state
                WHERE agent_type = ? AND project_id = ?
                """,
                (agent_type, project_id),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def record_self_evolution_schedule(self, *, agent_type: str, project_id: str, scheduled_date: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO self_evolution_schedule_state(
                    agent_type, project_id, last_scheduled_date, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_type, project_id) DO UPDATE SET
                    last_scheduled_date = excluded.last_scheduled_date,
                    updated_at = excluded.updated_at
                """,
                (agent_type, project_id, scheduled_date, _now()),
            )

    def record_self_evolution_manual_trigger(self, *, agent_type: str, project_id: str, triggered_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO self_evolution_schedule_state(
                    agent_type, project_id, last_manual_triggered_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_type, project_id) DO UPDATE SET
                    last_manual_triggered_at = excluded.last_manual_triggered_at,
                    updated_at = excluded.updated_at
                """,
                (agent_type, project_id, triggered_at, _now()),
            )

    def _get_run_row(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT trigger_events_json, agent_records_json, published_objects_json, feedback_events_json,
                   related_run_ids_json, published_issue_iid, published_merge_request_iid
            FROM tracked_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"tracked run not found: {run_id}")
        return row

    def record_run(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT trigger_events_json, agent_records_json, published_objects_json, feedback_events_json,
                       related_run_ids_json, published_issue_iid, published_merge_request_iid
                FROM tracked_runs
                WHERE run_id = ?
                """,
                (payload["run_id"],),
            ).fetchone()
            trigger_events_json = (
                _encode_value(payload.get("trigger_events", []))
                if "trigger_events" in payload
                else (str(existing["trigger_events_json"]) if existing else "[]")
            )
            agent_records_json = (
                _encode_value(payload.get("agent_records", []))
                if "agent_records" in payload
                else (str(existing["agent_records_json"]) if existing else "[]")
            )
            published_objects_json = (
                _encode_value(payload.get("published_objects", []))
                if "published_objects" in payload
                else (str(existing["published_objects_json"]) if existing else "[]")
            )
            feedback_events_json = (
                _encode_value(payload.get("feedback_events", []))
                if "feedback_events" in payload
                else (str(existing["feedback_events_json"]) if existing else "[]")
            )
            related_run_ids_json = (
                _encode_value(payload.get("related_run_ids", []))
                if "related_run_ids" in payload
                else (str(existing["related_run_ids_json"]) if existing else "[]")
            )
            published_issue_iid = (
                payload.get("published_issue_iid")
                if "published_issue_iid" in payload
                else (existing["published_issue_iid"] if existing else None)
            )
            published_merge_request_iid = (
                payload.get("published_merge_request_iid")
                if "published_merge_request_iid" in payload
                else (existing["published_merge_request_iid"] if existing else None)
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO tracked_runs(
                    run_id, execution_key, actor_key, project_id, mr_iid, event_type, state, reason, error,
                    head_sha, note_id, discussion_id, batch_size, started_at, completed_at,
                    review_run_id, review_mode, compressed_review, published_findings_count,
                    suppressed_findings_count, confirmed_findings_count, suspicious_findings_count,
                    open_questions_count, inline_comments_count, mention_intent, mention_status,
                    mention_degraded_reason, changed_files_count, commit_sha, covered_note_ids_json,
                    trigger_events_json, agent_records_json, published_objects_json, feedback_events_json,
                    related_run_ids_json, published_issue_iid, published_merge_request_iid,
                    trace_id, trace_url, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload.get("execution_key"),
                    payload["actor_key"],
                    payload["project_id"],
                    payload["mr_iid"],
                    payload["event_type"],
                    payload["state"],
                    payload.get("reason"),
                    payload.get("error"),
                    payload.get("head_sha"),
                    payload.get("note_id"),
                    payload.get("discussion_id"),
                    payload.get("batch_size", 1),
                    payload.get("started_at") or _now(),
                    payload.get("completed_at"),
                    payload.get("review_run_id"),
                    payload.get("review_mode"),
                    1 if payload.get("compressed_review") else 0,
                    payload.get("published_findings_count", 0),
                    payload.get("suppressed_findings_count", 0),
                    payload.get("confirmed_findings_count", 0),
                    payload.get("suspicious_findings_count", 0),
                    payload.get("open_questions_count", 0),
                    payload.get("inline_comments_count", 0),
                    payload.get("mention_intent"),
                    payload.get("mention_status"),
                    payload.get("mention_degraded_reason"),
                    payload.get("changed_files_count", 0),
                    payload.get("commit_sha"),
                    _encode_value(payload.get("covered_note_ids", [])),
                    trigger_events_json,
                    agent_records_json,
                    published_objects_json,
                    feedback_events_json,
                    related_run_ids_json,
                    published_issue_iid,
                    published_merge_request_iid,
                    payload.get("trace_id"),
                    payload.get("trace_url"),
                    payload.get("session_id"),
                ),
            )
            conn.commit()

    def _append_json_array_field(
        self,
        run_id: str,
        field_name: str,
        item: Any,
        *,
        dedupe: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_run_row(conn, run_id)
            current = _decode_value(str(row[field_name]))
            values = list(current) if isinstance(current, list) else []
            if not (dedupe and item in values):
                values.append(item)
            conn.execute(
                f"UPDATE tracked_runs SET {field_name} = ? WHERE run_id = ?",
                (_encode_value(values), run_id),
            )
            conn.commit()

    def append_trigger_event(self, run_id: str, payload: dict[str, Any]) -> None:
        self._append_json_array_field(run_id, "trigger_events_json", dict(payload))

    def append_agent_record(self, run_id: str, payload: dict[str, Any]) -> None:
        self._append_json_array_field(run_id, "agent_records_json", dict(payload))

    def append_published_object(self, run_id: str, payload: dict[str, Any]) -> None:
        self._append_json_array_field(run_id, "published_objects_json", dict(payload))

    def append_feedback_event(self, run_id: str, payload: dict[str, Any]) -> None:
        self._append_json_array_field(run_id, "feedback_events_json", dict(payload))

    def append_related_run_id(self, run_id: str, related_run_id: str) -> None:
        self._append_json_array_field(run_id, "related_run_ids_json", related_run_id, dedupe=True)

    def set_published_issue_iid(self, run_id: str, issue_iid: int | None) -> None:
        with self._connect() as conn:
            self._get_run_row(conn, run_id)
            conn.execute(
                "UPDATE tracked_runs SET published_issue_iid = ? WHERE run_id = ?",
                (issue_iid, run_id),
            )

    def set_published_merge_request_iid(self, run_id: str, merge_request_iid: int | None) -> None:
        with self._connect() as conn:
            self._get_run_row(conn, run_id)
            conn.execute(
                "UPDATE tracked_runs SET published_merge_request_iid = ? WHERE run_id = ?",
                (merge_request_iid, run_id),
            )

    def list_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracked_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tracked_runs
                WHERE run_id = ?
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        mr_iid: int | None = None,
        event_type: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tracked_runs WHERE 1 = 1"
        params: list[Any] = []
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        if mr_iid is not None:
            query += " AND mr_iid = ?"
            params.append(mr_iid)
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def list_runs_for_actor(self, actor_key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracked_runs
                WHERE actor_key = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (actor_key, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["compressed_review"] = bool(data["compressed_review"])
        data["covered_note_ids"] = _decode_value(data.pop("covered_note_ids_json"))
        data["trigger_events"] = _decode_value(data.pop("trigger_events_json"))
        data["agent_records"] = _decode_value(data.pop("agent_records_json"))
        data["published_objects"] = _decode_value(data.pop("published_objects_json"))
        data["feedback_events"] = _decode_value(data.pop("feedback_events_json"))
        data["related_run_ids"] = _decode_value(data.pop("related_run_ids_json"))
        return data

def get_config_service() -> ControlPlaneService:
    global _CONFIG_SERVICE
    if _CONFIG_SERVICE is None:
        _CONFIG_SERVICE = ControlPlaneService()
    return _CONFIG_SERVICE


def get_tracking_service() -> ControlPlaneService:
    return get_config_service()


def reset_controlplane_services() -> None:
    global _CONFIG_SERVICE
    _CONFIG_SERVICE = None
