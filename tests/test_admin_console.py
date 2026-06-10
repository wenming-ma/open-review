"""Tests for the built-in admin console."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import agent.admin.router as admin_router
from agent.config import settings
from agent.runtime.models import ActorRuntimeStatus, RunJournalEvent, RunRecord
from agent.webapp import app


def _make_client(tmp_path, monkeypatch, *, initialize_admin: bool = True) -> TestClient:
    db_path = tmp_path / "controlplane.db"
    settings.reset_overrides()
    monkeypatch.setattr(settings, "OPEN_REVIEW_DB_PATH", str(db_path))
    monkeypatch.setattr(settings, "PHOENIX_UI_BASE_URL", "http://phoenix.local")
    monkeypatch.setattr(
        admin_router,
        "resolve_bot_identity",
        lambda **_kwargs: SimpleNamespace(
            identity=SimpleNamespace(
                username="open-review-bot",
                name="Open Review Bot",
                avatar_url="http://gitlab.local/avatar.png",
                user_id=7,
            ),
            source="live",
            error=None,
            fetched_at="2026-04-10T10:00:00+00:00",
        ),
    )
    from agent.controlplane import get_config_service, reset_controlplane_services
    from agent.runtime.queue import reset_runtime_clients
    from agent.sandbox.manager import reset_runtime_sandbox_config

    reset_controlplane_services()
    reset_runtime_clients()
    reset_runtime_sandbox_config()
    service = get_config_service()
    if initialize_admin:
        if not service.has_admin_account():
            service.create_initial_admin("admin-pass")
    monkeypatch.setattr(admin_router, "get_config_service", lambda: service)
    return TestClient(app)


def _patch_runtime_statuses(monkeypatch, statuses: list[ActorRuntimeStatus]) -> None:
    class _FakeStore:
        async def list_actor_statuses(self) -> list[ActorRuntimeStatus]:
            return statuses

        async def list_run_journal(self, execution_key: str):
            from agent.runtime.queue import get_runtime_store

            store = await get_runtime_store()
            return await store.list_run_journal(execution_key)

    async def _fake_get_runtime_store():
        return _FakeStore()

    monkeypatch.setattr(admin_router, "get_runtime_store", _fake_get_runtime_store)


def test_admin_requires_login(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)

    response = client.get("/admin", follow_redirects=False)

    assert response.status_code in {302, 303}
    assert response.headers["location"] == "/admin/login"


def test_admin_requires_setup_before_login_when_uninitialized(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, initialize_admin=False)

    admin_response = client.get("/admin", follow_redirects=False)
    login_response = client.get("/admin/login", follow_redirects=False)

    assert admin_response.status_code in {302, 303}
    assert admin_response.headers["location"] == "/admin/setup"
    assert login_response.status_code in {302, 303}
    assert login_response.headers["location"] == "/admin/setup"


def test_admin_setup_creates_initial_admin_and_redirects_into_console(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, initialize_admin=False)

    setup_page = client.get("/admin/setup")
    assert setup_page.status_code == 200
    assert "初始化管理后台" in setup_page.text

    response = client.post(
        "/admin/setup",
        data={"password": "first-pass"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Open Review 配置" in response.text
    assert "初始化已完成" in response.text

    from agent.controlplane import get_config_service

    service = get_config_service()
    assert service.has_admin_account() is True
    assert service.verify_admin_password("first-pass") is True

    setup_again = client.get("/admin/setup", follow_redirects=False)
    assert setup_again.status_code in {302, 303}
    assert setup_again.headers["location"] == "/admin"


def test_admin_login_page_supports_english_language(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)

    response = client.get("/admin/login?lang=en")

    assert response.status_code == 200
    assert "Admin Login" in response.text
    assert "Password" in response.text
    assert 'data-admin-lang="en"' in response.text
    assert "管理员登录" not in response.text
    assert "open_review_admin_lang=en" in response.headers.get("set-cookie", "")


def test_admin_dashboard_supports_english_language(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _patch_runtime_statuses(monkeypatch, [])
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin?lang=en")

    assert response.status_code == 200
    assert "System Overview" in response.text
    assert "Active Actors" in response.text
    assert "Log Out" in response.text
    assert 'data-admin-lang="en"' in response.text
    assert "系统总览" not in response.text


def test_admin_settings_supports_english_language_from_cookie(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    client.get("/admin/language?lang=en&next=/admin/settings", follow_redirects=False)

    response = client.get("/admin/settings?group=Sandbox")

    assert response.status_code == 200
    assert "Open Review Configuration" in response.text
    assert "Settings Groups" in response.text
    assert "Sandbox Type" in response.text
    assert "Save Settings" in response.text
    assert 'data-admin-lang="en"' in response.text
    assert "Open Review 配置" not in response.text


def test_admin_login_and_overview_page(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _patch_runtime_statuses(
        monkeypatch,
        [
            ActorRuntimeStatus(
                actor_key="team/project!42",
                pending_count=2,
                inflight_count=1,
                lease_owner="worker-1",
                lease_ttl_seconds=17,
                scheduled=True,
            )
        ],
    )

    login_page = client.get("/admin/login")
    assert login_page.status_code == 200
    assert "Open Review 管理后台" in login_page.text
    assert "管理员登录" in login_page.text

    response = client.post(
        "/admin/login",
        data={"password": "admin-pass"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "系统总览" in response.text
    assert "Open Review 管理后台" in response.text
    assert "活跃 Actor" in response.text
    assert "异常运行" in response.text
    assert ">安全<" in response.text
    assert "team/project!42" in response.text
    assert 'class="app-sidebar"' in response.text
    assert "SQLite" in response.text


def test_admin_settings_page_updates_non_sensitive_values(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.post(
        "/admin/settings",
        data={
            "LLM_ACTIVE_PROVIDER": "openai",
            "OPENAI_BASE_URL": "https://openai.local/v1",
            "OPENAI_MODEL": "gpt-4o-mini",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "设置已保存" in response.text

    from agent.controlplane import get_config_service

    snapshot = get_config_service().get_snapshot()
    assert snapshot["LLM_ACTIVE_PROVIDER"] == "openai"
    assert snapshot["OPENAI_BASE_URL"] == "https://openai.local/v1"
    assert snapshot["OPENAI_MODEL"] == "gpt-4o-mini"
    assert snapshot["LLM_MODEL_ID"] == "openai:gpt-4o-mini"


def test_admin_settings_page_hides_sensitive_values_but_keeps_reveal_controls(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_TOKEN": "secret-gitlab-token",
            "GITLAB_WEBHOOK_SECRET": "secret-webhook",
            "OPENAI_API_KEY": "secret-openai-key",
        },
        actor="test-suite",
    )

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert 'value="secret-gitlab-token"' not in response.text
    assert 'value="secret-webhook"' not in response.text
    assert 'placeholder="已配置，留空表示不修改"' in response.text
    assert 'data-secret-toggle' in response.text

    llm_response = client.get("/admin/settings?group=LLM")
    assert llm_response.status_code == 200
    assert 'value="secret-openai-key"' not in llm_response.text
    assert 'data-llm-test="openai"' in llm_response.text


def test_admin_sandbox_settings_show_restart_notice_and_save_flash(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    page = client.get("/admin/settings?group=Sandbox")
    assert page.status_code == 200
    assert "重启 worker" in page.text
    assert 'option value="local"' in page.text
    assert 'option value="docker"' in page.text

    response = client.post(
        "/admin/settings?group=Sandbox",
        data={
            "SANDBOX_TYPE": "docker",
            "DOCKER_IMAGE": "open-review/sandbox:test",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "设置已保存，重启 worker 后生效" in response.text

    from agent.controlplane import get_config_service

    snapshot = get_config_service().get_snapshot()
    assert snapshot["SANDBOX_TYPE"] == "docker"
    assert snapshot["DOCKER_IMAGE"] == "open-review/sandbox:test"


def test_admin_runtime_settings_hide_unused_deployment_fields(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=Runtime")

    assert response.status_code == 200
    assert "Docker 网络" not in response.text
    assert "Open Review 镜像" not in response.text
    assert "Worker 容器名" not in response.text
    assert "Phoenix 容器名" not in response.text
    assert "Phoenix DB 容器名" not in response.text
    assert "Phoenix 镜像" not in response.text
    assert "Postgres 镜像" not in response.text
    assert "Mention 改动文件上限" not in response.text


def test_admin_review_settings_hide_static_analysis_toggles(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=Review")

    assert response.status_code == 200
    assert "启用 Clang-Tidy" not in response.text
    assert "启用 Cppcheck" not in response.text
    assert "静态分析文件上限" not in response.text
    assert "Lane 超时" not in response.text
    assert "最大发布问题数" not in response.text
    assert "机器人评论历史上限" not in response.text
    assert "人工评论上限" not in response.text
    assert "Fetch 深度" not in response.text


def test_admin_daily_audit_settings_hide_limit_fields(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=Daily Audit")

    assert response.status_code == 200
    assert "自动修复文件上限" not in response.text
    assert "自动修复行数上限" not in response.text
    assert "演进最小样本数" not in response.text
    assert "演进新样本下限" not in response.text
    assert "演进冷却小时数" not in response.text
    assert "最长执行分钟数" not in response.text


def test_admin_runs_page_shows_phoenix_trace_links(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_tracking_service

    get_tracking_service().record_run(
        {
            "run_id": "run-1",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "succeeded",
            "reason": "published",
            "batch_size": 1,
            "started_at": "2026-04-09T10:00:00+00:00",
            "completed_at": "2026-04-09T10:01:00+00:00",
            "trace_id": "trace-123",
            "session_id": "team/project!42",
        }
    )

    response = client.get("/admin/runs")

    assert response.status_code == 200
    assert "http://phoenix.local/redirects/traces/trace-123" in response.text
    assert "http://phoenix.local/redirects/sessions/team%2Fproject%2142" in response.text


def test_admin_actors_page_shows_runtime_status_and_last_run(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _patch_runtime_statuses(
        monkeypatch,
        [
            ActorRuntimeStatus(
                actor_key="team/project!42",
                pending_count=1,
                inflight_count=1,
                lease_owner="worker-a",
                lease_ttl_seconds=25,
                scheduled=True,
            )
        ],
    )
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_tracking_service

    get_tracking_service().record_run(
        {
            "run_id": "run-actor",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "running",
            "reason": "lane execution",
            "batch_size": 1,
            "started_at": "2026-04-09T10:00:00+00:00",
        }
    )

    response = client.get("/admin/actors")

    assert response.status_code == 200
    assert "Actor 列表" in response.text
    assert "team/project!42" in response.text
    assert "worker-a" in response.text
    assert "自动审查" in response.text


def test_build_actor_summaries_reuses_prepared_runs(monkeypatch):
    prepared_run = {
        "run_id": "run-1",
        "actor_key": "team/project!42",
        "event_type": "auto_review",
        "state": "succeeded",
        "reason": "published",
        "started_at": "2026-04-09T10:00:00+00:00",
        "started_display": "2026-04-09 18:00:00 北京时间",
        "completed_display": "2026-04-09 18:01:00 北京时间",
        "state_display": "成功",
        "event_type_display": "自动审查",
        "reason_display": "已发布结果",
        "trace_href": "http://phoenix.local/redirects/traces/trace-1",
        "session_href": "http://phoenix.local/redirects/sessions/team%2Fproject%2142",
    }

    monkeypatch.setattr(
        admin_router,
        "_normalize_run",
        lambda _item: (_ for _ in ()).throw(AssertionError("prepared runs must not be normalized again")),
    )

    summaries = admin_router._build_actor_summaries([], [prepared_run])

    assert summaries[0]["actor_key"] == "team/project!42"
    assert summaries[0]["last_run_state"] == "succeeded"


def test_render_actor_detail_reuses_prepared_runs(monkeypatch):
    actor_summary = {
        "actor_key": "team/project!42",
        "pending_count": 0,
        "inflight_count": 0,
        "scheduled": False,
        "lease_owner": None,
        "lease_ttl_seconds": None,
        "last_run_state": "succeeded",
        "last_event_type": "auto_review",
        "last_started_display": "2026-04-09 18:00:00 北京时间",
    }
    prepared_run = {
        "run_id": "run-1",
        "actor_key": "team/project!42",
        "event_type": "auto_review",
        "state": "succeeded",
        "reason": "published",
        "started_at": "2026-04-09T10:00:00+00:00",
        "started_display": "2026-04-09 18:00:00 北京时间",
        "completed_display": "2026-04-09 18:01:00 北京时间",
        "state_display": "成功",
        "event_type_display": "自动审查",
        "reason_display": "已发布结果",
        "trace_href": "http://phoenix.local/redirects/traces/trace-1",
        "session_href": "http://phoenix.local/redirects/sessions/team%2Fproject%2142",
        "journal": [],
    }

    monkeypatch.setattr(
        admin_router,
        "_normalize_run",
        lambda _item: (_ for _ in ()).throw(AssertionError("prepared actor runs must not be normalized again")),
    )

    response = admin_router._render_actor_detail(
        actor_summary,
        [prepared_run],
        "team/project!42",
        pending_events=[],
        running_run=None,
        supported_controls=True,
    )

    assert "run-1" in response
    assert "http://phoenix.local/redirects/traces/trace-1" in response


def test_admin_dashboard_reads_snapshot_only_a_constant_number_of_times(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    _patch_runtime_statuses(monkeypatch, [])

    from agent.controlplane import get_config_service, get_tracking_service

    for index in range(3):
        get_tracking_service().record_run(
            {
                "run_id": f"run-{index}",
                "actor_key": f"team/project!{index}",
                "project_id": "team/project",
                "mr_iid": index,
                "event_type": "auto_review",
                "state": "succeeded",
                "reason": "published",
                "batch_size": 1,
                "started_at": f"2026-04-09T10:0{index}:00+00:00",
                "completed_at": f"2026-04-09T10:1{index}:00+00:00",
                "trace_id": f"trace-{index}",
                "session_id": f"team/project!{index}",
            }
        )

    service = get_config_service()
    original_get_snapshot = service.get_snapshot
    snapshot_calls = 0

    def _counting_get_snapshot():
        nonlocal snapshot_calls
        snapshot_calls += 1
        return original_get_snapshot()

    monkeypatch.setattr(service, "get_snapshot", _counting_get_snapshot)

    response = client.get("/admin")

    assert response.status_code == 200
    assert snapshot_calls <= 3


def test_attach_run_journal_can_limit_loaded_entries(monkeypatch):
    journal_entries = [
        RunJournalEvent(
            execution_key="exec-1",
            run_id="run-1",
            actor_key="team/project!42",
            scene="auto_review",
            workflow_version="auto_review.v1",
            stage_key=f"stage-{index}",
            event_type="stage_completed",
            status="completed",
            summary=f"summary-{index}",
            created_at=f"2026-04-09T10:0{index}:00+00:00",
        )
        for index in range(3)
    ]

    class _FakeStore:
        async def list_run_journal(self, execution_key: str, limit: int | None = None):
            assert execution_key == "exec-1"
            if limit is None:
                return list(journal_entries)
            return list(journal_entries[-limit:])

    async def _fake_get_runtime_store():
        return _FakeStore()

    monkeypatch.setattr(admin_router, "get_runtime_store", _fake_get_runtime_store)

    runs = asyncio.run(
        admin_router._attach_run_journal(
            [
                {
                    "run_id": "run-1",
                    "execution_key": "exec-1",
                    "actor_key": "team/project!42",
                    "event_type": "auto_review",
                    "state": "running",
                    "started_at": "2026-04-09T10:00:00+00:00",
                }
            ],
            journal_limit=2,
        )
    )

    assert [item["summary"] for item in runs[0]["journal"]] == ["summary-1", "summary-2"]


def test_admin_runs_page_filters_by_state_event_and_query(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_tracking_service

    get_tracking_service().record_run(
        {
            "run_id": "run-failed-mention",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "mention",
            "state": "failed",
            "reason": "broken branch",
            "batch_size": 1,
            "started_at": "2026-04-09T10:00:00+00:00",
        }
    )
    get_tracking_service().record_run(
        {
            "run_id": "run-success-review",
            "actor_key": "team/project!43",
            "project_id": "team/project",
            "mr_iid": 43,
            "event_type": "auto_review",
            "state": "succeeded",
            "reason": "published",
            "batch_size": 1,
            "started_at": "2026-04-09T09:00:00+00:00",
        }
    )

    response = client.get("/admin/runs?state=failed&event_type=mention&q=broken")

    assert response.status_code == 200
    assert "run-failed-mention" in response.text
    assert "run-success-review" not in response.text
    assert "team/project!43" not in response.text


def test_admin_runs_page_shows_daily_audit_entries(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_tracking_service

    get_tracking_service().record_run(
        {
            "run_id": "run-daily-audit",
            "actor_key": "team/project!daily_audit",
            "project_id": "team/project",
            "mr_iid": None,
            "event_type": "daily_audit",
            "state": "succeeded",
            "reason": "reported",
            "batch_size": 1,
            "started_at": "2026-04-09T08:00:00+00:00",
        }
    )

    response = client.get("/admin/runs?event_type=daily_audit")

    assert response.status_code == 200
    assert "run-daily-audit" in response.text
    assert "日常审计" in response.text


def test_admin_actor_detail_page_shows_review_and_mention_fields(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    _patch_runtime_statuses(monkeypatch, [])

    from agent.controlplane import get_tracking_service
    from agent.runtime.queue import get_runtime_store

    get_tracking_service().record_run(
        {
            "run_id": "run-review",
            "execution_key": "exec-review",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "auto_review",
            "state": "stale",
            "reason": "head changed",
            "batch_size": 1,
            "started_at": "2026-04-09T10:00:00+00:00",
            "review_mode": "incremental",
            "compressed_review": True,
            "confirmed_findings_count": 2,
            "suspicious_findings_count": 1,
            "open_questions_count": 1,
            "inline_comments_count": 1,
        }
    )
    get_tracking_service().record_run(
        {
            "run_id": "run-mention",
            "execution_key": "exec-mention",
            "actor_key": "team/project!42",
            "project_id": "team/project",
            "mr_iid": 42,
            "event_type": "mention",
            "state": "succeeded",
            "reason": "replied",
            "batch_size": 1,
            "started_at": "2026-04-09T11:00:00+00:00",
            "mention_intent": "code_change",
            "mention_status": "pushed",
            "mention_degraded_reason": "none",
            "changed_files_count": 2,
            "commit_sha": "abc1234",
            "covered_note_ids": [101, 102],
        }
    )
    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.record_run_journal_event(
            RunJournalEvent(
                execution_key="exec-review",
                run_id="run-review",
                actor_key="team/project!42",
                scene="auto_review",
                workflow_version="auto_review.v1",
                stage_key="preflight",
                event_type="stage_completed",
                status="completed",
                summary="stale head detected before sandbox setup",
            )
        )
    )
    asyncio.run(
        store.record_run_journal_event(
            RunJournalEvent(
                execution_key="exec-mention",
                run_id="run-mention",
                actor_key="team/project!42",
                scene="mention",
                workflow_version="mention.v1",
                stage_key="scene_execute",
                event_type="stage_completed",
                status="completed",
                summary="reply published to discussion",
            )
        )
    )

    response = client.get("/admin/mrs/team/project!42")

    assert response.status_code == 200
    assert "审查模式" in response.text
    assert "incremental" in response.text
    assert "code_change" in response.text
    assert "abc1234" in response.text
    assert "覆盖的 note" in response.text
    assert "执行时间线" in response.text
    assert "stale head detected before sandbox setup" in response.text
    assert "失败的 Lane" not in response.text

    api_response = client.get("/admin/api/actors/team/project!42")
    assert api_response.status_code == 200
    payload = api_response.json()
    assert payload["runs"][0]["execution_key"] in {"exec-review", "exec-mention"}
    assert any(item["summary"] == "reply published to discussion" for item in payload["runs"][0]["journal"])


def test_admin_actor_detail_page_shows_pending_queue_and_terminate_controls(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-auto-pending",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                source_branch="feature/router",
                target_branch="main",
                title="Auto review pending",
                head_sha="head-pending",
                payload={"kind": "merge_request"},
            )
        )
    )
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-mention-pending",
                event_type="mention",
                project_id="team/project",
                mr_iid=42,
                source_branch="feature/router",
                target_branch="main",
                title="Mention pending",
                note_id=202,
                discussion_id="disc-1",
                note_author="developer",
                note_body="please fix this",
                payload={"kind": "note"},
            )
        )
    )
    asyncio.run(
        store.write_run(
            RunRecord(
                run_id="run-running",
                actor_key="team/project!42",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                state="running",
                started_at="2026-04-10T10:00:00+08:00",
            )
        )
    )

    response = client.get("/admin/mrs/team/project!42")

    assert response.status_code == 200
    assert "待处理队列" in response.text
    assert "evt-auto-pending" in response.text
    assert "evt-mention-pending" in response.text
    assert "run-running" in response.text
    assert "终止当前运行" in response.text
    assert "取消排队任务" in response.text


def test_admin_api_actor_detail_includes_pending_events_and_running_run(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-daily-pending",
                event_type="daily_audit",
                project_id="team/project",
                mr_iid=None,
                source_branch="main",
                target_branch="main",
                title="Manual daily audit",
                payload={"kind": "daily_audit", "trigger": "admin-manual"},
            )
        )
    )
    asyncio.run(
        store.write_run(
            RunRecord(
                run_id="run-daily-running",
                actor_key="team/project!daily_audit",
                event_type="daily_audit",
                project_id="team/project",
                state="running",
                started_at="2026-04-10T10:00:00+08:00",
            )
        )
    )

    response = client.get("/admin/api/actors/team/project!daily_audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_events"][0]["event_id"] == "evt-daily-pending"
    assert payload["pending_events"][0]["trigger_source"] == "admin-manual"
    assert payload["running_run"]["run_id"] == "run-daily-running"


def test_admin_actor_detail_page_shows_termination_request_state(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.write_run(
            RunRecord(
                run_id="run-running",
                actor_key="team/project!42",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                state="running",
                started_at="2026-04-10T10:00:00+08:00",
            )
        )
    )
    asyncio.run(store.request_run_termination("run-running", actor_key="team/project!42", requested_by="admin"))

    response = client.get("/admin/mrs/team/project!42")

    assert response.status_code == 200
    assert "run-running" in response.text
    assert "终止请求已发送" in response.text
    assert "等待运行到取消检查点" in response.text
    assert "终止当前运行" not in response.text

    api_response = client.get("/admin/api/actors/team/project!42")
    assert api_response.status_code == 200
    payload = api_response.json()
    assert payload["running_run"]["termination_requested"] is True
    assert payload["running_run"]["terminatable"] is False


def test_admin_cancel_pending_event_api_removes_target_event(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-auto-pending",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                source_branch="feature/router",
                title="Auto review pending",
                head_sha="head-pending",
                payload={"kind": "merge_request"},
            )
        )
    )
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-mention-pending",
                event_type="mention",
                project_id="team/project",
                mr_iid=42,
                source_branch="feature/router",
                note_id=202,
                discussion_id="disc-1",
                note_body="please fix this",
                note_author="developer",
                title="Mention pending",
                payload={"kind": "note"},
            )
        )
    )

    response = client.post("/admin/api/actors/team/project!42/pending/evt-auto-pending/cancel")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    remaining = asyncio.run(store.list_actor_events("team/project!42"))
    assert [item.event_id for item in remaining] == ["evt-mention-pending"]


def test_admin_terminate_running_run_api_records_termination_request(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.write_run(
            RunRecord(
                run_id="run-running",
                actor_key="team/project!42",
                event_type="mention",
                project_id="team/project",
                mr_iid=42,
                state="running",
                started_at="2026-04-10T10:00:00+08:00",
            )
        )
    )

    response = client.post("/admin/api/actors/team/project!42/runs/run-running/terminate")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    termination = asyncio.run(store.get_run_termination("run-running"))
    assert termination is not None
    assert termination.requested_by == "admin"


def test_admin_terminate_running_run_api_clears_same_actor_pending_events(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.runtime.queue import get_runtime_store

    store = asyncio.run(get_runtime_store())
    asyncio.run(
        store.write_run(
            RunRecord(
                run_id="run-running",
                actor_key="team/project!42",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                state="running",
                started_at="2026-04-10T10:00:00+08:00",
            )
        )
    )
    asyncio.run(
        store.append_event(
            admin_router.EventEnvelope(
                event_id="evt-next-auto-review",
                event_type="auto_review",
                project_id="team/project",
                mr_iid=42,
                source_branch="feature/router",
                title="Queued follow-up auto review",
                head_sha="head-follow-up",
                payload={"kind": "merge_request"},
            )
        )
    )

    response = client.post("/admin/api/actors/team/project!42/runs/run-running/terminate")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["cancelled_pending_events"] == ["evt-next-auto-review"]
    assert asyncio.run(store.get_run_termination("run-running")) is not None
    assert asyncio.run(store.list_actor_events("team/project!42")) == []


def test_admin_settings_page_groups_fields_into_tabs(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {"GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"]},
        actor="test-suite",
    )

    response = client.get("/admin/settings?group=Agent")

    assert response.status_code == 200
    assert "Open Review 配置" in response.text
    assert ">Agent<" in response.text
    assert "同一讨论串 mention 的合批窗口" in response.text
    assert 'data-agent-project="team/service"' in response.text
    assert 'data-agent-project="team/webapp"' in response.text
    assert 'name="PROJECT_AGENT_CONFIG::team/service::MENTION_ENABLED"' in response.text
    assert 'name="PROJECT_AGENT_CONFIG::team/webapp::DAILY_AUDIT_ENABLED"' in response.text
    assert "GitLab 地址" not in response.text
    assert ">过滤<" not in response.text
    assert ">日常审计<" not in response.text
    assert ">审查<" not in response.text
    assert 'class="settings-sidebar"' in response.text


def test_admin_settings_page_no_longer_shows_password_panel(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=LLM")

    assert response.status_code == 200
    assert "管理员密码" not in response.text


def test_admin_llm_settings_page_renders_translated_copy(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=LLM")

    assert response.status_code == 200
    assert "模型服务" in response.text
    assert "当前支持 OpenAI 兼容接口和 Anthropic 兼容接口" in response.text
    assert "<code>LLM_MODEL_ID</code>" in response.text
    assert "{_te(" not in response.text


def test_admin_agent_settings_page_shows_global_self_evolution_controls(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_TARGET_PROJECTS": ["team/service"],
            "SELF_EVOLUTION_TIME_LOCAL": "02:00",
        },
        actor="test-suite",
    )

    response = client.get("/admin/settings?group=Agent")

    assert response.status_code == 200
    assert 'name="SELF_EVOLUTION_ENABLED"' in response.text
    assert 'name="SELF_EVOLUTION_INTERVAL_DAYS"' in response.text
    assert 'name="SELF_EVOLUTION_TIME_LOCAL"' in response.text
    assert 'name="MENTION_SELF_EVOLUTION_TIME_LOCAL"' not in response.text
    assert 'name="AUTO_REVIEW_SELF_EVOLUTION_TIME_LOCAL"' not in response.text
    assert 'name="DAILY_AUDIT_SELF_EVOLUTION_TIME_LOCAL"' not in response.text
    assert '<option value="02:00" selected>' in response.text
    assert 'data-agent-section="mention-runtime"' in response.text
    assert 'data-agent-section="auto-review-runtime"' in response.text
    assert 'data-agent-section="daily-audit-runtime"' in response.text
    assert 'data-agent-section="self-evolution"' in response.text
    assert 'data-self-evolution-agent=' not in response.text
    assert 'data-self-evolution-status="global"' in response.text
    assert "当前目标项目" not in response.text
    assert response.text.count(">立即触发</button>") == 1
    assert 'name="DAILY_AUDIT_TIMEZONE"' not in response.text
    assert "滚动 Issue 标题" not in response.text
    assert "Issue 标题前缀" in response.text


def test_admin_agent_settings_page_groups_runtime_and_self_evolution_under_each_agent(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {"GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"]},
        actor="test-suite",
    )

    response = client.get("/admin/settings?group=Agent")

    assert response.status_code == 200
    assert 'data-agent-project="team/service"' in response.text
    assert 'data-agent-project="team/webapp"' in response.text
    assert 'data-agent-section="mention-runtime"' in response.text
    assert 'data-agent-section="auto-review-runtime"' in response.text
    assert 'data-agent-section="daily-audit-runtime"' in response.text
    assert 'data-agent-section="self-evolution"' in response.text
    assert 'data-agent-card="self_evolution"' in response.text


def test_admin_self_evolution_trigger_enqueues_global_events(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {"GITLAB_TARGET_PROJECTS": ["team/project-a", "team/project-b"]},
        actor="test-suite",
    )
    monkeypatch.setattr(admin_router, "get_project_default_branch", lambda _project_id: "main")

    response = client.post("/admin/api/self-evolution/trigger", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["scheduled_count"] == 2
    assert payload["agent_type"] == "all"
    assert payload["agent_types"] == ["mention", "auto_review", "daily_audit"]
    assert payload["results"][0]["actor_key"] == "team/project-a!self_evolution"
    assert payload["results"][1]["actor_key"] == "team/project-b!self_evolution"


def test_admin_project_agent_config_api_updates_one_project(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {"GITLAB_TARGET_PROJECTS": ["team/project-a", "team/project-b"]},
        actor="test-suite",
    )

    response = client.post(
        "/admin/api/project-agent-configs",
        json={
            "project_id": "team/project-a",
            "values": {
                "MENTION_ENABLED": False,
                "DAILY_AUDIT_ENABLED": True,
                "DAILY_AUDIT_START_TIME_LOCAL": "04:30",
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["values"]["MENTION_ENABLED"] is False
    assert payload["values"]["DAILY_AUDIT_ENABLED"] is True
    assert get_config_service().get_project_agent_config("team/project-b")["MENTION_ENABLED"] is True


def test_admin_runtime_settings_only_show_runtime_controls(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=Runtime")

    assert response.status_code == 200
    assert "工作并发数" in response.text
    assert "租约秒数" in response.text
    assert "心跳秒数" in response.text
    assert "同一讨论串 mention 的合批窗口" not in response.text
    assert "立即触发日常审计" not in response.text


def test_admin_timestamp_helpers_render_beijing_time_and_compare_real_instants(monkeypatch):
    formatted = admin_router._format_timestamp("2026-04-10T10:00:00+00:00")
    rendered = admin_router._render_timestamp_cell(formatted)

    assert formatted == "2026-04-10 18:00:00 北京时间"
    assert 'timestamp-zone">北京时间<' in rendered

    monkeypatch.setattr(
        admin_router,
        "now_in_open_review_tz",
        lambda: admin_router.parse_iso_datetime("2026-04-15T08:30:00+08:00"),
    )
    token = admin_router._sign_session({"expires_at": "2026-04-15T01:00:00+00:00"})

    assert admin_router._decode_session(token) is not None


def test_admin_security_page_updates_password(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.post(
        "/admin/security/password",
        data={"password": "new-pass"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "密码已更新" in response.text

    client = _make_client(tmp_path, monkeypatch)
    relogin = client.post("/admin/login", data={"password": "new-pass"}, follow_redirects=False)
    assert relogin.status_code in {302, 303}


def test_admin_gitlab_settings_page_shows_live_identity_and_hides_editable_bot_username(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert "Open Review 配置" in response.text
    assert "当前 Token 身份" in response.text
    assert "Open Review Bot" in response.text
    assert "open-review-bot" in response.text
    assert "实时身份" in response.text
    assert "机器人用户名" not in response.text
    assert "GITLAB_TOKEN 决定 GitLab 上实际发言的用户名" not in response.text
    assert "GitLab API 地址" in response.text
    assert "GitLab 外部地址" in response.text
    assert "验证 GitLab 连接" in response.text
    assert "配置/同步 Webhook" in response.text


def test_admin_gitlab_settings_page_shows_project_target_fields(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert 'name="GITLAB_API_URL"' not in response.text
    assert 'name="GITLAB_EXTERNAL_URL"' not in response.text
    assert 'name="GITLAB_API_URL_OVERRIDE"' in response.text
    assert "仓库链接" in response.text
    assert "自动推断 GitLab 外部地址" in response.text
    assert "高级设置" in response.text


def test_admin_gitlab_settings_page_shows_cached_identity_status(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        admin_router,
        "resolve_bot_identity",
        lambda **_kwargs: SimpleNamespace(
            identity=SimpleNamespace(
                username="open-review-bot",
                name="Open Review Bot",
                avatar_url="http://gitlab.local/avatar.png",
                user_id=7,
            ),
            source="cached",
            error="GitLab temporarily unavailable",
            fetched_at="2026-04-10T09:00:00+00:00",
        ),
    )
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert "当前 Token 身份" in response.text
    assert "缓存身份" in response.text
    assert "GitLab temporarily unavailable" in response.text


def test_admin_gitlab_settings_page_shows_same_as_api_url_button(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert 'data-gitlab-project-list' in response.text
    assert 'data-gitlab-targets-input' in response.text
    assert 'name="GITLAB_API_URL_OVERRIDE"' in response.text
    assert 'name="GITLAB_API_URL"' not in response.text
    assert 'name="GITLAB_EXTERNAL_URL"' not in response.text
    assert 'name="GITLAB_TARGET_GROUP"' not in response.text
    assert 'name="GITLAB_TARGET_PROJECT"' not in response.text
    assert "仓库链接" in response.text
    assert "自动推断 GitLab 外部地址" in response.text
    assert "高级设置" in response.text


def test_admin_gitlab_settings_page_renders_repo_urls_from_configured_targets(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "GITLAB_TARGET_PROJECTS": ["team/service"],
        },
        actor="test-suite",
    )

    response = client.get("/admin/settings?group=GitLab")

    assert response.status_code == 200
    assert 'value="https://gitlab.example.com/team/service.git"' in response.text


def test_admin_gitlab_settings_page_saves_project_list_as_canonical_paths(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
        },
        actor="test-suite",
    )

    response = client.post(
        "/admin/settings?group=GitLab",
        data={
            "GITLAB_TARGET_PROJECTS": (
                "https://gitlab.example.com/team/service.git\n\n"
                " team/webapp \n"
                "https://gitlab.example.com/team/service/\n"
            ),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    snapshot = get_config_service().get_snapshot()
    assert snapshot["GITLAB_TARGET_PROJECTS"] == ["team/service", "team/webapp"]
    assert snapshot["GITLAB_EXTERNAL_URL"] == "https://gitlab.example.com"
    assert snapshot["GITLAB_API_URL"] == "https://gitlab.example.com"


def test_admin_gitlab_settings_page_uses_api_override_when_provided(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    response = client.post(
        "/admin/settings?group=GitLab",
        data={
            "GITLAB_TARGET_PROJECTS": "https://gitlab.example.com/team/service.git\n",
            "GITLAB_API_URL_OVERRIDE": "https://gitlab-api.internal",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    snapshot = get_config_service().get_snapshot()
    assert snapshot["GITLAB_EXTERNAL_URL"] == "https://gitlab.example.com"
    assert snapshot["GITLAB_API_URL"] == "https://gitlab-api.internal"


def test_admin_gitlab_settings_page_updates_existing_repo_url_host(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_API_URL": "https://gitlab-old.example.com",
            "GITLAB_EXTERNAL_URL": "https://gitlab-old.example.com",
            "GITLAB_TARGET_PROJECTS": ["team/service"],
        },
        actor="test-suite",
    )

    response = client.post(
        "/admin/settings?group=GitLab",
        data={
            "GITLAB_TARGET_PROJECTS": "https://gitlab-new.example.com/team/service.git\n",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    snapshot = get_config_service().get_snapshot()
    assert snapshot["GITLAB_TARGET_PROJECTS"] == ["team/service"]
    assert snapshot["GITLAB_EXTERNAL_URL"] == "https://gitlab-new.example.com"
    assert snapshot["GITLAB_API_URL"] == "https://gitlab-new.example.com"


def test_admin_gitlab_settings_page_rejects_invalid_project_url(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "GITLAB_API_URL": "https://gitlab-api.example.com",
            "GITLAB_EXTERNAL_URL": "https://gitlab.example.com",
            "GITLAB_TARGET_PROJECTS": ["team/service"],
        },
        actor="test-suite",
    )

    response = client.post(
        "/admin/settings?group=GitLab",
        data={"GITLAB_TARGET_PROJECTS": "https://github.com/wenming-ma/open-review.git\n"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "只支持当前 GitLab 实例的 HTTPS 仓库 URL" in response.text
    assert get_config_service().get_snapshot()["GITLAB_TARGET_PROJECTS"] == ["team/service"]


def test_admin_gitlab_copy_button_script_supports_overwrite_and_empty_source():
    script = Path("agent/admin/static/admin.js").read_text(encoding="utf-8")

    assert "data-gitlab-project-list" in script
    assert "data-gitlab-targets-input" in script
    assert "data-gitlab-project-add" in script
    assert "data-gitlab-project-remove" in script
    assert "syncHiddenInput" in script


def test_admin_gitlab_script_no_longer_contains_target_mode_toggle():
    script = Path("agent/admin/static/admin.js").read_text(encoding="utf-8")

    assert "setupGitlabTargetMode" not in script
    assert 'input[name="GITLAB_TARGET_KIND"]' not in script
    assert '[data-gitlab-target-field]' not in script


def test_admin_gitlab_verify_api_returns_live_check_results(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    calls = {}

    async def fake_to_thread(func, *args, **kwargs):
        calls["func"] = func
        return func(*args, **kwargs)

    monkeypatch.setattr(admin_router.asyncio, "to_thread", fake_to_thread)

    monkeypatch.setattr(
        admin_router,
        "verify_gitlab_configuration",
        lambda: {
            "status": "ready",
            "api_url": "https://gitlab-api.example.com",
            "external_url": "https://gitlab.example.com",
            "target_projects": ["team/service", "team/webapp"],
            "webhook_url": "https://open_review.example.com/webhooks/gitlab",
            "checks": [
                {"key": "api", "status": "ok", "message": "GitLab API 可达。"},
                {"key": "webhook", "status": "ok", "message": "Webhook healthz 可达。"},
            ],
            "results": [
                {"project_path": "team/service", "status": "ok", "detail": "Project 可访问。"},
                {"project_path": "team/webapp", "status": "ok", "detail": "Project 可访问。"},
            ],
        },
    )

    response = client.post("/admin/api/gitlab/verify")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"][0]["key"] == "api"
    assert payload["checks"][1]["message"] == "Webhook healthz 可达。"
    assert calls["func"] is admin_router.verify_gitlab_configuration


def test_admin_gitlab_verify_api_localizes_english_payload(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    client.get("/admin/language?lang=en&next=/admin/settings", follow_redirects=False)

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(admin_router.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        admin_router,
        "verify_gitlab_configuration",
        lambda: {
            "status": "ready",
            "checks": [
                {"key": "api_url", "status": "ok", "message": "GitLab API 地址：https://gitlab-api.example.com"},
                {"key": "api", "status": "ok", "message": "GitLab API 可达。"},
                {"key": "bot_identity", "status": "ok", "message": "当前 Token 对应用户：open-review-bot。"},
                {"key": "target_access", "status": "ok", "message": "2 / 2 个 GitLab Project 可访问。"},
            ],
            "results": [
                {"project_path": "team/service", "status": "ok", "detail": "Project 可访问。"},
            ],
        },
    )

    response = client.post("/admin/api/gitlab/verify")

    assert response.status_code == 200
    payload = response.json()
    assert payload["checks"][0]["message"] == "GitLab API URL: https://gitlab-api.example.com"
    assert payload["checks"][1]["message"] == "GitLab API is reachable."
    assert payload["checks"][2]["message"] == "Current token user: open-review-bot."
    assert payload["checks"][3]["message"] == "2 / 2 GitLab projects are accessible."
    assert payload["results"][0]["detail"] == "Project is accessible."


def test_admin_gitlab_webhook_sync_api_returns_manual_fallback_details(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    calls = {}

    async def fake_to_thread(func, *args, **kwargs):
        calls["func"] = func
        return func(*args, **kwargs)

    monkeypatch.setattr(admin_router.asyncio, "to_thread", fake_to_thread)

    monkeypatch.setattr(
        admin_router,
        "sync_gitlab_webhooks",
        lambda: {
            "status": "partial",
            "webhook_url": "https://open_review.example.com/webhooks/gitlab",
            "target_projects": ["team/service", "team/webapp"],
            "results": [
                {
                    "project_id": 7,
                    "project_path": "team/service",
                    "status": "updated",
                    "detail": "Webhook 已更新。",
                },
                {
                    "project_id": 8,
                    "project_path": "team/webapp",
                    "status": "error",
                    "detail": "403 Forbidden",
                },
            ],
            "manual_instructions": "请手工为 team/webapp 创建 Project Hook。",
        },
    )

    response = client.post("/admin/api/gitlab/webhooks/sync")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["results"][1]["project_path"] == "team/webapp"
    assert payload["manual_instructions"] == "请手工为 team/webapp 创建 Project Hook。"
    assert calls["func"] is admin_router.sync_gitlab_webhooks


def test_admin_gitlab_webhook_sync_api_localizes_english_payload(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)
    client.get("/admin/language?lang=en&next=/admin/settings", follow_redirects=False)

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(admin_router.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        admin_router,
        "sync_gitlab_webhooks",
        lambda: {
            "status": "partial",
            "results": [
                {"project_path": "team/service", "status": "created", "detail": "Webhook 已创建。"},
                {"project_path": "team/app", "status": "updated", "detail": "Webhook 已更新。"},
            ],
            "manual_instructions": "请手工为 team/service 创建 Project Hook。",
        },
    )

    response = client.post("/admin/api/gitlab/webhooks/sync")

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["detail"] == "Webhook created."
    assert payload["results"][1]["detail"] == "Webhook updated."
    assert payload["manual_instructions"] == "Manually create a Project Hook for team/service."


def test_admin_daily_audit_trigger_api_enqueues_events_for_target_projects(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    service = get_config_service()
    service.set_values(
        {
            "GITLAB_TARGET_PROJECTS": ["team/service", "team/webapp"],
        },
        actor="test-suite",
    )
    for project_id in ("team/service", "team/webapp"):
        service.set_project_agent_config(
            project_id,
            {"DAILY_AUDIT_ENABLED": "1"},
            actor="test-suite",
        )

    calls: dict[str, object] = {"projects": [], "events": []}

    async def fake_enqueue(event, *, store=None, queue=None):
        del store, queue
        calls["events"].append(event)
        return event.actor_key

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_default_branch(project_id: str) -> str:
        calls["projects"].append(project_id)
        return "master" if project_id == "team/service" else "main"

    monkeypatch.setattr(admin_router, "enqueue_gitlab_event", fake_enqueue)
    monkeypatch.setattr(admin_router.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(admin_router, "get_project_default_branch", fake_default_branch)

    response = client.post("/admin/api/daily-audit/trigger")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["scheduled_count"] == 2
    assert calls["projects"] == ["team/service", "team/webapp"]
    assert [event.project_id for event in calls["events"]] == ["team/service", "team/webapp"]
    assert [event.event_type for event in calls["events"]] == ["daily_audit", "daily_audit"]
    assert [event.source_branch for event in calls["events"]] == ["master", "main"]


def test_admin_llm_models_api_uses_current_form_values_for_openai(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    captured = {}

    class _FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="gpt-4.1"),
                    SimpleNamespace(id="gpt-4o-mini"),
                ]
            )

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.models = _FakeModels()

    monkeypatch.setattr(admin_router, "OpenAI", _FakeClient)

    response = client.post(
        "/admin/api/llm/models",
        json={
            "provider": "openai",
            "base_url": "https://openai.local/v1",
            "api_key": "secret-key",
        },
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["gpt-4.1", "gpt-4o-mini"]
    assert captured == {"base_url": "https://openai.local/v1", "api_key": "secret-key"}


def test_admin_llm_test_api_uses_current_form_values_and_returns_response_text(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    captured = {}

    class _FakeChatModel:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content=[{"type": "text", "text": "模型测试成功"}])

    def _fake_make_model_from_snapshot(snapshot, *, temperature, max_tokens, model_id=None):
        captured["snapshot"] = snapshot
        captured["temperature"] = temperature
        captured["max_tokens"] = max_tokens
        captured["model_id"] = model_id
        return _FakeChatModel()

    monkeypatch.setattr(admin_router, "make_model_from_snapshot", _fake_make_model_from_snapshot)

    response = client.post(
        "/admin/api/llm/test",
        json={
            "provider": "openai",
            "base_url": "https://openai.local/v1",
            "api_key": "secret-key",
            "model": "gpt-4.1-mini",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openai"
    assert payload["model_id"] == "openai:gpt-4.1-mini"
    assert payload["response_text"] == "模型测试成功"
    assert captured["snapshot"]["LLM_ACTIVE_PROVIDER"] == "openai"
    assert captured["snapshot"]["OPENAI_BASE_URL"] == "https://openai.local/v1"
    assert captured["snapshot"]["OPENAI_API_KEY"] == "secret-key"
    assert captured["snapshot"]["OPENAI_MODEL"] == "gpt-4.1-mini"
    assert captured["temperature"] == 0
    assert captured["max_tokens"] == 400
    assert captured["model_id"] is None
    assert captured["messages"][0].content == "你好"


def test_admin_llm_test_api_requires_api_key(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    response = client.post(
        "/admin/api/llm/test",
        json={
            "provider": "openai",
            "base_url": "https://openai.local/v1",
            "api_key": "",
            "model": "gpt-4.1-mini",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "当前没有可用 API Key，请先填写或先保存 API Key。"


def test_admin_llm_test_api_falls_back_to_saved_openai_key_and_default_base_url(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "OPENAI_API_KEY": "saved-openai-key",
            "OPENAI_BASE_URL": "https://ignored.example/v1",
            "OPENAI_MODEL": "gpt-4.1-mini",
        },
        actor="test-suite",
    )

    captured = {}

    class _FakeChatModel:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content="ok")

    def _fake_make_model_from_snapshot(snapshot, *, temperature, max_tokens, model_id=None):
        captured["snapshot"] = snapshot
        return _FakeChatModel()

    monkeypatch.setattr(admin_router, "make_model_from_snapshot", _fake_make_model_from_snapshot)

    response = client.post(
        "/admin/api/llm/test",
        json={
            "provider": "openai",
            "base_url": "",
            "api_key": "",
            "model": "gpt-4.1-mini",
        },
    )

    assert response.status_code == 200
    assert response.json()["model_id"] == "openai:gpt-4.1-mini"
    assert captured["snapshot"]["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert captured["snapshot"]["OPENAI_API_KEY"] == "saved-openai-key"
    assert captured["messages"][0].content == "你好"


def test_admin_llm_test_api_falls_back_to_saved_anthropic_key_and_default_base_url(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "ANTHROPIC_API_KEY": "saved-anthropic-key",
            "ANTHROPIC_BASE_URL": "https://ignored.example",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        },
        actor="test-suite",
    )

    captured = {}

    class _FakeChatModel:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content="ok")

    def _fake_make_model_from_snapshot(snapshot, *, temperature, max_tokens, model_id=None):
        captured["snapshot"] = snapshot
        return _FakeChatModel()

    monkeypatch.setattr(admin_router, "make_model_from_snapshot", _fake_make_model_from_snapshot)

    response = client.post(
        "/admin/api/llm/test",
        json={
            "provider": "anthropic",
            "base_url": "",
            "api_key": "",
            "model": "claude-sonnet-4-6",
        },
    )

    assert response.status_code == 200
    assert response.json()["model_id"] == "anthropic:claude-sonnet-4-6"
    assert captured["snapshot"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert captured["snapshot"]["ANTHROPIC_API_KEY"] == "saved-anthropic-key"
    assert captured["messages"][0].content == "你好"


def test_admin_llm_models_api_falls_back_to_saved_openai_key_and_default_base_url(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "OPENAI_API_KEY": "saved-openai-key",
            "OPENAI_BASE_URL": "https://ignored.example/v1",
        },
        actor="test-suite",
    )

    captured = {}

    class _FakeModels:
        def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id="gpt-4.1-mini")])

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.models = _FakeModels()

    monkeypatch.setattr(admin_router, "OpenAI", _FakeClient)

    response = client.post(
        "/admin/api/llm/models",
        json={
            "provider": "openai",
            "base_url": "",
            "api_key": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["gpt-4.1-mini"]
    assert captured == {"base_url": "https://api.openai.com/v1", "api_key": "saved-openai-key"}


def test_admin_llm_models_api_uses_current_form_values_for_anthropic(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    captured = {}

    class _FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="claude-sonnet-4-6"),
                    SimpleNamespace(id="claude-3-7-sonnet-latest"),
                ]
            )

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.models = _FakeModels()

    monkeypatch.setattr(admin_router, "Anthropic", _FakeClient)

    response = client.post(
        "/admin/api/llm/models",
        json={
            "provider": "anthropic",
            "base_url": "https://anthropic.local",
            "api_key": "secret-key",
        },
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["claude-sonnet-4-6", "claude-3-7-sonnet-latest"]
    assert captured == {"base_url": "https://anthropic.local", "api_key": "secret-key"}


def test_admin_llm_models_api_falls_back_to_saved_anthropic_key_and_default_base_url(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    from agent.controlplane import get_config_service

    get_config_service().set_values(
        {
            "ANTHROPIC_API_KEY": "saved-anthropic-key",
            "ANTHROPIC_BASE_URL": "https://ignored.example",
        },
        actor="test-suite",
    )

    captured = {}

    class _FakeModels:
        def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id="claude-sonnet-4-6")])

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.models = _FakeModels()

    monkeypatch.setattr(admin_router, "Anthropic", _FakeClient)

    response = client.post(
        "/admin/api/llm/models",
        json={
            "provider": "anthropic",
            "base_url": "",
            "api_key": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["claude-sonnet-4-6"]
    assert captured == {"base_url": "https://api.anthropic.com", "api_key": "saved-anthropic-key"}


def test_admin_llm_models_api_returns_clear_degraded_hint_for_unsupported_anthropic_discovery(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=True)

    class _FakeModels:
        def list(self):
            raise RuntimeError(
                "<html><head><title>404 Not Found</title></head><body><center><h1>404 Not Found</h1></center></body></html>"
            )

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            self.models = _FakeModels()

    monkeypatch.setattr(admin_router, "Anthropic", _FakeClient)

    response = client.post(
        "/admin/api/llm/models",
        json={
            "provider": "anthropic",
            "base_url": "https://api.minimax.io/anthropic",
            "api_key": "secret-key",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"] == []
    assert payload["error"] == "当前 Anthropic 兼容端点不支持自动获取模型列表，请手动填写模型名；这不影响实际模型调用。"
