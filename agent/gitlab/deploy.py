"""GitLab deployment verification and webhook sync helpers."""

from __future__ import annotations

from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from agent.config import settings
from agent.gitlab.client import get_gitlab_client
from agent.utils.gitlab_project_targets import normalize_gitlab_project_targets


def _snapshot(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(snapshot or settings.current_snapshot().model_dump())


def _clean_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _webhook_url(snapshot: dict[str, Any]) -> str:
    base = _clean_url(snapshot.get("OPEN_REVIEW_EXTERNAL_URL", ""))
    return f"{base}/webhooks/gitlab" if base else ""


def _healthcheck_url(snapshot: dict[str, Any]) -> str:
    base = _clean_url(snapshot.get("OPEN_REVIEW_EXTERNAL_URL", ""))
    return f"{base}/healthz" if base else ""


def _probe_health_url(url: str) -> tuple[bool, str]:
    request = Request(url, headers={"Accept": "application/json"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return True, f"{url} returned {status}."
            return False, f"{url} returned {status}."
    except URLError as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        return False, f"{url} unreachable: {reason}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{url} unreachable: {exc}"


def _ok_check(key: str, message: str) -> dict[str, str]:
    return {"key": key, "status": "ok", "message": message}


def _warning_check(key: str, message: str) -> dict[str, str]:
    return {"key": key, "status": "warning", "message": message}


def _error_check(key: str, message: str) -> dict[str, str]:
    return {"key": key, "status": "error", "message": message}


def _normalize_target_projects(raw: Any, *, api_url: str, external_url: str) -> list[str]:
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, list):
        items = [str(item).strip() for item in raw]
    else:
        items = [item.strip() for item in str(raw).splitlines()]
    filtered = [item for item in items if item]
    if not filtered:
        return []
    return normalize_gitlab_project_targets(filtered, api_url=api_url, external_url=external_url)


def verify_gitlab_configuration(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _snapshot(snapshot)
    api_url = _clean_url(current.get("GITLAB_API_URL", ""))
    external_url = _clean_url(current.get("GITLAB_EXTERNAL_URL", ""))
    token = str(current.get("GITLAB_TOKEN", "")).strip()
    webhook_secret = str(current.get("GITLAB_WEBHOOK_SECRET", "")).strip()
    target_projects_error = ""
    try:
        target_projects = _normalize_target_projects(
            current.get("GITLAB_TARGET_PROJECTS", []),
            api_url=api_url,
            external_url=external_url,
        )
    except ValueError as exc:
        target_projects = []
        target_projects_error = str(exc)
    open_review_external_url = _clean_url(current.get("OPEN_REVIEW_EXTERNAL_URL", ""))
    webhook_url = _webhook_url(current)

    checks = [
        _ok_check("api_url", f"GitLab API 地址：{api_url}") if api_url else _error_check("api_url", "请先填写 GitLab API 地址。"),
        _ok_check("external_url", f"GitLab 外部地址：{external_url}")
        if external_url
        else _warning_check("external_url", "未填写 GitLab 外部地址；浏览器上的 GitLab 链接可能不正确。"),
        _ok_check("token", "GitLab Token 已配置。") if token else _error_check("token", "请先填写 GitLab Token。"),
        _ok_check("webhook_secret", "Webhook 密钥已配置。")
        if webhook_secret
        else _error_check("webhook_secret", "请先填写 GitLab Webhook 密钥。"),
        _error_check("target_projects", target_projects_error)
        if target_projects_error
        else (
            _ok_check("target_projects", f"已配置 {len(target_projects)} 个 GitLab Project。")
            if target_projects
            else _error_check("target_projects", "请先填写至少一个 GitLab Project。")
        ),
        _ok_check("open_review_external_url", f"Open Review 外部地址：{open_review_external_url}")
        if open_review_external_url
        else _error_check("open_review_external_url", "请先填写 Open Review 外部地址。"),
    ]

    if any(item["status"] == "error" for item in checks):
        return {
            "status": "invalid",
            "api_url": api_url,
            "external_url": external_url,
            "target_projects": target_projects,
            "webhook_url": webhook_url,
            "results": [],
            "checks": checks,
        }

    client = get_gitlab_client()
    status = "ready"

    try:
        payload = client.http_get("/user")
        username = str(payload.get("username") or "").strip() or "unknown"
        checks.append(_ok_check("api", "GitLab API 可达。"))
        checks.append(_ok_check("bot_identity", f"当前 Token 对应用户：{username}。"))
    except Exception as exc:
        checks.append(_error_check("api", f"GitLab API 不可达：{exc}"))
        return {
            "status": "invalid",
            "api_url": api_url,
            "external_url": external_url,
            "target_projects": target_projects,
            "webhook_url": webhook_url,
            "results": [],
            "checks": checks,
        }

    results: list[dict[str, Any]] = []
    accessible_count = 0
    for project_ref in target_projects:
        try:
            project = client.projects.get(project_ref)
            if getattr(project, "archived", False):
                raise RuntimeError("Project is archived")
            project_path = str(getattr(project, "path_with_namespace", getattr(project, "path", project_ref)))
            project_id = getattr(project, "id", None)
            results.append(
                {
                    "project_ref": project_ref,
                    "project_id": project_id,
                    "project_path": project_path,
                    "status": "ok",
                    "detail": "Project 可访问。",
                }
            )
            accessible_count += 1
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            results.append(
                {
                    "project_ref": project_ref,
                    "project_id": None,
                    "project_path": project_ref,
                    "status": "error",
                    "detail": detail,
                }
            )

    if accessible_count == len(target_projects):
        checks.append(_ok_check("target_access", f"{accessible_count} / {len(target_projects)} 个 GitLab Project 可访问。"))
    elif accessible_count > 0:
        checks.append(_warning_check("target_access", f"{accessible_count} / {len(target_projects)} 个 GitLab Project 可访问。"))
    else:
        checks.append(_error_check("target_access", "没有可访问的 GitLab Project。"))

    healthy, message = _probe_health_url(_healthcheck_url(current))
    if healthy:
        checks.append(_ok_check("webhook_health", message))
    else:
        checks.append(_error_check("webhook_health", message))
        status = "invalid"

    if external_url and external_url != api_url:
        checks.append(_ok_check("url_topology", "GitLab API 地址与外部地址已分离，适合内外网不同的部署。"))
    elif external_url:
        checks.append(_warning_check("url_topology", "GitLab API 地址与外部地址相同；如果 GitLab 对外地址不同，请分别配置。"))

    if accessible_count == 0:
        status = "invalid"
    elif accessible_count < len(target_projects) and status != "invalid":
        status = "partial"

    return {
        "status": status,
        "api_url": api_url,
        "external_url": external_url,
        "target_projects": target_projects,
        "webhook_url": webhook_url,
        "results": results,
        "checks": checks,
    }


def _list_target_projects(client, *, target_projects: list[str]) -> list[Any]:
    projects = []
    for target_project in target_projects:
        project = client.projects.get(target_project)
        if getattr(project, "archived", False):
            continue
        projects.append(project)
    projects.sort(key=lambda item: str(getattr(item, "path_with_namespace", getattr(item, "path", ""))))
    return projects


def _hook_id(hook) -> int | None:
    hook_id = getattr(hook, "id", None)
    if hook_id is not None:
        return int(hook_id)
    getter = getattr(hook, "get_id", None)
    if callable(getter):
        return int(getter())
    return None


def _hook_url(hook) -> str:
    attributes = getattr(hook, "attributes", None) or getattr(hook, "_attrs", None) or {}
    return str(getattr(hook, "url", None) or attributes.get("url") or "")


def _sync_project_webhook(project, *, webhook_url: str, webhook_secret: str) -> dict[str, str]:
    desired = {
        "url": webhook_url,
        "token": webhook_secret,
        "merge_requests_events": True,
        "note_events": True,
        "issues_events": True,
        "emoji_events": True,
        "enable_ssl_verification": True,
    }
    existing = None
    for hook in project.hooks.list(get_all=True):
        if _hook_url(hook) == webhook_url:
            existing = hook
            break
    if existing is None:
        project.hooks.create(desired)
        return {"status": "created", "detail": "Webhook 已创建。"}
    hook_id = _hook_id(existing)
    if hook_id is None:
        raise RuntimeError("Existing hook is missing an id.")
    project.hooks.update(hook_id, desired)
    return {"status": "updated", "detail": "Webhook 已更新。"}


def _is_forbidden_error(exc: Exception) -> bool:
    response_code = getattr(exc, "response_code", None)
    if response_code == 403:
        return True
    text = str(exc).lower()
    return "403" in text or "forbidden" in text


def _manual_hook_instructions(*, webhook_url: str, webhook_secret: str, project_paths: list[str]) -> str:
    projects = ", ".join(project_paths)
    return (
        "以下项目无法自动配置 webhook。请在 GitLab 项目设置 -> Webhooks 中手工创建 Project Hook："
        f"URL={webhook_url}；Secret Token={webhook_secret}；开启 Merge request events 和 Comments events。"
        f" 失败项目：{projects}。"
    )


def sync_gitlab_webhooks(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    verification = verify_gitlab_configuration(snapshot=snapshot)
    if verification["status"] == "invalid":
        return {
            "status": "invalid",
            "webhook_url": verification.get("webhook_url", ""),
            "target_projects": verification.get("target_projects", []),
            "results": [],
            "manual_instructions": "",
            "checks": verification.get("checks", []),
        }

    current = _snapshot(snapshot)
    webhook_url = _webhook_url(current)
    webhook_secret = str(current.get("GITLAB_WEBHOOK_SECRET", "")).strip()
    target_projects = list(verification.get("target_projects") or [])
    client = get_gitlab_client()
    verification_results = {
        str(item.get("project_ref") or item.get("project_path") or ""): item
        for item in verification.get("results", [])
    }
    project_objects = {
        str(getattr(project, "path_with_namespace", getattr(project, "path", getattr(project, "id", "")))): project
        for project in _list_target_projects(
            client,
            target_projects=[
                str(item.get("project_ref") or item.get("project_path") or "")
                for item in verification.get("results", [])
                if item.get("status") == "ok"
            ],
        )
    }

    results: list[dict[str, Any]] = []
    manual_projects: list[str] = []
    for target_project in target_projects:
        item = verification_results.get(target_project)
        if item is None:
            continue
        if item.get("status") == "error":
            results.append(
                {
                    "project_id": item.get("project_id"),
                    "project_path": item.get("project_path"),
                    "status": "error",
                    "detail": item.get("detail"),
                }
            )
            continue
        project = project_objects.get(str(item.get("project_path") or ""))
        if project is None:
            continue
        project_path = str(getattr(project, "path_with_namespace", getattr(project, "path", getattr(project, "id", ""))))
        project_id = getattr(project, "id", None)
        try:
            outcome = _sync_project_webhook(project, webhook_url=webhook_url, webhook_secret=webhook_secret)
            results.append(
                {
                    "project_id": project_id,
                    "project_path": project_path,
                    "status": outcome["status"],
                    "detail": outcome["detail"],
                }
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            results.append(
                {
                    "project_id": project_id,
                    "project_path": project_path,
                    "status": "error",
                    "detail": detail,
                }
            )
            if _is_forbidden_error(exc):
                manual_projects.append(project_path)

    status = "ok" if results and all(item["status"] != "error" for item in results) else "partial"
    if not results:
        status = "invalid"

    manual_instructions = ""
    if manual_projects:
        manual_instructions = _manual_hook_instructions(
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            project_paths=manual_projects,
        )

    return {
        "status": status,
        "webhook_url": webhook_url,
        "target_projects": target_projects,
        "results": results,
        "manual_instructions": manual_instructions,
    }
