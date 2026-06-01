"""Routes for the built-in admin console."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import re
from datetime import timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlparse

from anthropic import Anthropic
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from openai import OpenAI

from agent.config import settings
from agent.controlplane import get_config_service
from agent.gitlab.deploy import sync_gitlab_webhooks, verify_gitlab_configuration
from agent.gitlab.identity import resolve_bot_identity
from agent.gitlab.project_ops import get_project_default_branch
from agent.observability import build_phoenix_session_url, build_phoenix_trace_url
from agent.runtime.models import EventEnvelope, RunJournalEvent
from agent.runtime.queue import enqueue_gitlab_event, get_runtime_store
from agent.utils.gitlab_project_targets import (
    build_gitlab_project_clone_url,
    infer_gitlab_external_url,
    parse_gitlab_project_target,
)
from agent.utils.model import (
    _DEFAULT_ANTHROPIC_BASE_URL,
    _DEFAULT_OPENAI_BASE_URL,
    extract_model_response_text,
    make_model_from_snapshot,
    resolve_llm_config,
)
from agent.utils.timezone import format_beijing_display, now_in_open_review_tz, parse_iso_datetime

COOKIE_NAME = "open_review_admin_session"
LANG_COOKIE_NAME = "open_review_admin_lang"
SUPPORTED_ADMIN_LANGS = {"zh", "en"}

router = APIRouter(tags=["admin"])
static_mount = StaticFiles(directory=str(Path(__file__).with_name("static")))
_ADMIN_STATIC_DIR = Path(__file__).with_name("static")

GROUP_LABELS = {
    "GitLab": "GitLab",
    "LLM": "模型服务",
    "Sandbox": "沙箱",
    "Runtime": "运行时",
    "Agent": "Agent",
    "Daily Audit": "日常审计",
    "Review": "审查",
    "Filtering": "过滤",
    "Phoenix": "Phoenix",
}

PROVIDER_LABELS = {
    "openai": "OpenAI 兼容接口",
    "anthropic": "Anthropic 兼容接口",
}

PROVIDER_DEFAULT_BASE_URLS = {
    "openai": _DEFAULT_OPENAI_BASE_URL,
    "anthropic": _DEFAULT_ANTHROPIC_BASE_URL,
}

STATE_LABELS = {
    "queued": "排队",
    "running": "执行中",
    "publishing": "发布中",
    "succeeded": "成功",
    "skipped": "已跳过",
    "failed": "失败",
    "stale": "已过期",
    "terminated": "已终止",
    "unknown": "未知",
}

EVENT_TYPE_LABELS = {
    "auto_review": "自动审查",
    "mention": "提及处理",
    "daily_audit": "日常审计",
    "agent_self_evolution": "Agent 自我演进",
    "daily_audit_evolution": "日常审计演进",
    "daily_audit_direction_persistence": "日常审计方向归档",
    "daily_audit_short_term_persistence": "日常审计短期记忆归档",
    "daily_audit_long_term_persistence": "日常审计长期记忆归档",
    "daily_audit_skill_persistence": "日常审计技能沉淀",
}

REASON_LABELS = {
    "published": "已发布结果",
    "reported": "已生成报告",
    "lane execution": "正在执行审查任务",
    "stale_webhook_head_sha": "MR 已更新，本次运行已过期",
    "head changed": "MR Head 已变化，本次结果已过期",
    "gitlab_bot_identity_unavailable": "当前无法解析 GitLab Bot 身份",
    "gitlab_bot_identity_mismatch": "GitLab Bot 身份配置不一致",
    "user_terminated": "管理员已终止本次运行",
    "parent_run_terminated": "父运行已终止，本次后台任务已取消",
}

_TOP_LEVEL_CONTROLLABLE_EVENT_TYPES = frozenset({"auto_review", "mention", "daily_audit"})
_BACKGROUND_ACTOR_SUFFIXES = (
    "!daily_audit_direction_persistence",
    "!daily_audit_short_term_persistence",
    "!daily_audit_long_term_persistence",
    "!daily_audit_skill_persistence",
    "!daily_audit_evolution",
)
_ACTOR_DETAIL_JOURNAL_LIMIT = 100
_LLM_CONNECTIVITY_TEST_PROMPT = "你好"

_ADMIN_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("open_review_admin_lang", default="zh")

ADMIN_I18N_EN = {
    "内部控制台": "Control Console",
    "Open Review 管理后台": "Open Review Admin",
    "监控运行态、查看执行记录，并在线管理服务配置。": "Monitor runtime state, inspect run history, and manage service configuration.",
    "运行状态": "Runtime Status",
    "运行后端": "Runtime Backend",
    "Phoenix 已启用": "Phoenix Enabled",
    "Phoenix 未启用": "Phoenix Disabled",
    "刷新": "Refresh",
    "手动": "Manual",
    "退出登录": "Log Out",
    "总览": "Dashboard",
    "运行记录": "Runs",
    "设置": "Settings",
    "安全": "Security",
    "登录": "Login",
    "管理员登录": "Admin Login",
    "登录后可查看 Actor 状态、运行记录和运行时配置。": "Sign in to view Actor status, run history, and runtime configuration.",
    "密码": "Password",
    "首次初始化": "First-Time Setup",
    "初始化管理后台": "Initialize Admin",
    "首次启动时先设置管理员密码。完成后，GitLab、模型和 Agent 配置都在管理页里维护并持久化到数据库。": "Set the initial admin password first. After setup, GitLab, model, and Agent configuration are managed in the admin pages and persisted to the database.",
    "管理员密码": "Admin Password",
    "管理员密码不能为空。": "Admin password cannot be empty.",
    "管理员账号已初始化。": "Admin account is already initialized.",
    "完成初始化": "Finish Setup",
    "系统总览": "System Overview",
    "查看当前队列压力、Actor 活跃度、异常运行和最近执行记录。": "View queue pressure, Actor activity, abnormal runs, and recent execution history.",
    "个已知 Actor": "known Actors",
    "条最近运行": "recent runs",
    "活跃 Actor": "Active Actors",
    "排队事件": "Queued Events",
    "失败运行": "Failed Runs",
    "过期运行": "Stale Runs",
    "已跟踪 Actor": "Tracked Actors",
    "队列": "Queue",
    "查看全部 Actor": "View All Actors",
    "异常": "Alerts",
    "异常运行": "Abnormal Runs",
    "历史": "History",
    "最近活动": "Recent Activity",
    "查看全部运行记录": "View All Runs",
    "Actor 列表": "Actor List",
    "按 MR 查看 Actor 当前状态、队列深度、租约持有者和最近运行结果。": "Inspect each Actor's state, queue depth, lease holder, and latest run result by merge request.",
    "个可见 Actor": "visible Actors",
    "个正在执行": "running",
    "搜索 Actor": "Search Actor",
    "筛选": "Filter",
    "执行日志": "Execution Log",
    "按状态、事件类型和关键字筛选 durable run 历史，并查看链路引用。": "Filter durable run history by state, event type, and keyword, and inspect trace links.",
    "条可见运行": "visible runs",
    "条失败": "failed",
    "全部状态": "All States",
    "全部事件": "All Events",
    "状态": "State",
    "事件": "Event",
    "搜索": "Search",
    "运行 ID、Actor、原因": "Run ID, Actor, or reason",
    "应用": "Apply",
    "运行": "Run",
    "原因": "Reason",
    "开始时间": "Started",
    "链路": "Trace",
    "工作流": "Workflow",
    "说明": "Detail",
    "时间": "Time",
    "当前视图下没有符合条件的 Actor。": "No Actors match the current view.",
    "当前视图下没有符合条件的运行记录。": "No runs match the current view.",
    "当前没有失败或过期的运行。": "No failed or stale runs.",
    "运行 ID": "Run ID",
    "事件类型": "Event Type",
    "结束时间": "Completed",
    "审查模式": "Review Mode",
    "压缩审查": "Compressed Review",
    "已确认问题": "Confirmed Findings",
    "可疑问题": "Suspicious Findings",
    "开放问题": "Open Questions",
    "意图": "Intent",
    "处理结果": "Result",
    "降级原因": "Degraded Reason",
    "改动文件数": "Changed Files",
    "提交 SHA": "Commit SHA",
    "覆盖的 note": "Covered Notes",
    "Actor 详情": "Actor Detail",
    "查看这个 Actor 的当前运行状态、租约持有者和执行历史。": "View this Actor's current run state, lease holder, and execution history.",
    "最近状态：": "Latest state: ",
    "最近事件：": "Latest event: ",
    "删除": "Remove",
    "状态：": "Status: ",
    "排队": "Queued",
    "执行中": "Running",
    "已调度": "Scheduled",
    "租约持有者": "Lease Holder",
    "租约 TTL": "Lease TTL",
    "当前运行": "Current Run",
    "当前没有正在运行的任务。": "No task is currently running.",
    "待处理队列": "Pending Queue",
    "当前没有排队任务。": "No queued tasks.",
    "触发时间": "Triggered",
    "标题": "Title",
    "来源": "Source",
    "操作": "Action",
    "取消排队任务": "Cancel Queued Task",
    "不可取消": "Not Cancelable",
    "终止请求已发送，等待运行到取消检查点": "Termination requested; waiting for the next cancellation checkpoint.",
    "终止当前运行": "Terminate Current Run",
    "当前运行不可终止": "Current run cannot be terminated",
    "终止请求": "Termination Request",
    "已发送": "Sent",
    "执行时间线": "Execution Timeline",
    "阶段": "Stage",
    "版本": "Version",
    "摘要": "Summary",
    "当前没有执行时间线。": "No execution timeline.",
    "没有记录原因": "No recorded reason",
    "Open Review 配置": "Open Review Configuration",
    "按分组维护集成、运行时、审查与 tracing 配置。": "Manage integration, runtime, review, and tracing configuration by group.",
    "设置分组": "Settings Groups",
    "保存设置": "Save Settings",
    "运行配置": "Runtime Configuration",
    "自我演进": "Self-Evolution",
    "当前目标项目：": "Current target projects: ",
    "当前没有额外运行配置。": "No additional runtime configuration.",
    "立即触发": "Trigger Now",
    "立即触发日常审计": "Trigger Daily Audit Now",
    "尚未触发日常审计。": "Daily Audit has not been triggered.",
    "尚未触发自我演进。": "Self-evolution has not been triggered.",
    "模型服务": "Model Service",
    "当前支持 OpenAI 兼容接口和 Anthropic 兼容接口。旧版 ": "OpenAI-compatible and Anthropic-compatible APIs are supported. Legacy ",
    " 仍会自动同步，用于兼容现有运行时。": " is still synchronized automatically for runtime compatibility.",
    "OpenAI 兼容接口": "OpenAI-Compatible API",
    "Anthropic 兼容接口": "Anthropic-Compatible API",
    "设为当前 Provider": "Set Active Provider",
    "当前运行时将优先使用这个 Provider 的模型、地址和密钥。": "Runtime will prefer this provider's model, base URL, and key.",
    "可填官方地址，也可填兼容网关地址；留空时默认使用官方地址。": "Use the official endpoint or a compatible gateway. Leave empty to use the official default.",
    "敏感值不会明文回显；留空会继续使用已保存密钥，输入新值才会覆盖。": "Sensitive values are not shown. Leave empty to keep the saved key; enter a value to replace it.",
    "模型": "Model",
    "模型名称": "Model Name",
    "点击“刷新模型列表”会优先使用当前表单中的地址和密钥；留空项会回退到官方默认地址或已保存密钥。拉取失败时仍可手动输入模型名。": "Refresh model list uses the current form URL and key first. Empty fields fall back to official defaults or saved keys. You can still enter a model manually if discovery fails.",
    "刷新模型列表": "Refresh Models",
    "测试真实请求": "Test Request",
    "未拉取模型列表": "Model list not fetched",
    "GitLab 身份": "GitLab Identity",
    "当前 Token 身份": "Current Token Identity",
    "实时身份": "Live Identity",
    "缓存身份": "Cached Identity",
    "身份不可用": "Identity Unavailable",
    "当前无法解析 GitLab Bot 身份：": "Cannot resolve GitLab Bot identity: ",
    "当前使用缓存身份。最近一次实时解析失败：": "Using cached identity. Latest live resolution failed: ",
    "当前 GitLab Bot 身份来自实时解析。": "Current GitLab Bot identity was resolved live.",
    "当前用户名": "Username",
    "显示名称": "Display Name",
    "用户 ID": "User ID",
    "身份来源": "Identity Source",
    "GitLab 部署": "GitLab Deployment",
    "验证与同步": "Verify and Sync",
    "保存 GitLab 设置后，先验证连接，再同步目标 Projects 的 project webhook。项目列表以仓库链接为主输入；系统会自动推断 GitLab 外部地址，并在未填写覆盖值时让 API 地址跟随同一个地址。当前目标 webhook：": "After saving GitLab settings, verify connectivity, then sync project webhooks for target projects. Repository URLs are the main input; the system infers the external GitLab URL and, unless overridden, uses the same URL for API access. Current target webhook: ",
    "验证 GitLab 连接": "Verify GitLab Connection",
    "配置/同步 Webhook": "Configure/Sync Webhook",
    "尚未验证 GitLab 部署状态。": "GitLab deployment has not been verified.",
    "GitLab 项目": "GitLab Projects",
    "仓库链接": "Repository URL",
    "项目仓库": "Project Repository",
    "支持当前 GitLab 实例的 HTTPS 仓库 URL 或 project path。保存后内部仍统一转换成 canonical project path。": "Supports HTTPS repository URLs or project paths from the current GitLab instance. Values are saved as canonical project paths.",
    "添加项目": "Add Project",
    "自动推断 GitLab 外部地址：": "Inferred GitLab external URL: ",
    "保存后自动生成": "Generated after save",
    "高级设置": "Advanced Settings",
    "GitLab API 地址覆盖": "GitLab API URL Override",
    "默认跟随仓库链接推断出的地址；仅在 worker 访问 GitLab API/clone 需要走不同地址时填写。": "Defaults to the URL inferred from repository links. Set only when the worker must use a different API/clone address.",
    "API 地址覆盖": "API URL Override",
    "留空表示跟随仓库链接推断": "Leave empty to follow repository URL inference",
    "Sandbox 配置保存后需要重启 worker 才会对新的运行生效。": "Sandbox configuration changes require a worker restart before new runs use them.",
    "安全设置": "Security Settings",
    "修改内置管理后台的管理员密码。修改后新密码立即生效。": "Change the built-in admin password. The new password takes effect immediately.",
    "管理员": "Administrator",
    "新密码": "New Password",
    "更新密码": "Update Password",
    "排队": "Queued",
    "发布中": "Publishing",
    "成功": "Succeeded",
    "已跳过": "Skipped",
    "失败": "Failed",
    "已过期": "Stale",
    "已终止": "Terminated",
    "未知": "Unknown",
    "自动审查": "Auto Review",
    "提及处理": "Mention",
    "日常审计": "Daily Audit",
    "Agent 自我演进": "Agent Self-Evolution",
    "日常审计演进": "Daily Audit Evolution",
    "日常审计方向归档": "Daily Audit Direction Persistence",
    "日常审计短期记忆归档": "Daily Audit Short-Term Persistence",
    "日常审计长期记忆归档": "Daily Audit Long-Term Persistence",
    "日常审计技能沉淀": "Daily Audit Skill Persistence",
    "已发布结果": "Published result",
    "已生成报告": "Generated report",
    "正在执行审查任务": "Executing review task",
    "MR 已更新，本次运行已过期": "MR updated; this run is stale",
    "MR Head 已变化，本次结果已过期": "MR head changed; this result is stale",
    "当前无法解析 GitLab Bot 身份": "Cannot resolve GitLab Bot identity",
    "GitLab 未返回当前 token 对应的用户名。": "GitLab did not return the username for the current token.",
    "GitLab Bot 身份配置不一致": "GitLab Bot identity configuration mismatch",
    "管理员已终止本次运行": "Administrator terminated this run",
    "父运行已终止，本次后台任务已取消": "Parent run terminated; this background task was cancelled",
    "已配置，留空表示不修改": "Configured; leave empty to keep unchanged",
    "未配置": "Not configured",
    "显示或隐藏敏感信息": "Show or hide sensitive value",
    "启用": "Enabled",
    "是否启用该 Agent 的自我演进。": "Whether to enable this Agent's self-evolution.",
    "每几天一次": "Interval Days",
    "按固定北京时间周期执行。": "Run on a fixed Beijing-time schedule.",
    "执行时间": "Run Time",
    "固定北京时间，格式 HH:MM。": "Fixed Beijing time, format HH:MM.",
    "是否启用": "Whether to enable",
    "保存设置": "Save Settings",
    "设置已保存，重启 worker 后生效": "Settings saved. Restart the worker to apply.",
    "设置已保存": "Settings saved",
    "密码错误。": "Incorrect password.",
    "密码已更新": "Password updated",
    "初始化已完成": "Setup complete",
    "初始化": "Setup",
    "北京时间": "Beijing Time",
    "会话": "Session",
    "未启用 Tracing": "Tracing Disabled",
    "是": "Yes",
    "否": "No",
    "最近状态": "Latest State",
    "最近事件": "Latest Event",
    "当前还没有运行记录。": "No run history yet.",
    "未配置项目": "No projects configured",
    "等": "and",
    "个项目": "projects",
    "Inline 评论": "Inline Comments",
    "当前支持 OpenAI 兼容接口和 Anthropic 兼容接口。旧版": "OpenAI-compatible and Anthropic-compatible APIs are supported. Legacy",
    "仍会自动同步，用于兼容现有运行时。": "is still synchronized automatically for runtime compatibility.",
    "模型服务": "Model Service",
    "沙箱": "Sandbox",
    "运行时": "Runtime",
    "审查": "Review",
    "过滤": "Filtering",
    "GitLab API 地址": "GitLab API URL",
    "Open Review 访问 GitLab API 与 git remote 时使用的地址。": "URL used by Open Review for GitLab API access and git remotes.",
    "GitLab 外部地址": "GitLab External URL",
    "浏览器访问 GitLab 时使用的外部地址。": "Browser-facing GitLab URL.",
    "专用 bot 账号使用的 API Token。": "API token used by the dedicated bot account.",
    "Webhook 密钥": "Webhook Secret",
    "用于校验 GitLab Webhook 的共享密钥。": "Shared secret used to validate GitLab webhooks.",
    "校验证书": "Verify TLS",
    "是否启用 TLS 证书校验。": "Whether TLS certificate verification is enabled.",
    "需要自动配置 webhook 的项目列表；一行一个 project path 或 project id，例如 group/project。": "Projects that should receive automatic webhook configuration; one project path or project ID per line, for example group/project.",
    "Open Review 外部地址": "Open Review External URL",
    "GitLab 访问 Open Review Webhook 与后台时使用的外部地址。": "Externally reachable URL GitLab uses for Open Review webhooks and admin links.",
    "当前 Provider": "Active Provider",
    "当前生效的模型服务提供方。": "Currently active model provider.",
    "OpenAI 兼容接口的基础地址。": "Base URL for the OpenAI-compatible API.",
    "OpenAI 兼容接口的访问密钥。": "API key for the OpenAI-compatible API.",
    "OpenAI 模型": "OpenAI Model",
    "OpenAI 兼容接口当前使用的模型。": "Model currently used for the OpenAI-compatible API.",
    "Anthropic 兼容接口的基础地址。": "Base URL for the Anthropic-compatible API.",
    "Anthropic 兼容接口的访问密钥。": "API key for the Anthropic-compatible API.",
    "Anthropic 模型": "Anthropic Model",
    "Anthropic 兼容接口当前使用的模型。": "Model currently used for the Anthropic-compatible API.",
    "沙箱类型": "Sandbox Type",
    "执行沙箱后端。": "Execution sandbox backend.",
    "Docker 镜像": "Docker Image",
    "Docker 沙箱镜像名。": "Docker sandbox image name.",
    "工作并发数": "Worker Concurrency",
    "每个 worker 最多并行处理的 actor 数。": "Maximum number of actors each worker processes concurrently.",
    "租约秒数": "Lease Seconds",
    "Actor 租约 TTL。": "Actor lease TTL.",
    "心跳秒数": "Heartbeat Seconds",
    "租约续期心跳间隔。": "Lease renewal heartbeat interval.",
    "@ 提及合批窗口": "@ Mention Batch Window",
    "同一讨论串 mention 的合批窗口。": "Batching window for mentions in the same discussion.",
    "启用 Mention 自我演进": "Enable Mention Self-Evolution",
    "是否启用 Mention agent 的自我演进。": "Whether to enable Mention agent self-evolution.",
    "Mention 演进间隔天数": "Mention Evolution Interval Days",
    "Mention 自我演进按固定日历时间每隔多少天运行一次。": "How many days between fixed-time Mention self-evolution runs.",
    "Mention 演进时间": "Mention Evolution Time",
    "Mention 自我演进在北京时间的固定触发时间，格式 HH:MM。": "Fixed Beijing-time trigger time for Mention self-evolution, format HH:MM.",
    "启用日常审计": "Enable Daily Audit",
    "是否启用每日定时审计 agent。": "Whether to enable the scheduled Daily Audit agent.",
    "开始时间": "Start Time",
    "每日定时审计在本地时区中的开始时间，格式 HH:MM。": "Daily scheduled audit start time in local timezone, format HH:MM.",
    "允许自动修复": "Allow Autofix",
    "高置信且低风险时是否允许自动建分支并提 MR。": "Whether high-confidence, low-risk findings may create branches and MRs automatically.",
    "Issue 标题前缀": "Issue Title Prefix",
    "报告型日常审计结果新建 GitLab issue 时使用的标题前缀。": "Title prefix used when report-only Daily Audit results create GitLab issues.",
    "启用 Daily Audit 自我演进": "Enable Daily Audit Self-Evolution",
    "是否启用 Daily Audit agent 的自我演进。": "Whether to enable Daily Audit agent self-evolution.",
    "Daily Audit 演进间隔天数": "Daily Audit Evolution Interval Days",
    "Daily Audit 自我演进按固定日历时间每隔多少天运行一次。": "How many days between fixed-time Daily Audit self-evolution runs.",
    "Daily Audit 演进时间": "Daily Audit Evolution Time",
    "Daily Audit 自我演进在北京时间的固定触发时间，格式 HH:MM。": "Fixed Beijing-time trigger time for Daily Audit self-evolution, format HH:MM.",
    "启用 Auto Review 自我演进": "Enable Auto Review Self-Evolution",
    "是否启用 Auto Review agent 的自我演进。": "Whether to enable Auto Review agent self-evolution.",
    "Auto Review 演进间隔天数": "Auto Review Evolution Interval Days",
    "Auto Review 自我演进按固定日历时间每隔多少天运行一次。": "How many days between fixed-time Auto Review self-evolution runs.",
    "Auto Review 演进时间": "Auto Review Evolution Time",
    "Auto Review 自我演进在北京时间的固定触发时间，格式 HH:MM。": "Fixed Beijing-time trigger time for Auto Review self-evolution, format HH:MM.",
    "启用 Phoenix Tracing": "Enable Phoenix Tracing",
    "启用可选的本地 Phoenix tracing。": "Enable optional local Phoenix tracing.",
    "Collector 地址": "Collector URL",
    "Phoenix OTLP / collector 地址。": "Phoenix OTLP collector URL.",
    "Phoenix 可选 tracing 使用的 API key。": "API key used for optional Phoenix tracing.",
    "项目名称": "Project Name",
    "Phoenix 中显示的逻辑项目名。": "Logical project name displayed in Phoenix.",
    "Phoenix 页面深链使用的浏览器基础地址。": "Browser base URL used for Phoenix deep links.",
    "只支持当前 GitLab 实例的 HTTPS 仓库 URL。": "Only HTTPS repository URLs from the current GitLab instance are supported.",
    "GitLab 项目不能为空。": "GitLab project cannot be empty.",
    "只支持 GitLab project path 或当前实例的 HTTPS 仓库 URL。": "Only GitLab project paths or HTTPS repository URLs from the current instance are supported.",
    "只支持 GitLab 项目根 URL，不支持 MR、Issue 或 Wiki 链接。": "Only GitLab project root URLs are supported; MR, issue, or wiki links are not supported.",
    "暂不支持 SSH 仓库地址；请使用当前 GitLab 实例的 HTTPS 地址。": "SSH repository URLs are not supported yet; use an HTTPS URL from the current GitLab instance.",
    "首次配置 GitLab 项目时，请填写当前 GitLab 实例的 HTTPS 仓库 URL。": "When configuring GitLab projects for the first time, enter an HTTPS repository URL from the current GitLab instance.",
    "Project 可访问。": "Project is accessible.",
    "Webhook 已创建。": "Webhook created.",
    "Webhook 已更新。": "Webhook updated.",
    "Webhook healthz 可达。": "Webhook healthz is reachable.",
    "请手工为": "Manually create a Project Hook for",
    "创建 Project Hook。": ".",
    "请先填写 GitLab API 地址。": "Enter the GitLab API URL first.",
    "未填写 GitLab 外部地址；浏览器上的 GitLab 链接可能不正确。": "GitLab external URL is not set; browser GitLab links may be incorrect.",
    "GitLab Token 已配置。": "GitLab Token is configured.",
    "请先填写 GitLab Token。": "Enter the GitLab Token first.",
    "Webhook 密钥已配置。": "Webhook secret is configured.",
    "请先填写 GitLab Webhook 密钥。": "Enter the GitLab webhook secret first.",
    "请先填写至少一个 GitLab Project。": "Enter at least one GitLab project first.",
    "请先填写 Open Review 外部地址。": "Enter the Open Review external URL first.",
    "GitLab API 可达。": "GitLab API is reachable.",
    "没有可访问的 GitLab Project。": "No GitLab project is accessible.",
    "GitLab API 地址与外部地址已分离，适合内外网不同的部署。": "GitLab API URL and external URL are separated, suitable for split internal/external deployments.",
    "GitLab API 地址与外部地址相同；如果 GitLab 对外地址不同，请分别配置。": "GitLab API URL and external URL are the same; configure them separately if the browser-facing GitLab URL differs.",
    "该排队任务已不存在。": "The queued task no longer exists.",
    "该任务当前不支持手动取消。": "This task does not currently support manual cancellation.",
    "该排队任务已进入执行或已不存在。": "The queued task has started executing or no longer exists.",
    "该运行已不存在。": "The run no longer exists.",
    "该运行当前不在执行中。": "The run is not currently executing.",
    "该运行当前不支持手动终止。": "This run does not currently support manual termination.",
    "当前没有配置任何 GitLab Projects。": "No GitLab projects are configured.",
    "未知的 agent_type。": "Unknown agent_type.",
    "当前 Anthropic 兼容端点不支持自动获取模型列表，请手动填写模型名；这不影响实际模型调用。": "The current Anthropic-compatible endpoint does not support automatic model discovery. Enter the model manually; actual model calls are unaffected.",
    "拉取模型列表失败：": "Failed to fetch model list: ",
    "不支持的 Provider。": "Unsupported provider.",
    "当前没有可用 API Key，请先填写或先保存 API Key。": "No API key is available. Enter one or save it first.",
    "模型已响应，但没有返回可展示的文本内容。": "The model responded, but returned no displayable text.",
    "模型测试失败：": "Model test failed: ",
    "未登录": "Not authenticated",
}

ADMIN_I18N = {
    "zh": {},
    "en": ADMIN_I18N_EN,
}


def _admin_is_initialized() -> bool:
    try:
        return get_config_service().has_admin_account()
    except Exception:
        return False


def _cookie_secret() -> str:
    return get_config_service().get_admin_session_secret()


def _sign_session(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(_cookie_secret().encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"


def _decode_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, signature = token.rsplit(".", 1)
    expected = hmac.new(_cookie_secret().encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    try:
        expires_at = parse_iso_datetime(str(payload.get("expires_at") or ""))
    except Exception:
        return None
    if expires_at <= now_in_open_review_tz():
        return None
    return payload


async def _parse_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _is_authenticated(request: Request) -> bool:
    _ADMIN_LANG.set(_admin_lang(request))
    if not _admin_is_initialized():
        return False
    return _decode_session(request.cookies.get(COOKIE_NAME)) is not None


def _redirect_to_login(request: Request | None = None) -> RedirectResponse:
    target = "/admin/login" if _admin_is_initialized() else "/admin/setup"
    if request is not None:
        return _redirect_response(request, target, status_code=303)
    return RedirectResponse(target, status_code=303)


def _json_auth_error() -> JSONResponse:
    if not _admin_is_initialized():
        return JSONResponse({"detail": "admin_setup_required"}, status_code=409)
    return JSONResponse({"detail": _t("未登录")}, status_code=401)


def _admin_lang(request: Request) -> str:
    query_lang = request.query_params.get("lang")
    if query_lang in SUPPORTED_ADMIN_LANGS:
        return query_lang
    cookie_lang = request.cookies.get(LANG_COOKIE_NAME)
    if cookie_lang in SUPPORTED_ADMIN_LANGS:
        return cookie_lang
    return "zh"


def _current_admin_lang() -> str:
    lang = _ADMIN_LANG.get()
    return lang if lang in SUPPORTED_ADMIN_LANGS else "zh"


def _t(text: str) -> str:
    return ADMIN_I18N.get(_current_admin_lang(), {}).get(text, text)


def _te(text: str) -> str:
    return escape(_t(text))


def _set_admin_lang(request: Request) -> contextvars.Token[str]:
    return _ADMIN_LANG.set(_admin_lang(request))


def _format_admin_flash(text: str) -> str:
    return _t(text)


def _localize_gitlab_deploy_text(text: object) -> str:
    raw = str(text or "")
    if _current_admin_lang() != "en" or not raw:
        return raw
    exact = _t(raw)
    if exact != raw:
        return exact

    dynamic_patterns: tuple[tuple[str, str], ...] = (
        (r"^GitLab API 地址：(.+)$", "GitLab API URL: {0}"),
        (r"^GitLab 外部地址：(.+)$", "GitLab external URL: {0}"),
        (r"^Open Review 外部地址：(.+)$", "Open Review external URL: {0}"),
        (r"^已配置 (\d+) 个 GitLab Project。$", "Configured {0} GitLab projects."),
        (r"^当前 Token 对应用户：(.+)。$", "Current token user: {0}."),
        (r"^GitLab API 不可达：(.+)$", "GitLab API is unreachable: {0}"),
        (r"^(\d+) / (\d+) 个 GitLab Project 可访问。$", "{0} / {1} GitLab projects are accessible."),
        (r"^请手工为 (.+) 创建 Project Hook。$", "Manually create a Project Hook for {0}."),
    )
    for pattern, replacement in dynamic_patterns:
        match = re.match(pattern, raw)
        if match:
            return replacement.format(*match.groups())

    manual_match = re.match(
        r"^以下项目无法自动配置 webhook。请在 GitLab 项目设置 -> Webhooks 中手工创建 Project Hook："
        r"URL=(.+?)；Secret Token=(.+?)；开启 Merge request events 和 Comments events。 失败项目：(.+)。$",
        raw,
    )
    if manual_match:
        webhook_url, webhook_secret, projects = manual_match.groups()
        return (
            "The following projects could not be configured automatically. "
            "Create a Project Hook manually in GitLab Project Settings -> Webhooks: "
            f"URL={webhook_url}; Secret Token={webhook_secret}; "
            "enable Merge request events and Comments events. "
            f"Failed projects: {projects}."
        )

    return raw


def _localize_gitlab_deploy_payload(payload: dict) -> dict:
    if _current_admin_lang() != "en":
        return payload
    localized = dict(payload)
    if isinstance(localized.get("checks"), list):
        localized["checks"] = [
            {**item, "message": _localize_gitlab_deploy_text(item.get("message"))}
            if isinstance(item, dict)
            else item
            for item in localized["checks"]
        ]
    if isinstance(localized.get("results"), list):
        localized["results"] = [
            {**item, "detail": _localize_gitlab_deploy_text(item.get("detail"))}
            if isinstance(item, dict)
            else item
            for item in localized["results"]
        ]
    if "manual_instructions" in localized:
        localized["manual_instructions"] = _localize_gitlab_deploy_text(localized.get("manual_instructions"))
    if "error" in localized:
        localized["error"] = _localize_gitlab_deploy_text(localized.get("error"))
    return localized


def _set_lang_cookie_if_needed(request: Request, response: HTMLResponse | RedirectResponse) -> None:
    query_lang = request.query_params.get("lang")
    if query_lang in SUPPORTED_ADMIN_LANGS:
        response.set_cookie(
            LANG_COOKIE_NAME,
            query_lang,
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            samesite="lax",
        )


def _html_response(request: Request, html: str, *, status_code: int = 200) -> HTMLResponse:
    response = HTMLResponse(html, status_code=status_code)
    _set_lang_cookie_if_needed(request, response)
    return response


def _render_html_response(request: Request, renderer, *, status_code: int = 200) -> HTMLResponse:
    token = _set_admin_lang(request)
    try:
        html = renderer()
    finally:
        _ADMIN_LANG.reset(token)
    return _html_response(request, html, status_code=status_code)


def _redirect_response(request: Request, target: str, *, status_code: int = 303) -> RedirectResponse:
    response = RedirectResponse(target, status_code=status_code)
    _set_lang_cookie_if_needed(request, response)
    return response


def _count_by_state(runs: list[dict], state: str) -> int:
    return sum(1 for item in runs if item.get("state") == state)


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return format_beijing_display(parse_iso_datetime(value))
    except ValueError:
        return value


def _render_timestamp_cell(value: str) -> str:
    if not value or value == "—":
        return "—"
    suffix = " 北京时间"
    if value.endswith(suffix):
        main = escape(value[: -len(suffix)])
        return (
            '<span class="timestamp-stack">'
            f'<span class="timestamp-main">{main}</span>'
            f'<span class="timestamp-zone">{_te("北京时间")}</span>'
            "</span>"
        )
    return escape(value)


def _phoenix_base_url(snapshot: dict | None = None) -> str | None:
    if snapshot is None:
        snapshot = get_config_service().get_snapshot()
    base_url = str(snapshot.get("PHOENIX_UI_BASE_URL") or snapshot.get("PHOENIX_COLLECTOR_ENDPOINT") or "").rstrip("/")
    return base_url or None


def _build_phoenix_trace_href(trace_id: str | None, *, base_url: str | None) -> str | None:
    if not trace_id or not base_url:
        return None
    return f"{base_url}/redirects/traces/{quote(trace_id, safe='')}"


def _build_phoenix_session_href(session_id: str | None, *, base_url: str | None) -> str | None:
    if not session_id or not base_url:
        return None
    return f"{base_url}/redirects/sessions/{quote(session_id, safe='')}"


def _run_is_prepared(item: dict) -> bool:
    return all(
        key in item
        for key in (
            "started_display",
            "completed_display",
            "trace_href",
            "session_href",
            "state_display",
            "event_type_display",
            "reason_display",
        )
    )


def _prepare_run(item: dict, *, phoenix_base_url: str | None = None) -> dict:
    if _run_is_prepared(item):
        return dict(item)
    run = dict(item)
    run["started_display"] = _format_timestamp(run.get("started_at"))
    run["completed_display"] = _format_timestamp(run.get("completed_at"))
    base_url = phoenix_base_url if phoenix_base_url is not None else _phoenix_base_url()
    run["trace_href"] = run.get("trace_url") or _build_phoenix_trace_href(run.get("trace_id"), base_url=base_url)
    run["session_href"] = _build_phoenix_session_href(run.get("session_id"), base_url=base_url)
    run["state_display"] = _t(STATE_LABELS.get(run.get("state") or "unknown", run.get("state") or "未知"))
    run["event_type_display"] = _t(EVENT_TYPE_LABELS.get(run.get("event_type") or "", run.get("event_type") or "—"))
    raw_reason = str(run.get("reason") or run.get("error") or "—")
    run["reason_display"] = _t(REASON_LABELS.get(raw_reason, raw_reason))
    return run


def _prepare_runs(runs: list[dict], *, phoenix_base_url: str | None = None) -> list[dict]:
    return [_prepare_run(item, phoenix_base_url=phoenix_base_url) for item in runs]


def _normalize_run(item: dict) -> dict:
    return _prepare_run(item)


def _normalize_journal_event(item: dict) -> dict:
    event = dict(item)
    event["created_display"] = _format_timestamp(event.get("created_at"))
    return event


def _event_type_display(event_type: str | None) -> str:
    return _t(EVENT_TYPE_LABELS.get(event_type or "", event_type or "—"))


def _actor_supports_controls(actor_key: str) -> bool:
    return bool(actor_key) and "!self_evolution:" not in actor_key and not actor_key.endswith(_BACKGROUND_ACTOR_SUFFIXES)


def _is_controllable_event_type(event_type: str | None) -> bool:
    return (event_type or "") in _TOP_LEVEL_CONTROLLABLE_EVENT_TYPES


def _pending_event_source_summary(event: EventEnvelope) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "daily_audit":
        return str(payload.get("trigger") or payload.get("kind") or event.event_type)
    if event.event_type == "mention":
        pieces = []
        if event.note_author:
            pieces.append(str(event.note_author))
        if event.note_id is not None:
            pieces.append(f"note #{event.note_id}")
        if event.discussion_id:
            pieces.append(f"discussion {event.discussion_id}")
        return " · ".join(pieces) or event.event_type
    if event.event_type == "auto_review":
        if event.head_sha:
            return event.head_sha
        if event.source_branch:
            return event.source_branch
    return str(payload.get("trigger") or payload.get("kind") or event.event_type)


def _serialize_pending_event(event: EventEnvelope) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_type_display": _event_type_display(event.event_type),
        "received_at": event.received_at,
        "received_display": _format_timestamp(event.received_at),
        "title": event.title or "—",
        "trigger_source": _pending_event_source_summary(event),
        "cancelable": _is_controllable_event_type(event.event_type),
    }


def _normalize_runtime_run(item: dict) -> dict:
    run = dict(item)
    run["started_display"] = _format_timestamp(run.get("started_at"))
    run["completed_display"] = _format_timestamp(run.get("completed_at"))
    run["termination_requested_display"] = _format_timestamp(run.get("termination_requested_at"))
    run["state_display"] = _t(STATE_LABELS.get(run.get("state") or "unknown", run.get("state") or "未知"))
    run["event_type_display"] = _event_type_display(run.get("event_type"))
    run["termination_requested"] = bool(run.get("termination_requested"))
    run["terminatable"] = bool(
        run.get("state") in {"running", "publishing"}
        and _is_controllable_event_type(run.get("event_type"))
        and not run["termination_requested"]
    )
    return run


def _pick_running_run(runtime_runs: list[dict]) -> dict | None:
    for item in runtime_runs:
        normalized = _normalize_runtime_run(item)
        if normalized.get("state") in {"running", "publishing"}:
            return normalized
    return None


async def _load_run_journal(
    store,
    execution_key: str,
    *,
    journal_limit: int | None = None,
) -> list[RunJournalEvent]:
    if journal_limit is None:
        return await store.list_run_journal(execution_key)
    try:
        return await store.list_run_journal(execution_key, limit=journal_limit)
    except TypeError:
        journal = await store.list_run_journal(execution_key)
        if journal_limit > 0:
            return journal[-journal_limit:]
        return journal


async def _attach_run_journal(
    runs: list[dict],
    *,
    phoenix_base_url: str | None = None,
    journal_limit: int | None = None,
) -> list[dict]:
    store = await get_runtime_store()
    enriched: list[dict] = []
    for item in runs:
        run = _prepare_run(item, phoenix_base_url=phoenix_base_url)
        execution_key = run.get("execution_key")
        if execution_key:
            journal = await _load_run_journal(store, str(execution_key), journal_limit=journal_limit)
            run["journal"] = [_normalize_journal_event(entry.model_dump(mode="json")) for entry in journal]
        else:
            run["journal"] = []
        enriched.append(run)
    return enriched


def _filter_runs(
    runs: list[dict],
    *,
    state: str = "",
    event_type: str = "",
    query: str = "",
    phoenix_base_url: str | None = None,
) -> list[dict]:
    filtered = _prepare_runs(runs, phoenix_base_url=phoenix_base_url)
    if state:
        filtered = [item for item in filtered if item.get("state") == state]
    if event_type:
        filtered = [item for item in filtered if item.get("event_type") == event_type]
    if query:
        needle = query.lower().strip()
        filtered = [
            item
            for item in filtered
            if needle in " ".join(
                [
                    str(item.get("run_id", "")),
                    str(item.get("actor_key", "")),
                    str(item.get("project_id", "")),
                    str(item.get("reason", "")),
                    str(item.get("error", "")),
                ]
            ).lower()
        ]
    return filtered


def _build_actor_summaries(
    runtime_statuses: list[dict],
    recent_runs: list[dict],
    *,
    phoenix_base_url: str | None = None,
) -> list[dict]:
    runtime_by_actor = {item["actor_key"]: dict(item) for item in runtime_statuses}
    latest_run_by_actor: dict[str, dict] = {}
    for raw_run in recent_runs:
        run = raw_run if _run_is_prepared(raw_run) else _prepare_run(raw_run, phoenix_base_url=phoenix_base_url)
        latest_run_by_actor.setdefault(run["actor_key"], run)

    actor_keys = set(runtime_by_actor) | set(latest_run_by_actor)
    summaries: list[dict] = []
    for actor_key in actor_keys:
        runtime = runtime_by_actor.get(
            actor_key,
            {
                "actor_key": actor_key,
                "pending_count": 0,
                "inflight_count": 0,
                "lease_owner": None,
                "lease_ttl_seconds": None,
                "scheduled": False,
            },
        )
        last_run = latest_run_by_actor.get(actor_key)
        summaries.append(
            {
                "actor_key": actor_key,
                "pending_count": runtime.get("pending_count", 0),
                "inflight_count": runtime.get("inflight_count", 0),
                "scheduled": bool(runtime.get("scheduled")),
                "lease_owner": runtime.get("lease_owner"),
                "lease_ttl_seconds": runtime.get("lease_ttl_seconds"),
                "last_run_id": last_run.get("run_id") if last_run else None,
                "last_run_state": last_run.get("state") if last_run else None,
                "last_event_type": last_run.get("event_type") if last_run else None,
                "last_event_type_display": last_run.get("event_type_display") if last_run else None,
                "last_reason": last_run.get("reason") if last_run else None,
                "last_started_at": last_run.get("started_at") if last_run else None,
                "last_started_display": last_run.get("started_display") if last_run else "—",
                "trace_href": last_run.get("trace_href") if last_run else None,
                "session_href": last_run.get("session_href") if last_run else None,
            }
        )
    summaries.sort(key=lambda item: item["actor_key"])
    summaries.sort(key=lambda item: item["last_started_at"] or "", reverse=True)
    summaries.sort(key=lambda item: item["scheduled"], reverse=True)
    summaries.sort(key=lambda item: item["pending_count"], reverse=True)
    summaries.sort(key=lambda item: item["inflight_count"], reverse=True)
    return summaries


def _render_state_badge(state: str) -> str:
    raw = (state or "unknown").strip().lower()
    label = _te(STATE_LABELS.get(raw, raw or "未知"))
    return f'<span class="run-state state-{escape(raw)}">{label}</span>'


def _render_trace_meta(item: dict) -> str:
    trace_url = item.get("trace_href") or item.get("trace_url") or build_phoenix_trace_url(item.get("trace_id"))
    session_url = item.get("session_href") or build_phoenix_session_url(item.get("session_id"))
    parts = []
    if trace_url:
        parts.append(
            f'<a class="trace-link" href="{escape(trace_url)}" target="_blank" rel="noreferrer">{_te("链路")}</a>'
        )
    if session_url:
        parts.append(
            f'<a class="trace-link" href="{escape(session_url)}" target="_blank" rel="noreferrer">{_te("会话")}</a>'
        )
    if not parts:
        return f'<span class="trace-missing">{_te("未启用 Tracing")}</span>'
    return " · ".join(parts)


def _ordered_groups(fields: list[dict]) -> list[str]:
    ordered: list[str] = []
    for field in fields:
        group = field["group"]
        if group not in ordered:
            ordered.append(group)
    return ordered


def _group_label(group: str) -> str:
    return _t(GROUP_LABELS.get(group, group))


def _render_status_chip(label: str, value: str, tone: str = "") -> str:
    tone_class = f" tone-{tone}" if tone else ""
    return (
        f'<div class="status-chip{tone_class}"><span class="status-chip-label">{_te(label)}</span>'
        f"<strong>{_te(value)}</strong></div>"
    )


def _render_table_empty(colspan: int, message: str) -> str:
    return f'<tr><td colspan="{colspan}" class="empty-cell">{_te(message)}</td></tr>'


def _render_actor_rows(actors: list[dict]) -> str:
    if not actors:
        return _render_table_empty(8, "当前视图下没有符合条件的 Actor。")
    rows = []
    for item in actors:
        rows.append(
            f"""
            <tr>
              <td class="table-key"><a href="/admin/mrs/{escape(item['actor_key'])}">{escape(item['actor_key'])}</a></td>
              <td>{item['pending_count']}</td>
              <td>{item['inflight_count']}</td>
              <td>{_te('是' if item['scheduled'] else '否')}</td>
              <td>{escape(item.get('lease_owner') or '—')}</td>
              <td>{item['lease_ttl_seconds'] if item.get('lease_ttl_seconds') is not None else '—'}</td>
              <td>{_render_state_badge(item.get('last_run_state') or 'unknown')}</td>
              <td class="table-muted">{escape(item.get('last_event_type_display') or '—')}</td>
            </tr>
            """
        )
    return "".join(rows)


def _render_run_rows(runs: list[dict], *, include_run_id: bool = True) -> str:
    if not runs:
        colspan = 7 if include_run_id else 6
        return _render_table_empty(colspan, "当前视图下没有符合条件的运行记录。")
    rows = []
    for item in runs:
        item = item if _run_is_prepared(item) else _prepare_run(item)
        run_id_cell = f"<td>{escape(item['run_id'])}</td>" if include_run_id else ""
        rows.append(
            f"""
            <tr>
              {run_id_cell}
              <td>{_render_state_badge(item['state'])}</td>
              <td>{escape(item['event_type_display'])}</td>
              <td class="table-key"><a href="/admin/mrs/{escape(item['actor_key'])}">{escape(item['actor_key'])}</a></td>
              <td class="table-reason">{escape(item['reason_display'])}</td>
              <td class="table-muted table-time">{_render_timestamp_cell(item['started_display'])}</td>
              <td class="table-ref">{_render_trace_meta(item)}</td>
            </tr>
            """
        )
    return "".join(rows)


def _render_alert_rows(runs: list[dict]) -> str:
    if not runs:
        return _render_table_empty(5, "当前没有失败或过期的运行。")
    rows = []
    for item in runs:
        item = item if _run_is_prepared(item) else _prepare_run(item)
        rows.append(
            f"""
            <tr>
              <td>{_render_state_badge(item['state'])}</td>
              <td>{escape(item['event_type_display'])}</td>
              <td class="table-key"><a href="/admin/mrs/{escape(item['actor_key'])}">{escape(item['actor_key'])}</a></td>
              <td class="table-reason">{escape(item['reason_display'])}</td>
              <td class="table-muted table-time">{_render_timestamp_cell(item['started_display'])}</td>
            </tr>
            """
        )
    return "".join(rows)


def _render_detail_blocks(item: dict) -> str:
    blocks = [
        ("运行 ID", item.get("run_id") or "—"),
        ("状态", item.get("state") or "—"),
        ("事件类型", item.get("event_type") or "—"),
        ("开始时间", item.get("started_display") or "—"),
        ("结束时间", item.get("completed_display") or "—"),
    ]
    if item.get("event_type") == "auto_review":
        blocks.extend(
            [
                ("审查模式", item.get("review_mode") or "—"),
                ("压缩审查", _t("是" if item.get("compressed_review") else "否")),
                ("已确认问题", str(item.get("confirmed_findings_count", 0))),
                ("可疑问题", str(item.get("suspicious_findings_count", 0))),
                ("开放问题", str(item.get("open_questions_count", 0))),
                ("Inline 评论", str(item.get("inline_comments_count", 0))),
            ]
        )
    if item.get("event_type") == "mention":
        blocks.extend(
            [
                ("意图", item.get("mention_intent") or "—"),
                ("处理结果", item.get("mention_status") or "—"),
                ("降级原因", item.get("mention_degraded_reason") or "—"),
                ("改动文件数", str(item.get("changed_files_count", 0))),
                ("提交 SHA", item.get("commit_sha") or "—"),
                ("覆盖的 note", ", ".join(str(note) for note in item.get("covered_note_ids", [])) or "—"),
            ]
        )
    return "".join(
        f'<div class="detail-item"><span class="detail-key">{_te(key)}</span><span class="detail-value">{escape(value)}</span></div>'
        for key, value in blocks
    )


def _render_layout(
    *,
    title: str,
    nav_active: str,
    page_slug: str,
    body: str,
    flash: str = "",
    auto_refresh_seconds: int | None = None,
    body_attrs: dict[str, str] | None = None,
    snapshot: dict | None = None,
) -> str:
    nav = [
        ("总览", "/admin", "dashboard"),
        ("Actor", "/admin/actors", "actors"),
        ("运行记录", "/admin/runs", "runs"),
        ("设置", "/admin/settings", "settings"),
        ("安全", "/admin/security", "security"),
    ]
    nav_html = "".join(
        f'<a class="nav-link{" active" if slug == nav_active else ""}" href="{href}">{_te(label)}</a>'
        for label, href, slug in nav
    )
    flash_html = f'<div class="flash">{_te(flash)}</div>' if flash else ""
    snapshot = snapshot or get_config_service().get_snapshot()
    header_chips = "".join(
        [
            _render_status_chip("运行后端", "SQLite", "neutral"),
            _render_status_chip(
                "Tracing",
                "Phoenix 已启用" if snapshot.get("PHOENIX_TRACING_ENABLED") else "Phoenix 未启用",
                "good" if snapshot.get("PHOENIX_TRACING_ENABLED") else "neutral",
            ),
            _render_status_chip(
                "刷新",
                f"{auto_refresh_seconds}s" if auto_refresh_seconds else "手动",
                "neutral",
            ),
        ]
    )
    extra_attrs = body_attrs or {}
    lang = _current_admin_lang()
    html_lang = "en" if lang == "en" else "zh-CN"
    attr_pairs = [("class", "console-body"), ("data-page", page_slug), ("data-admin-lang", lang)]
    if auto_refresh_seconds:
        attr_pairs.append(("data-autorefresh", str(auto_refresh_seconds)))
    attr_pairs.extend(extra_attrs.items())
    body_attr_html = " ".join(f'{key}="{escape(value)}"' for key, value in attr_pairs)
    return f"""<!doctype html>
<html lang="{html_lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{_te(title)} · {_te("Open Review 管理后台")}</title>
    <link rel="stylesheet" href="{_admin_static_href('admin.css')}">
    <script defer src="{_admin_static_href('admin.js')}"></script>
  </head>
  <body {body_attr_html}>
    <div class="app-shell">
      <aside class="app-sidebar">
        <div class="sidebar-brand">
          <div class="sidebar-kicker">{_te("内部控制台")}</div>
          <div class="sidebar-title">{_te("Open Review 管理后台")}</div>
          <p class="sidebar-copy">{_te("监控运行态、查看执行记录，并在线管理服务配置。")}</p>
        </div>
        <nav class="sidebar-nav">{nav_html}</nav>
        <section class="sidebar-section">
          <div class="sidebar-section-title">{_te("运行状态")}</div>
          <div class="sidebar-status">{header_chips}</div>
        </section>
      </aside>
      <div class="app-main">
        <header class="topbar reveal">
          <div class="topbar-copy">
            <div class="topbar-kicker">Open Review</div>
            <h1 class="topbar-title">{_te(title)}</h1>
          </div>
          <div class="topbar-actions">
            <form method="get" action="/admin/language" class="language-switch">
              <input type="hidden" name="next" value="/admin" data-lang-next>
              <button class="language-option" type="submit" name="lang" value="zh">中文</button>
              <button class="language-option" type="submit" name="lang" value="en">English</button>
            </form>
            <form method="post" action="/admin/logout">
              <button class="ghost-button" type="submit">{_te("退出登录")}</button>
            </form>
          </div>
        </header>
        {flash_html}
        <main class="shell-main">{body}</main>
      </div>
    </div>
  </body>
</html>"""


def _render_login(error: str = "") -> str:
    error_html = f'<div class="flash error">{_te(error)}</div>' if error else ""
    lang = _current_admin_lang()
    html_lang = "en" if lang == "en" else "zh-CN"
    return f"""<!doctype html>
<html lang="{html_lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{_te("登录")} · {_te("Open Review 管理后台")}</title>
    <link rel="stylesheet" href="{_admin_static_href('admin.css')}">
  </head>
  <body class="login-body" data-admin-lang="{lang}">
    <section class="login-sheet reveal">
      <form method="get" action="/admin/language" class="language-switch login-language-switch">
        <input type="hidden" name="next" value="/admin/login" data-lang-next>
        <button class="language-option" type="submit" name="lang" value="zh">中文</button>
        <button class="language-option" type="submit" name="lang" value="en">English</button>
      </form>
      <div class="login-kicker">{_te("管理员登录")}</div>
      <h1>{_te("Open Review 管理后台")}</h1>
      <p class="lede">{_te("登录后可查看 Actor 状态、运行记录和运行时配置。")}</p>
      {error_html}
      <form method="post" action="/admin/login" class="login-form">
        <label class="field-title">{_te("密码")}</label>
        <input type="password" name="password" autocomplete="current-password" required>
        <button type="submit">{_te("登录")}</button>
      </form>
    </section>
  </body>
</html>"""


def _render_setup(error: str = "") -> str:
    error_html = f'<div class="flash error">{_te(error)}</div>' if error else ""
    lang = _current_admin_lang()
    html_lang = "en" if lang == "en" else "zh-CN"
    return f"""<!doctype html>
<html lang="{html_lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{_te("初始化")} · {_te("Open Review 管理后台")}</title>
    <link rel="stylesheet" href="{_admin_static_href('admin.css')}">
  </head>
  <body class="login-body" data-admin-lang="{lang}">
    <section class="login-sheet reveal">
      <form method="get" action="/admin/language" class="language-switch login-language-switch">
        <input type="hidden" name="next" value="/admin/setup" data-lang-next>
        <button class="language-option" type="submit" name="lang" value="zh">中文</button>
        <button class="language-option" type="submit" name="lang" value="en">English</button>
      </form>
      <div class="login-kicker">{_te("首次初始化")}</div>
      <h1>{_te("初始化管理后台")}</h1>
      <p class="lede">{_te("首次启动时先设置管理员密码。完成后，GitLab、模型和 Agent 配置都在管理页里维护并持久化到数据库。")}</p>
      {error_html}
      <form method="post" action="/admin/setup" class="login-form">
        <label class="field-title">{_te("管理员密码")}</label>
        <input type="password" name="password" autocomplete="new-password" required>
        <button type="submit">{_te("完成初始化")}</button>
      </form>
    </section>
  </body>
</html>"""


def _render_dashboard(
    runtime_statuses: list[dict],
    recent_runs: list[dict],
    flash: str = "",
    *,
    snapshot: dict | None = None,
) -> str:
    snapshot = snapshot or get_config_service().get_snapshot()
    recent_runs = _prepare_runs(recent_runs, phoenix_base_url=_phoenix_base_url(snapshot))
    actor_summaries = _build_actor_summaries(runtime_statuses, recent_runs)
    active_actors = [
        item for item in actor_summaries if item["pending_count"] or item["inflight_count"] or item["scheduled"]
    ][:12]
    alert_runs = [item for item in recent_runs if item["state"] in {"failed", "stale"}][:10]
    metrics = {
        "active_actors": sum(1 for item in actor_summaries if item["inflight_count"]),
        "queued_events": sum(item["pending_count"] for item in actor_summaries),
        "failed_runs": _count_by_state(recent_runs, "failed"),
        "stale_runs": _count_by_state(recent_runs, "stale"),
        "known_actors": len(actor_summaries),
    }
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">{_te("总览")}</div>
        <h2>{_te("系统总览")}</h2>
        <p class="lede">{_te("查看当前队列压力、Actor 活跃度、异常运行和最近执行记录。")}</p>
      </div>
      <div class="page-meta">
        <span>{metrics['known_actors']} {_te("个已知 Actor")}</span>
        <span>{len(recent_runs)} {_te("条最近运行")}</span>
      </div>
    </section>
    <section class="status-strip reveal">
      <article class="status-card"><span>{_te("活跃 Actor")}</span><strong>{metrics['active_actors']}</strong></article>
      <article class="status-card"><span>{_te("排队事件")}</span><strong>{metrics['queued_events']}</strong></article>
      <article class="status-card"><span>{_te("失败运行")}</span><strong>{metrics['failed_runs']}</strong></article>
      <article class="status-card"><span>{_te("过期运行")}</span><strong>{metrics['stale_runs']}</strong></article>
      <article class="status-card"><span>{_te("已跟踪 Actor")}</span><strong>{metrics['known_actors']}</strong></article>
    </section>
    <section class="dashboard-grid">
      <section class="control-panel reveal">
        <div class="panel-head">
          <div>
            <div class="eyebrow">{_te("队列")}</div>
            <h3>{_te("活跃 Actor")}</h3>
          </div>
          <a class="panel-link" href="/admin/actors">{_te("查看全部 Actor")}</a>
        </div>
        <table class="console-table">
          <thead>
            <tr>
              <th>Actor</th>
              <th>{_te("排队")}</th>
              <th>{_te("执行中")}</th>
              <th>{_te("已调度")}</th>
              <th>{_te("租约持有者")}</th>
              <th>TTL</th>
              <th>{_te("最近状态")}</th>
              <th>{_te("最近事件")}</th>
            </tr>
          </thead>
          <tbody>{_render_actor_rows(active_actors)}</tbody>
        </table>
      </section>
      <section class="control-panel reveal">
        <div class="panel-head">
          <div>
            <div class="eyebrow">{_te("异常")}</div>
            <h3>{_te("异常运行")}</h3>
          </div>
        </div>
        <table class="console-table console-table-compact">
          <thead>
            <tr>
              <th>{_te("状态")}</th>
              <th>{_te("工作流")}</th>
              <th>Actor</th>
              <th>{_te("说明")}</th>
              <th class="col-time">{_te("时间")}</th>
            </tr>
          </thead>
          <tbody>{_render_alert_rows(alert_runs)}</tbody>
        </table>
      </section>
    </section>
    <section class="control-panel reveal">
      <div class="panel-head">
        <div>
          <div class="eyebrow">{_te("历史")}</div>
          <h3>{_te("最近活动")}</h3>
        </div>
        <a class="panel-link" href="/admin/runs">{_te("查看全部运行记录")}</a>
      </div>
      <table class="console-table">
        <thead>
          <tr>
            <th>{_te("运行")}</th>
            <th>{_te("状态")}</th>
            <th>{_te("事件")}</th>
            <th>Actor</th>
            <th>{_te("原因")}</th>
            <th class="col-time">{_te("开始时间")}</th>
            <th class="col-trace">{_te("链路")}</th>
          </tr>
        </thead>
        <tbody>{_render_run_rows(recent_runs[:15])}</tbody>
      </table>
    </section>
    """
    return _render_layout(
        title="总览",
        nav_active="dashboard",
        page_slug="dashboard",
        body=body,
        flash=flash,
        auto_refresh_seconds=10,
        snapshot=snapshot,
    )


def _render_actors_page(actors: list[dict], *, query: str = "", snapshot: dict | None = None) -> str:
    query_value = escape(query)
    filtered = actors
    if query:
        needle = query.lower().strip()
        filtered = [item for item in actors if needle in item["actor_key"].lower()]
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">Actor</div>
        <h2>{_te("Actor 列表")}</h2>
        <p class="lede">{_te("按 MR 查看 Actor 当前状态、队列深度、租约持有者和最近运行结果。")}</p>
      </div>
      <div class="page-meta">
        <span>{len(filtered)} {_te("个可见 Actor")}</span>
        <span>{sum(1 for item in filtered if item['inflight_count'])} {_te("个正在执行")}</span>
      </div>
    </section>
    <section class="control-panel reveal">
      <form class="filter-form" method="get" action="/admin/actors">
        <label class="filter-field">
          <span>{_te("搜索 Actor")}</span>
          <input type="text" name="q" value="{query_value}" placeholder="team/project!42">
        </label>
        <div class="filter-actions"><button type="submit">{_te("筛选")}</button></div>
      </form>
    </section>
    <section class="control-panel reveal">
      <table class="console-table">
        <thead>
          <tr>
            <th>Actor</th>
            <th>{_te("排队")}</th>
            <th>{_te("执行中")}</th>
            <th>{_te("已调度")}</th>
            <th>{_te("租约持有者")}</th>
            <th>{_te("租约 TTL")}</th>
            <th>{_te("最近状态")}</th>
            <th>{_te("最近事件")}</th>
          </tr>
        </thead>
        <tbody>{_render_actor_rows(filtered)}</tbody>
      </table>
    </section>
    """
    return _render_layout(
        title="Actor",
        nav_active="actors",
        page_slug="actors",
        body=body,
        auto_refresh_seconds=10,
        snapshot=snapshot,
    )


def _render_runs_page(
    runs: list[dict],
    *,
    state: str = "",
    event_type: str = "",
    query: str = "",
    snapshot: dict | None = None,
) -> str:
    snapshot = snapshot or get_config_service().get_snapshot()
    states = ["", "queued", "running", "publishing", "succeeded", "skipped", "failed", "stale", "terminated"]
    event_types = [
        "",
        "auto_review",
        "mention",
        "daily_audit",
        "daily_audit_evolution",
        "daily_audit_direction_persistence",
        "daily_audit_short_term_persistence",
        "daily_audit_long_term_persistence",
        "daily_audit_skill_persistence",
    ]
    state_options = "".join(
        f'<option value="{escape(option)}"{" selected" if option == state else ""}>{_te(STATE_LABELS.get(option, option) if option else "全部状态")}</option>'
        for option in states
    )
    event_options = "".join(
        f'<option value="{escape(option)}"{" selected" if option == event_type else ""}>{_te(EVENT_TYPE_LABELS.get(option, option) if option else "全部事件")}</option>'
        for option in event_types
    )
    filtered = _filter_runs(
        runs,
        state=state,
        event_type=event_type,
        query=query,
        phoenix_base_url=_phoenix_base_url(snapshot),
    )
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">{_te("运行记录")}</div>
        <h2>{_te("执行日志")}</h2>
        <p class="lede">{_te("按状态、事件类型和关键字筛选 durable run 历史，并查看链路引用。")}</p>
      </div>
      <div class="page-meta">
        <span>{len(filtered)} {_te("条可见运行")}</span>
        <span>{_count_by_state(filtered, 'failed')} {_te("条失败")}</span>
      </div>
    </section>
    <section class="control-panel reveal">
      <form class="filter-form filter-form-wide" method="get" action="/admin/runs">
        <label class="filter-field">
          <span>{_te("状态")}</span>
          <select name="state">{state_options}</select>
        </label>
        <label class="filter-field">
          <span>{_te("事件")}</span>
          <select name="event_type">{event_options}</select>
        </label>
        <label class="filter-field filter-field-grow">
          <span>{_te("搜索")}</span>
          <input type="text" name="q" value="{escape(query)}" placeholder="{_te("运行 ID、Actor、原因")}">
        </label>
        <div class="filter-actions"><button type="submit">{_te("应用")}</button></div>
      </form>
    </section>
    <section class="control-panel reveal">
      <table class="console-table">
        <thead>
          <tr>
            <th>{_te("运行")}</th>
            <th>{_te("状态")}</th>
            <th>{_te("事件")}</th>
            <th>Actor</th>
            <th>{_te("原因")}</th>
            <th class="col-time">{_te("开始时间")}</th>
            <th class="col-trace">{_te("链路")}</th>
          </tr>
        </thead>
        <tbody>{_render_run_rows(filtered)}</tbody>
      </table>
    </section>
    """
    return _render_layout(
        title="运行记录",
        nav_active="runs",
        page_slug="runs",
        body=body,
        snapshot=snapshot,
    )


def _render_run_journal(journal: list[dict]) -> str:
    if not journal:
        return f'<p class="empty-note">{_te("当前没有执行时间线。")}</p>'
    rows = []
    for item in journal:
        rows.append(
            f"""
            <tr>
              <td>{escape(item.get('stage_key') or item.get('event_type') or '—')}</td>
              <td>{escape(item.get('status') or '—')}</td>
              <td>{escape(item.get('workflow_version') or '—')}</td>
              <td>{escape(item.get('summary') or '—')}</td>
              <td class="col-time">{_render_timestamp_cell(item.get('created_display') or '—')}</td>
            </tr>
            """
        )
    return (
        '<div class="detail-section timeline-section">'
        f'<div class="detail-section-head">{_te("执行时间线")}</div>'
        '<table class="console-table console-table-compact">'
        f'<thead><tr><th>{_te("阶段")}</th><th>{_te("状态")}</th><th>{_te("版本")}</th><th>{_te("摘要")}</th><th class="col-time">{_te("时间")}</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def _render_pending_event_rows(actor_key: str, pending_events: list[dict]) -> str:
    if not pending_events:
        return _render_table_empty(6, "当前没有排队任务。")
    rows = []
    for item in pending_events:
        action = (
            f'<button type="button" class="button" data-actor-pending-cancel="1" '
            f'data-actor-key="{escape(actor_key)}" data-event-id="{escape(str(item["event_id"]))}">{_te("取消排队任务")}</button>'
            if item.get("cancelable")
            else f'<span class="table-muted">{_te("不可取消")}</span>'
        )
        rows.append(
            f"""
            <tr>
              <td class="table-key">{escape(str(item.get("event_id") or "—"))}</td>
              <td>{escape(str(item.get("event_type_display") or "—"))}</td>
              <td>{_render_timestamp_cell(str(item.get("received_display") or "—"))}</td>
              <td>{escape(str(item.get("title") or "—"))}</td>
              <td>{escape(str(item.get("trigger_source") or "—"))}</td>
              <td>{action}</td>
            </tr>
            """
        )
    return "".join(rows)


def _render_actor_controls(actor_key: str, pending_events: list[dict], running_run: dict | None) -> str:
    running_html = f'<p class="empty-note">{_te("当前没有正在运行的任务。")}</p>'
    if running_run is not None:
        if running_run.get("termination_requested"):
            action = f'<span class="table-muted">{_te("终止请求已发送，等待运行到取消检查点")}</span>'
        elif running_run.get("terminatable"):
            action = (
                f'<button type="button" class="button danger" data-actor-run-terminate="1" '
                f'data-actor-key="{escape(actor_key)}" data-run-id="{escape(str(running_run["run_id"]))}">{_te("终止当前运行")}</button>'
            )
        else:
            action = f'<span class="table-muted">{_te("当前运行不可终止")}</span>'
        termination_detail = (
            f'<div class="detail-item"><span class="detail-key">{_te("终止请求")}</span><span class="detail-value">{escape(str(running_run.get("termination_requested_display") or _t("已发送")))}</span></div>'
            if running_run.get("termination_requested")
            else ""
        )
        running_html = (
            '<div class="detail-grid">'
            f'<div class="detail-item"><span class="detail-key">{_te("运行 ID")}</span><span class="detail-value">{escape(str(running_run.get("run_id") or "—"))}</span></div>'
            f'<div class="detail-item"><span class="detail-key">{_te("事件类型")}</span><span class="detail-value">{escape(str(running_run.get("event_type_display") or "—"))}</span></div>'
            f'<div class="detail-item"><span class="detail-key">{_te("状态")}</span><span class="detail-value">{escape(str(running_run.get("state_display") or "—"))}</span></div>'
            f'<div class="detail-item"><span class="detail-key">{_te("开始时间")}</span><span class="detail-value">{escape(str(running_run.get("started_display") or "—"))}</span></div>'
            f"{termination_detail}"
            "</div>"
            f'<div class="filter-actions">{action}</div>'
        )
    return f"""
    <section class="control-panel reveal">
      <div class="panel-head"><h3>{_te("当前运行")}</h3></div>
      {running_html}
      <p class="empty-note" data-actor-control-status></p>
    </section>
    <section class="control-panel reveal">
      <div class="panel-head"><h3>{_te("待处理队列")}</h3></div>
      <table class="console-table console-table-compact">
        <thead>
          <tr>
            <th>Event ID</th>
            <th>{_te("事件类型")}</th>
            <th>{_te("触发时间")}</th>
            <th>{_te("标题")}</th>
            <th>{_te("来源")}</th>
            <th>{_te("操作")}</th>
          </tr>
        </thead>
        <tbody>{_render_pending_event_rows(actor_key, pending_events)}</tbody>
      </table>
    </section>
    """


def _render_actor_detail(
    actor_summary: dict | None,
    runs: list[dict],
    actor_key: str,
    *,
    pending_events: list[dict],
    running_run: dict | None,
    supported_controls: bool,
    snapshot: dict | None = None,
) -> str:
    actor_summary = actor_summary or {
        "actor_key": actor_key,
        "pending_count": 0,
        "inflight_count": 0,
        "scheduled": False,
        "lease_owner": None,
        "lease_ttl_seconds": None,
        "last_run_state": None,
        "last_event_type": None,
        "last_started_display": "—",
    }
    snapshot = snapshot or get_config_service().get_snapshot()
    normalized_runs = [
        item if _run_is_prepared(item) else _prepare_run(item, phoenix_base_url=_phoenix_base_url(snapshot))
        for item in runs
    ]
    controls_html = (
        _render_actor_controls(actor_key, pending_events, running_run)
        if supported_controls
        else ""
    )
    history = "".join(
        f"""
        <article class="run-card reveal">
          <div class="run-card-head">
            <div class="run-card-meta">
              {_render_state_badge(item['state'])}
              <span>{escape(item['event_type'])}</span>
              <span>{escape(item['started_display'])}</span>
            </div>
            <div class="run-card-ref">{_render_trace_meta(item)}</div>
          </div>
          <h3>{escape(item.get('reason') or item.get('error') or _t('没有记录原因'))}</h3>
          <div class="detail-grid">{_render_detail_blocks(item)}</div>
          {_render_run_journal(item.get('journal', []))}
        </article>
        """
        for item in normalized_runs
    ) or f'<section class="control-panel"><p class="empty-note">{_te("当前还没有运行记录。")}</p></section>'
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">{_te("Actor 详情")}</div>
        <h2>{escape(actor_key)}</h2>
        <p class="lede">{_te("查看这个 Actor 的当前运行状态、租约持有者和执行历史。")}</p>
      </div>
      <div class="page-meta">
        <span>{_te("最近状态：")}{escape(actor_summary.get('last_run_state') or 'unknown')}</span>
        <span>{_te("最近事件：")}{escape(actor_summary.get('last_event_type') or '—')}</span>
      </div>
    </section>
    <section class="status-strip reveal">
      <article class="status-card"><span>{_te("排队")}</span><strong>{actor_summary['pending_count']}</strong></article>
      <article class="status-card"><span>{_te("执行中")}</span><strong>{actor_summary['inflight_count']}</strong></article>
      <article class="status-card"><span>{_te("已调度")}</span><strong>{_te('是' if actor_summary['scheduled'] else '否')}</strong></article>
      <article class="status-card"><span>{_te("租约持有者")}</span><strong>{escape(actor_summary.get('lease_owner') or '—')}</strong></article>
      <article class="status-card"><span>{_te("租约 TTL")}</span><strong>{actor_summary['lease_ttl_seconds'] if actor_summary.get('lease_ttl_seconds') is not None else '—'}</strong></article>
    </section>
    {controls_html}
    <section class="run-history">{history}</section>
    """
    return _render_layout(
        title="Actor 详情",
        nav_active="actors",
        page_slug="actor-detail",
        body=body,
        auto_refresh_seconds=10,
        body_attrs={"data-actor-key": actor_key},
        snapshot=snapshot,
    )


def _render_generic_setting_rows(fields: list[dict]) -> str:
    controls = []
    for item in fields:
        key = escape(item["key"])
        raw_label = str(item["label"])
        raw_description = str(item["description"])
        if item["key"].endswith("_SELF_EVOLUTION_ENABLED"):
            raw_label = "启用"
            raw_description = "是否启用该 Agent 的自我演进。"
        elif item["key"].endswith("_SELF_EVOLUTION_INTERVAL_DAYS"):
            raw_label = "每几天一次"
            raw_description = "按固定北京时间周期执行。"
        elif item["key"].endswith("_SELF_EVOLUTION_TIME_LOCAL"):
            raw_label = "执行时间"
            raw_description = "固定北京时间，格式 HH:MM。"
        label = _te(raw_label)
        description = _te(raw_description)
        value = item["value"]
        if item["key"] == "SANDBOX_TYPE":
            current = str(value or "local").strip().lower() or "local"
            control = (
                f'<label class="field-shell"><span class="field-title">{label}</span>'
                f'<select name="{key}">'
                f'<option value="local"{" selected" if current == "local" else ""}>local</option>'
                f'<option value="docker"{" selected" if current == "docker" else ""}>docker</option>'
                "</select></label>"
            )
        elif item["key"] == "DAILY_AUDIT_START_TIME_LOCAL" or item["key"].endswith("_SELF_EVOLUTION_TIME_LOCAL"):
            current = str(value or "02:00").strip() or "02:00"
            time_slots = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]
            if current not in time_slots:
                time_slots = [current, *time_slots]
            options = "".join(
                f'<option value="{escape(slot)}"{" selected" if slot == current else ""}>{escape(slot)}</option>'
                for slot in time_slots
            )
            control = (
                f'<label class="field-shell"><span class="field-title">{label}</span>'
                f'<select name="{key}" data-daily-audit-time-select>{options}</select></label>'
            )
        elif item["kind"] == "bool":
            checked = "checked" if value else ""
            control = (
                f'<input type="hidden" name="{key}" value="0">'
                f'<label class="toggle-row"><span>{label}</span>'
                f'<input type="checkbox" name="{key}" value="1" {checked}></label>'
            )
        elif item["kind"] == "int":
            control = (
                f'<label class="field-shell"><span class="field-title">{label}</span>'
                f'<input type="number" name="{key}" value="{value}"></label>'
            )
        elif item["kind"] == "multiline":
            rendered_value = escape("\n".join(value or []))
            control = (
                f'<label class="field-shell"><span class="field-title">{label}</span>'
                f'<textarea name="{key}" rows="4">{rendered_value}</textarea></label>'
            )
        elif item["sensitive"]:
            placeholder = _t("已配置，留空表示不修改") if item.get("configured") else _t("未配置")
            control = (
                f'<label class="field-shell"><span class="field-title">{label}</span>'
                '<span class="secret-input">'
                f'<input type="password" name="{key}" value="" placeholder="{placeholder}" autocomplete="off" spellcheck="false" data-secret-input>'
                f'<button type="button" class="secret-toggle" data-secret-toggle aria-label="{_te("显示或隐藏敏感信息")}" aria-pressed="false">'
                f"{_eye_icon_svg()}"
                "</button>"
                "</span></label>"
            )
        else:
            control = f'<label class="field-shell"><span class="field-title">{label}</span><input type="text" name="{key}" value="{escape(str(value))}"></label>'
        controls.append(
            f"""
            <div class="setting-row">
              <div class="setting-copy">
                <div class="field-name">{label}</div>
                <div class="field-desc">{description}</div>
              </div>
              <div class="setting-control">{control}</div>
            </div>
            """
        )
    return "".join(controls)


def _admin_static_href(filename: str) -> str:
    path = _ADMIN_STATIC_DIR / filename
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/admin/static/{filename}?v={version}"


def _eye_icon_svg() -> str:
    return '<span class="secret-toggle-glyph" aria-hidden="true">👁</span>'


def _render_llm_provider_section(snapshot: dict[str, str], provider: str) -> str:
    base_key = "OPENAI_BASE_URL" if provider == "openai" else "ANTHROPIC_BASE_URL"
    api_key = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    model_key = "OPENAI_MODEL" if provider == "openai" else "ANTHROPIC_MODEL"
    active_provider = snapshot.get("LLM_ACTIVE_PROVIDER", "openai")
    checked = "checked" if active_provider == provider else ""
    api_key_configured = bool(snapshot.get(api_key))
    api_key_placeholder = _t("已配置，留空表示不修改") if api_key_configured else _t("未配置")
    return f"""
    <section class="control-panel reveal llm-provider-panel{" is-active" if active_provider == provider else ""}" data-provider-panel="{provider}">
      <div class="panel-head">
        <div>
          <div class="eyebrow">Provider</div>
          <h3>{_te(PROVIDER_LABELS[provider])}</h3>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <div class="field-name">{_te("设为当前 Provider")}</div>
          <div class="field-desc">{_te("当前运行时将优先使用这个 Provider 的模型、地址和密钥。")}</div>
        </div>
        <div class="setting-control">
          <label class="toggle-row">
            <span>{_te(PROVIDER_LABELS[provider])}</span>
            <input type="radio" name="LLM_ACTIVE_PROVIDER" value="{provider}" {checked}>
          </label>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <div class="field-name">Base URL</div>
          <div class="field-desc">{_te("可填官方地址，也可填兼容网关地址；留空时默认使用官方地址。")}</div>
        </div>
        <div class="setting-control">
          <label class="field-shell">
            <span class="field-title">Base URL</span>
            <input type="text" name="{base_key}" value="{escape(str(snapshot.get(base_key, '')))}" data-llm-base-url>
          </label>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <div class="field-name">API Key</div>
          <div class="field-desc">{_te("敏感值不会明文回显；留空会继续使用已保存密钥，输入新值才会覆盖。")}</div>
        </div>
        <div class="setting-control">
          <label class="field-shell">
            <span class="field-title">API Key</span>
            <span class="secret-input">
              <input type="password" name="{api_key}" value="" placeholder="{api_key_placeholder}" autocomplete="off" spellcheck="false" data-llm-api-key data-secret-input>
              <button type="button" class="secret-toggle" data-secret-toggle aria-label="{_te("显示或隐藏敏感信息")}" aria-pressed="false">
                {_eye_icon_svg()}
              </button>
            </span>
          </label>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <div class="field-name">{_te("模型")}</div>
          <div class="field-desc">{_te("点击“刷新模型列表”会优先使用当前表单中的地址和密钥；留空项会回退到官方默认地址或已保存密钥。拉取失败时仍可手动输入模型名。")}</div>
        </div>
        <div class="setting-control">
          <label class="field-shell">
            <span class="field-title">{_te("模型名称")}</span>
            <input type="text" name="{model_key}" value="{escape(str(snapshot.get(model_key, '')))}" list="{provider}-model-list" data-llm-model-input>
            <datalist id="{provider}-model-list"></datalist>
          </label>
          <div class="inline-actions">
            <button type="button" class="secondary-button llm-refresh-button" data-model-refresh="{provider}">{_te("刷新模型列表")}</button>
            <button type="button" class="secondary-button llm-test-button" data-llm-test="{provider}">{_te("测试真实请求")}</button>
            <span class="field-note llm-status" data-llm-status="{provider}">{_te("未拉取模型列表")}</span>
          </div>
          <pre class="llm-test-output" data-llm-test-output="{provider}" hidden></pre>
        </div>
      </div>
    </section>
    """


def _render_llm_settings(snapshot: dict[str, str]) -> str:
    return """
    <section class="control-panel reveal">
      <div class="panel-head">
        <div>
          <div class="eyebrow">LLM</div>
          <h3>{_te("模型服务")}</h3>
        </div>
      </div>
      <div class="settings-note">
        {_te("当前支持 OpenAI 兼容接口和 Anthropic 兼容接口。旧版")} <code>LLM_MODEL_ID</code> {_te("仍会自动同步，用于兼容现有运行时。")}
      </div>
    </section>
    """ + _render_llm_provider_section(snapshot, "openai") + _render_llm_provider_section(snapshot, "anthropic")


def _render_gitlab_identity_panel(snapshot: dict[str, str]) -> str:
    resolved = resolve_bot_identity()
    identity = resolved.identity
    actual_username = identity.username if identity else "—"
    actual_name = identity.name if identity else "—"
    actual_user_id = str(identity.user_id) if identity and identity.user_id is not None else "—"
    source_label = {
        "live": "实时身份",
        "cached": "缓存身份",
        "unavailable": "身份不可用",
    }.get(resolved.source, resolved.source)
    avatar_html = ""
    if identity and identity.avatar_url:
        avatar_html = (
            f'<img class="identity-avatar" src="{escape(identity.avatar_url)}" '
            f'alt="{escape(identity.username)}" loading="lazy">'
        )
    else:
        avatar_html = '<div class="identity-avatar identity-avatar-fallback">GitLab</div>'

    notice = ""
    if resolved.source == "unavailable":
        notice = f'<div class="flash error">{_te("当前无法解析 GitLab Bot 身份：")}{escape(_t(resolved.error or "unknown error"))}</div>'
    elif resolved.source == "cached":
        notice = (
            f'<div class="flash error">{_te("当前使用缓存身份。最近一次实时解析失败：")}{escape(_t(resolved.error or "unknown error"))}</div>'
        )
    else:
        notice = f'<div class="flash">{_te("当前 GitLab Bot 身份来自实时解析。")}</div>'

    return f"""
    <section class="control-panel reveal">
      <div class="panel-head">
        <div>
          <div class="eyebrow">{_te("GitLab 身份")}</div>
          <h3>{_te("当前 Token 身份")}</h3>
        </div>
      </div>
      {notice}
      <div class="identity-card">
        {avatar_html}
        <div class="identity-grid">
          <div class="detail-item"><span class="detail-key">{_te("当前用户名")}</span><span class="detail-value">{escape(actual_username)}</span></div>
          <div class="detail-item"><span class="detail-key">{_te("显示名称")}</span><span class="detail-value">{escape(actual_name)}</span></div>
          <div class="detail-item"><span class="detail-key">{_te("用户 ID")}</span><span class="detail-value">{escape(actual_user_id)}</span></div>
          <div class="detail-item"><span class="detail-key">{_te("身份来源")}</span><span class="detail-value">{_te(source_label)}</span></div>
        </div>
      </div>
    </section>
    """


def _render_gitlab_settings(snapshot: dict[str, str], current_fields: list[dict]) -> str:
    webhook_url = str(snapshot.get("OPEN_REVIEW_EXTERNAL_URL", "") or "").strip().rstrip("/")
    webhook_url = f"{webhook_url}/webhooks/gitlab" if webhook_url else _t("未配置")
    project_targets = [str(item).strip() for item in snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]
    display_base_url = str(snapshot.get("GITLAB_EXTERNAL_URL") or snapshot.get("GITLAB_API_URL") or "").strip()
    project_rows = project_targets or [""]
    rendered_rows = "".join(
        f"""
        <div class="gitlab-project-row" data-gitlab-project-row>
          <input type="text" value="{escape(build_gitlab_project_clone_url(project, external_url=display_base_url) or project)}" data-gitlab-project-item placeholder="https://gitlab.example.com/group/project.git" spellcheck="false">
          <button type="button" class="secondary-button" data-gitlab-project-remove>{_te("删除")}</button>
        </div>
        """
        for project in project_rows
    )
    hidden_targets = escape("\n".join(project_targets).strip())
    inferred_external = str(snapshot.get("GITLAB_EXTERNAL_URL") or "").strip()
    api_url = str(snapshot.get("GITLAB_API_URL") or "").strip()
    api_override = api_url if api_url and api_url != inferred_external else ""
    remaining_fields = [
        item
        for item in current_fields
        if item["key"] not in {"GITLAB_TARGET_PROJECTS", "GITLAB_EXTERNAL_URL", "GITLAB_API_URL"}
    ]
    return (
        _render_gitlab_identity_panel(snapshot)
        + f"""
        <section class="control-panel reveal">
          <div class="panel-head">
            <div>
          <div class="eyebrow">{_te("GitLab 部署")}</div>
          <h3>{_te("验证与同步")}</h3>
            </div>
          </div>
          <div class="settings-note">
            {_te("保存 GitLab 设置后，先验证连接，再同步目标 Projects 的 project webhook。项目列表以仓库链接为主输入；系统会自动推断 GitLab 外部地址，并在未填写覆盖值时让 API 地址跟随同一个地址。当前目标 webhook：")}<code>{escape(webhook_url)}</code>
          </div>
          <div class="inline-actions">
            <button type="button" class="secondary-button" data-gitlab-action="verify">{_te("验证 GitLab 连接")}</button>
            <button type="button" class="secondary-button" data-gitlab-action="sync">{_te("配置/同步 Webhook")}</button>
            <span class="field-note llm-status" data-gitlab-status="summary">{_te("尚未验证 GitLab 部署状态。")}</span>
          </div>
          <div class="gitlab-checklist" data-gitlab-checklist></div>
          <div class="gitlab-results" data-gitlab-results></div>
        </section>
        """
        + f"""
        <section class="control-panel reveal">
          <div class="panel-head">
            <div>
              <div class="eyebrow">{_te("GitLab 项目")}</div>
              <h3>{_te("仓库链接")}</h3>
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-copy">
              <div class="field-name">{_te("项目仓库")}</div>
              <div class="field-desc">{_te("支持当前 GitLab 实例的 HTTPS 仓库 URL 或 project path。保存后内部仍统一转换成 canonical project path。")}</div>
            </div>
            <div class="setting-control">
              <div class="field-shell gitlab-project-targets" data-gitlab-project-targets>
                <span class="field-title">{_te("仓库链接")}</span>
                <div class="gitlab-project-list" data-gitlab-project-list>
                  {rendered_rows}
                </div>
                <div class="inline-actions">
                  <button type="button" class="secondary-button" data-gitlab-project-add>{_te("添加项目")}</button>
                </div>
                <textarea name="GITLAB_TARGET_PROJECTS" rows="1" hidden data-gitlab-targets-input>{hidden_targets}</textarea>
                <div class="field-note">{_te("自动推断 GitLab 外部地址：")}<code>{escape(inferred_external or _t("保存后自动生成"))}</code></div>
              </div>
            </div>
          </div>
        </section>
        <details class="control-panel reveal gitlab-advanced">
          <summary class="gitlab-advanced-summary">{_te("高级设置")}</summary>
          <div class="setting-row">
            <div class="setting-copy">
              <div class="field-name">{_te("GitLab API 地址覆盖")}</div>
              <div class="field-desc">{_te("默认跟随仓库链接推断出的地址；仅在 worker 访问 GitLab API/clone 需要走不同地址时填写。")}</div>
            </div>
            <div class="setting-control">
              <label class="field-shell">
                <span class="field-title">{_te("API 地址覆盖")}</span>
                <input type="text" name="GITLAB_API_URL_OVERRIDE" value="{escape(api_override)}" placeholder="{_te("留空表示跟随仓库链接推断")}">
              </label>
            </div>
          </div>
        </details>
        """
        + _render_generic_setting_rows(remaining_fields)
    )


def _render_daily_audit_settings(snapshot: dict[str, str], current_fields: list[dict]) -> str:
    target_preview = _build_target_preview(snapshot)
    return _render_agent_settings_subsection(
        eyebrow="运行配置",
        title="运行配置",
        section_kind="runtime",
        description=f"{_te('当前目标项目：')}<code>{escape(target_preview)}</code>",
        action_html=f'<button type="button" class="secondary-button" data-daily-audit-action="trigger">{_te("立即触发日常审计")}</button>',
        status_html=f'<span class="field-note llm-status" data-daily-audit-status="summary">{_te("尚未触发日常审计。")}</span>',
        body=_render_generic_setting_rows(current_fields),
    )


def _build_target_preview(snapshot: dict[str, str]) -> str:
    target_projects = [str(item).strip() for item in snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]
    target_preview = ", ".join(target_projects[:3]) if target_projects else _t("未配置项目")
    if len(target_projects) > 3:
        target_preview += f" {_t('等')} {len(target_projects)} {_t('个项目')}"
    return target_preview


def _render_agent_settings_subsection(
    *,
    eyebrow: str,
    title: str,
    section_kind: str,
    body: str,
    description: str = "",
    action_html: str = "",
    status_html: str = "",
) -> str:
    description_html = f'<div class="settings-note agent-settings-note">{description}</div>' if description else ""
    status_line = f'<div class="agent-settings-status">{status_html}</div>' if status_html else ""
    actions = f'<div class="agent-settings-actions">{action_html}</div>' if action_html else ""
    return f"""
    <section class="agent-settings-section" data-agent-section="{escape(section_kind)}">
      <div class="agent-settings-section-head">
        <div class="agent-settings-section-copy">
          <div class="eyebrow">{_te(eyebrow)}</div>
          <h4>{_te(title)}</h4>
          {description_html}
        </div>
        {actions}
      </div>
      {status_line}
      <div class="agent-settings-section-body">
        {body}
      </div>
    </section>
    """


def _render_self_evolution_section(
    *,
    snapshot: dict[str, str],
    agent_type: str,
    fields: list[dict],
) -> str:
    if not fields:
        return ""
    target_preview = _build_target_preview(snapshot)
    return _render_agent_settings_subsection(
        eyebrow="Self-Evolution",
        title="自我演进",
        section_kind="self-evolution",
        description=f"{_te('当前目标项目：')}<code>{escape(target_preview)}</code>",
        action_html=(
            '<button type="button" class="secondary-button" '
            f'data-self-evolution-action="trigger" data-self-evolution-agent="{escape(agent_type)}">{_te("立即触发")}</button>'
        ),
        status_html=(
            f'<span class="field-note llm-status" data-self-evolution-status="{escape(agent_type)}">'
            f'{_te("尚未触发自我演进。")}'
            "</span>"
        ),
        body=_render_generic_setting_rows(fields),
    )


def _render_agent_section(*, eyebrow: str, title: str, agent_key: str, body: str) -> str:
    return f"""
    <section class="control-panel reveal agent-settings-card" data-agent-card="{escape(agent_key)}">
      <div class="panel-head">
        <div>
          <div class="eyebrow">{_te(eyebrow)}</div>
          <h3>{_te(title)}</h3>
        </div>
      </div>
      {body}
    </section>
    """


def _render_agent_settings(snapshot: dict[str, str], current_fields: list[dict]) -> str:
    mention_fields = [
        field for field in current_fields if field["key"].startswith("MENTION_") and "_SELF_EVOLUTION_" not in field["key"]
    ]
    mention_evolution_fields = [field for field in current_fields if field["key"].startswith("MENTION_SELF_EVOLUTION_")]
    daily_audit_fields = [
        field
        for field in current_fields
        if field["key"].startswith("DAILY_AUDIT_") and "_SELF_EVOLUTION_" not in field["key"]
    ]
    daily_audit_evolution_fields = [
        field for field in current_fields if field["key"].startswith("DAILY_AUDIT_SELF_EVOLUTION_")
    ]
    review_fields = [
        field
        for field in current_fields
        if field["key"].startswith("AUTO_REVIEW_") and "_SELF_EVOLUTION_" not in field["key"]
    ]
    review_evolution_fields = [field for field in current_fields if field["key"].startswith("AUTO_REVIEW_SELF_EVOLUTION_")]

    sections: list[str] = []
    if mention_fields or mention_evolution_fields:
        mention_runtime = _render_generic_setting_rows(mention_fields) or (
            f'<div class="settings-note agent-settings-empty">{_te("当前没有额外运行配置。")}</div>'
        )
        mention_body = _render_agent_settings_subsection(
            eyebrow="运行配置",
            title="运行配置",
            section_kind="runtime",
            body=mention_runtime,
        )
        if mention_evolution_fields:
            mention_body += _render_self_evolution_section(
                snapshot=snapshot,
                agent_type="mention",
                fields=mention_evolution_fields,
            )
        sections.append(
            _render_agent_section(
                eyebrow="Agent",
                title="Mention",
                agent_key="mention",
                body=mention_body,
            )
        )
    if daily_audit_fields or daily_audit_evolution_fields:
        daily_audit_body = _render_daily_audit_settings(snapshot, daily_audit_fields)
        if daily_audit_evolution_fields:
            daily_audit_body += _render_self_evolution_section(
                snapshot=snapshot,
                agent_type="daily_audit",
                fields=daily_audit_evolution_fields,
            )
        sections.append(
            _render_agent_section(
                eyebrow="Agent",
                title="Daily Audit",
                agent_key="daily_audit",
                body=daily_audit_body,
            )
        )
    if review_fields or review_evolution_fields:
        review_runtime = _render_generic_setting_rows(review_fields) or (
            f'<div class="settings-note agent-settings-empty">{_te("当前没有额外运行配置。")}</div>'
        )
        review_body = _render_agent_settings_subsection(
            eyebrow="运行配置",
            title="运行配置",
            section_kind="runtime",
            body=review_runtime,
        )
        if review_evolution_fields:
            review_body += _render_self_evolution_section(
                snapshot=snapshot,
                agent_type="auto_review",
                fields=review_evolution_fields,
            )
        sections.append(
            _render_agent_section(
                eyebrow="Agent",
                title="Auto Review",
                agent_key="auto_review",
                body=review_body,
            )
        )
    return "".join(sections)


def _render_settings(fields: list[dict], *, active_group: str, flash: str = "") -> str:
    snapshot = get_config_service().get_snapshot()
    groups = _ordered_groups(fields)
    if active_group not in groups:
        active_group = groups[0]
    tabs = "".join(
        f'<a class="settings-link{" active" if group == active_group else ""}" href="/admin/settings?group={escape(group)}">{escape(_group_label(group))}</a>'
        for group in groups
    )
    current_fields = [field for field in fields if field["group"] == active_group]
    if active_group == "GitLab":
        form_body = _render_gitlab_settings(snapshot, current_fields)
    elif active_group == "LLM":
        form_body = _render_llm_settings(snapshot)
    elif active_group == "Agent":
        form_body = _render_agent_settings(snapshot, current_fields)
    else:
        form_body = _render_generic_setting_rows(current_fields)
    sandbox_note = ""
    if active_group == "Sandbox":
        sandbox_note = (
            '<div class="settings-note">'
            f'{_te("Sandbox 配置保存后需要重启 worker 才会对新的运行生效。")}'
            "</div>"
        )
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">{_te("设置")}</div>
        <h2>{_te("Open Review 配置")}</h2>
        <p class="lede">{_te("按分组维护集成、运行时、审查与 tracing 配置。")}</p>
      </div>
      <div class="page-meta">
        <span>{escape(_group_label(active_group))}</span>
      </div>
    </section>
    <section class="settings-layout">
      <aside class="settings-sidebar">
        <div class="settings-sidebar-title">{_te("设置分组")}</div>
        <nav class="settings-nav">{tabs}</nav>
      </aside>
      <div class="settings-content">
        <section class="control-panel reveal">
          {sandbox_note}
          <form method="post" action="/admin/settings?group={escape(active_group)}" class="settings-form" data-settings-form="1">
            {form_body}
            <div class="sticky-actions"><button type="submit">{_te("保存设置")}</button></div>
          </form>
        </section>
      </div>
    </section>
    """
    return _render_layout(
        title="Open Review 配置",
        nav_active="settings",
        page_slug="settings",
        body=body,
        flash=flash,
    )


def _render_security(flash: str = "") -> str:
    body = f"""
    <section class="page-header reveal">
      <div class="page-header-copy">
        <div class="eyebrow">{_te("安全")}</div>
        <h2>{_te("安全设置")}</h2>
        <p class="lede">{_te("修改内置管理后台的管理员密码。修改后新密码立即生效。")}</p>
      </div>
    </section>
    <section class="control-panel reveal">
      <div class="panel-head">
        <div>
          <div class="eyebrow">{_te("管理员")}</div>
          <h3>{_te("管理员密码")}</h3>
        </div>
      </div>
      <form method="post" action="/admin/security/password" class="settings-form compact">
        <label class="field-shell">
          <span class="field-title">{_te("新密码")}</span>
          <input type="password" name="password" required>
        </label>
        <div class="sticky-actions"><button type="submit">{_te("更新密码")}</button></div>
      </form>
    </section>
    """
    return _render_layout(
        title="安全",
        nav_active="security",
        page_slug="security",
        body=body,
        flash=flash,
    )




@router.get("/admin/language")
async def admin_language(request: Request):
    lang = request.query_params.get("lang", "zh")
    if lang not in SUPPORTED_ADMIN_LANGS:
        lang = "zh"
    next_url = request.query_params.get("next") or "/admin"
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc or not next_url.startswith("/admin"):
        next_url = "/admin"
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        LANG_COOKIE_NAME,
        lang,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
    )
    return response


@router.get("/admin/setup", response_class=HTMLResponse)
async def admin_setup_page(request: Request):
    if _admin_is_initialized():
        if _is_authenticated(request):
            return _redirect_response(request, "/admin", status_code=303)
        return _redirect_response(request, "/admin/login", status_code=303)
    return _render_html_response(request, lambda: _render_setup())


@router.post("/admin/setup")
async def admin_setup(request: Request):
    if _admin_is_initialized():
        if _is_authenticated(request):
            return _redirect_response(request, "/admin", status_code=303)
        return _redirect_response(request, "/admin/login", status_code=303)
    form = await _parse_form(request)
    password = form.get("password", "")
    try:
        get_config_service().create_initial_admin(password)
    except ValueError as exc:
        return _render_html_response(request, lambda: _render_setup(str(exc)), status_code=400)

    response = RedirectResponse("/admin/settings?flash=%E5%88%9D%E5%A7%8B%E5%8C%96%E5%B7%B2%E5%AE%8C%E6%88%90", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        _sign_session(
            {
                "user": "admin",
                "expires_at": (now_in_open_review_tz() + timedelta(days=7)).isoformat(),
            }
        ),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if not _admin_is_initialized():
        return _redirect_response(request, "/admin/setup", status_code=303)
    return _render_html_response(request, lambda: _render_login())


@router.post("/admin/login")
async def admin_login(request: Request):
    if not _admin_is_initialized():
        return _redirect_response(request, "/admin/setup", status_code=303)
    form = await _parse_form(request)
    if not get_config_service().verify_admin_password(form.get("password", "")):
        return _render_html_response(request, lambda: _render_login("密码错误。"), status_code=401)

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        _sign_session(
            {
                "user": "admin",
                "expires_at": (now_in_open_review_tz() + timedelta(days=7)).isoformat(),
            }
        ),
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/admin", response_class=HTMLResponse)
async def admin_overview(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    snapshot = get_config_service().get_snapshot()
    runtime_statuses = [item.model_dump() for item in await (await get_runtime_store()).list_actor_statuses()]
    recent_runs = get_config_service().list_recent_runs(limit=100)
    return _render_html_response(
        request,
        lambda: _render_dashboard(
            runtime_statuses,
            recent_runs,
            request.query_params.get("flash", ""),
            snapshot=snapshot,
        )
    )


@router.get("/admin/actors", response_class=HTMLResponse)
async def admin_actors(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    snapshot = get_config_service().get_snapshot()
    runtime_statuses = [item.model_dump() for item in await (await get_runtime_store()).list_actor_statuses()]
    recent_runs = _prepare_runs(
        get_config_service().list_recent_runs(limit=150),
        phoenix_base_url=_phoenix_base_url(snapshot),
    )
    actors = _build_actor_summaries(runtime_statuses, recent_runs)
    return _render_html_response(
        request,
        lambda: _render_actors_page(actors, query=request.query_params.get("q", ""), snapshot=snapshot),
    )


async def _actor_runtime_detail(actor_key: str) -> tuple[list[dict], dict | None]:
    runtime_store = await get_runtime_store()
    list_actor_events = getattr(runtime_store, "list_actor_events", None)
    list_runs = getattr(runtime_store, "list_runs", None)
    pending_events = (
        [_serialize_pending_event(item) for item in await list_actor_events(actor_key)]
        if callable(list_actor_events)
        else []
    )
    runtime_runs = (
        [item.model_dump(mode="json") for item in await list_runs(actor_key, limit=30)]
        if callable(list_runs)
        else []
    )
    running_run = _pick_running_run(runtime_runs)
    get_run_termination = getattr(runtime_store, "get_run_termination", None)
    if running_run is not None and callable(get_run_termination):
        termination = await get_run_termination(str(running_run.get("run_id") or ""))
        if termination is not None:
            running_run["termination_requested"] = True
            running_run["termination_requested_at"] = termination.requested_at
            running_run["termination_requested_display"] = _format_timestamp(termination.requested_at)
            running_run["terminatable"] = False
    return pending_events, running_run


@router.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    snapshot = get_config_service().get_snapshot()
    return _render_html_response(
        request,
        lambda: _render_runs_page(
            get_config_service().list_recent_runs(limit=200),
            state=request.query_params.get("state", ""),
            event_type=request.query_params.get("event_type", ""),
            query=request.query_params.get("q", ""),
            snapshot=snapshot,
        )
    )


@router.get("/admin/mrs/{actor_key:path}", response_class=HTMLResponse)
async def admin_actor_detail(request: Request, actor_key: str):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    snapshot = get_config_service().get_snapshot()
    phoenix_base_url = _phoenix_base_url(snapshot)
    runtime_store = await get_runtime_store()
    runtime_statuses = [item.model_dump() for item in await runtime_store.list_actor_statuses()]
    recent_runs = _prepare_runs(
        get_config_service().list_recent_runs(limit=200),
        phoenix_base_url=phoenix_base_url,
    )
    actor_summary = next(
        (item for item in _build_actor_summaries(runtime_statuses, recent_runs) if item["actor_key"] == actor_key),
        None,
    )
    runs = await _attach_run_journal(
        get_config_service().list_runs_for_actor(actor_key, limit=30),
        phoenix_base_url=phoenix_base_url,
        journal_limit=_ACTOR_DETAIL_JOURNAL_LIMIT,
    )
    pending_events, running_run = await _actor_runtime_detail(actor_key)
    return _render_html_response(
        request,
        lambda: _render_actor_detail(
            actor_summary,
            runs,
            actor_key,
            pending_events=pending_events,
            running_run=running_run,
            supported_controls=_actor_supports_controls(actor_key),
            snapshot=snapshot,
        )
    )


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    fields = get_config_service().list_fields()
    return _render_html_response(
        request,
        lambda: _render_settings(
            fields,
            active_group=request.query_params.get("group", ""),
            flash=request.query_params.get("flash", ""),
        )
    )


@router.post("/admin/settings")
async def admin_settings_update(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    form = await _parse_form(request)
    active_group = request.query_params.get("group", "")
    previous_snapshot = get_config_service().get_snapshot()
    try:
        if active_group == "GitLab":
            project_values = [item.strip() for item in str(form.get("GITLAB_TARGET_PROJECTS", "")).splitlines() if item.strip()]
            current_external_url = str(previous_snapshot.get("GITLAB_EXTERNAL_URL", ""))
            current_api_url = str(previous_snapshot.get("GITLAB_API_URL", ""))
            configured_targets = [str(item).strip() for item in previous_snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]
            bootstrap = settings.bootstrap_snapshot()
            if (
                not configured_targets
                and current_external_url == bootstrap.GITLAB_EXTERNAL_URL
                and current_api_url == bootstrap.GITLAB_API_URL
            ):
                current_external_url = ""
                current_api_url = ""
            inferred_external = infer_gitlab_external_url(
                project_values,
                current_external_url=current_external_url,
                current_api_url=current_api_url,
            )
            if configured_targets and project_values:
                first_project = project_values[0]
                parsed_first = urlparse(first_project)
                current_hosts = {
                    parsed.netloc.lower()
                    for parsed in (
                        urlparse(current_external_url),
                        urlparse(current_api_url),
                    )
                    if parsed.netloc
                }
                if parsed_first.scheme and parsed_first.netloc and parsed_first.netloc.lower() not in current_hosts:
                    normalized_target = parse_gitlab_project_target(
                        first_project,
                        api_url=inferred_external,
                        external_url=inferred_external,
                    )
                    if normalized_target not in configured_targets:
                        raise ValueError("只支持当前 GitLab 实例的 HTTPS 仓库 URL。")
            api_override = str(form.pop("GITLAB_API_URL_OVERRIDE", "") or "").strip()
            form["GITLAB_EXTERNAL_URL"] = inferred_external
            form["GITLAB_API_URL"] = api_override or inferred_external
        get_config_service().set_values(form, actor="admin")
    except ValueError as exc:
        flash = str(exc)
        target = f"/admin/settings?flash={quote_plus(flash)}"
        if active_group:
            target += f"&group={escape(active_group)}"
        return RedirectResponse(target, status_code=303)
    flash = "设置已保存"
    if active_group == "Sandbox":
        changed = any(
            str(previous_snapshot.get(key)) != str(get_config_service().get_snapshot().get(key))
            for key in ("SANDBOX_TYPE", "DOCKER_IMAGE")
            if key in form
        )
        if changed:
            flash = "设置已保存，重启 worker 后生效"
    target = f"/admin/settings?flash={quote_plus(flash)}"
    if active_group:
        target += f"&group={escape(active_group)}"
    return RedirectResponse(target, status_code=303)


@router.get("/admin/security", response_class=HTMLResponse)
async def admin_security_page(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    return _render_html_response(request, lambda: _render_security(request.query_params.get("flash", "")))


@router.post("/admin/security/password")
async def admin_password_update(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    form = await _parse_form(request)
    if form.get("password"):
        get_config_service().set_admin_password(form["password"])
    return RedirectResponse("/admin/security?flash=%E5%AF%86%E7%A0%81%E5%B7%B2%E6%9B%B4%E6%96%B0", status_code=303)


@router.get("/admin/api/overview")
async def admin_api_overview(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    snapshot = get_config_service().get_snapshot()
    runtime_statuses = [item.model_dump() for item in await (await get_runtime_store()).list_actor_statuses()]
    recent_runs = _prepare_runs(
        get_config_service().list_recent_runs(limit=100),
        phoenix_base_url=_phoenix_base_url(snapshot),
    )
    actor_summaries = _build_actor_summaries(runtime_statuses, recent_runs)
    return JSONResponse(
        {
            "metrics": {
                "active_actors": sum(1 for item in actor_summaries if item["inflight_count"]),
                "queued_events": sum(item["pending_count"] for item in actor_summaries),
                "failed_runs": _count_by_state(recent_runs, "failed"),
                "stale_runs": _count_by_state(recent_runs, "stale"),
                "known_actors": len(actor_summaries),
            },
            "actors": actor_summaries,
            "attention_runs": [item for item in recent_runs if item["state"] in {"failed", "stale"}][:10],
            "recent_runs": recent_runs[:15],
        }
    )


@router.get("/admin/api/runs")
async def admin_api_runs(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    snapshot = get_config_service().get_snapshot()
    runs = get_config_service().list_recent_runs(limit=200)
    return JSONResponse(
        {
            "runs": _filter_runs(
                runs,
                state=request.query_params.get("state", ""),
                event_type=request.query_params.get("event_type", ""),
                query=request.query_params.get("q", ""),
                phoenix_base_url=_phoenix_base_url(snapshot),
            )
        }
    )


@router.get("/admin/api/actors")
async def admin_api_actors(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    snapshot = get_config_service().get_snapshot()
    runtime_statuses = [item.model_dump() for item in await (await get_runtime_store()).list_actor_statuses()]
    recent_runs = _prepare_runs(
        get_config_service().list_recent_runs(limit=150),
        phoenix_base_url=_phoenix_base_url(snapshot),
    )
    actors = _build_actor_summaries(runtime_statuses, recent_runs)
    query = request.query_params.get("q", "").lower().strip()
    if query:
        actors = [item for item in actors if query in item["actor_key"].lower()]
    return JSONResponse({"actors": actors})


@router.get("/admin/api/actors/{actor_key:path}")
async def admin_api_actor_detail(request: Request, actor_key: str):
    if not _is_authenticated(request):
        return _json_auth_error()
    snapshot = get_config_service().get_snapshot()
    phoenix_base_url = _phoenix_base_url(snapshot)
    runtime_store = await get_runtime_store()
    runtime_statuses = [item.model_dump() for item in await runtime_store.list_actor_statuses()]
    recent_runs = _prepare_runs(
        get_config_service().list_recent_runs(limit=200),
        phoenix_base_url=phoenix_base_url,
    )
    actor_summary = next(
        (item for item in _build_actor_summaries(runtime_statuses, recent_runs) if item["actor_key"] == actor_key),
        None,
    )
    runs = await _attach_run_journal(
        get_config_service().list_runs_for_actor(actor_key, limit=30),
        phoenix_base_url=phoenix_base_url,
    )
    pending_events, running_run = await _actor_runtime_detail(actor_key)
    return JSONResponse(
        {
            "actor": actor_summary,
            "runs": runs,
            "pending_events": pending_events,
            "running_run": running_run,
            "supported_controls": _actor_supports_controls(actor_key),
        }
    )


@router.post("/admin/api/actors/{actor_key:path}/pending/{event_id:path}/cancel")
async def admin_api_cancel_pending_actor_event(request: Request, actor_key: str, event_id: str):
    if not _is_authenticated(request):
        return _json_auth_error()
    runtime_store = await get_runtime_store()
    pending_events = await runtime_store.list_actor_events(actor_key)
    target = next((item for item in pending_events if item.event_id == event_id), None)
    if target is None:
        return JSONResponse({"status": "conflict", "error": _t("该排队任务已不存在。")}, status_code=409)
    if not _is_controllable_event_type(target.event_type):
        return JSONResponse({"status": "conflict", "error": _t("该任务当前不支持手动取消。")}, status_code=409)
    removed = await runtime_store.remove_pending_event(actor_key, event_id)
    if not removed:
        return JSONResponse({"status": "conflict", "error": _t("该排队任务已进入执行或已不存在。")}, status_code=409)
    return JSONResponse({"status": "ok", "actor_key": actor_key, "event_id": event_id})


@router.post("/admin/api/actors/{actor_key:path}/runs/{run_id:path}/terminate")
async def admin_api_terminate_actor_run(request: Request, actor_key: str, run_id: str):
    if not _is_authenticated(request):
        return _json_auth_error()
    runtime_store = await get_runtime_store()
    runtime_runs = [
        item.model_dump(mode="json")
        for item in await runtime_store.list_runs(actor_key, limit=30)
    ]
    target = next((item for item in runtime_runs if item.get("run_id") == run_id), None)
    if target is None:
        return JSONResponse({"status": "conflict", "error": _t("该运行已不存在。")}, status_code=409)
    if target.get("state") not in {"running", "publishing"}:
        return JSONResponse({"status": "conflict", "error": _t("该运行当前不在执行中。")}, status_code=409)
    if not _is_controllable_event_type(str(target.get("event_type") or "")):
        return JSONResponse({"status": "conflict", "error": _t("该运行当前不支持手动终止。")}, status_code=409)
    termination = await runtime_store.request_run_termination(
        run_id,
        actor_key=actor_key,
        requested_by="admin",
    )
    cancelled_pending_events: list[str] = []
    for event in await runtime_store.list_actor_events(actor_key):
        if not _is_controllable_event_type(event.event_type):
            continue
        if await runtime_store.remove_pending_event(actor_key, event.event_id):
            cancelled_pending_events.append(event.event_id)
    return JSONResponse(
        {
            "status": "ok",
            "actor_key": actor_key,
            "run_id": run_id,
            "requested_by": termination.requested_by,
            "requested_at": termination.requested_at,
            "cancelled_pending_events": cancelled_pending_events,
        }
    )


@router.get("/admin/api/settings")
async def admin_api_settings(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    return JSONResponse({"fields": get_config_service().list_fields()})


@router.post("/admin/api/settings")
async def admin_api_settings_update(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    payload = await request.json()
    get_config_service().set_values(payload, actor="admin")
    return JSONResponse({"status": "ok"})


@router.post("/admin/api/gitlab/verify")
async def admin_api_gitlab_verify(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    try:
        payload = await asyncio.to_thread(verify_gitlab_configuration)
        return JSONResponse(_localize_gitlab_deploy_payload(payload))
    except Exception as exc:
        return JSONResponse(
            _localize_gitlab_deploy_payload({"status": "invalid", "checks": [], "error": str(exc)}),
            status_code=500,
        )


@router.post("/admin/api/gitlab/webhooks/sync")
async def admin_api_gitlab_webhooks_sync(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    try:
        payload = await asyncio.to_thread(sync_gitlab_webhooks)
        return JSONResponse(_localize_gitlab_deploy_payload(payload))
    except Exception as exc:
        return JSONResponse(
            _localize_gitlab_deploy_payload({"status": "invalid", "results": [], "error": str(exc)}),
            status_code=500,
        )


@router.post("/admin/api/daily-audit/trigger")
async def admin_api_daily_audit_trigger(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()

    snapshot = get_config_service().get_snapshot()
    target_projects = [str(item).strip() for item in snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]
    if not target_projects:
        return JSONResponse({"status": "invalid", "error": _t("当前没有配置任何 GitLab Projects。")}, status_code=400)

    results: list[dict[str, str]] = []
    for project_id in target_projects:
        default_branch = "main"
        branch_source = "fallback"
        try:
            default_branch = await asyncio.to_thread(get_project_default_branch, project_id)
            branch_source = "gitlab"
        except Exception:
            branch_source = "fallback"
        timestamp = now_in_open_review_tz()
        event = EventEnvelope(
            event_id=f"admin-daily-audit:{project_id}:{timestamp.strftime('%Y%m%d%H%M%S%f')}",
            event_type="daily_audit",
            project_id=project_id,
            mr_iid=None,
            source_branch=default_branch,
            target_branch=default_branch,
            title=f"Manual daily audit {timestamp.strftime('%Y-%m-%d %H:%M:%S')} 北京时间",
            received_at=timestamp.isoformat(),
            payload={
                "kind": "daily_audit",
                "default_branch": default_branch,
                "trigger": "admin-manual",
            },
        )
        actor_key = await enqueue_gitlab_event(event)
        results.append(
            {
                "project_id": project_id,
                "default_branch": default_branch,
                "branch_source": branch_source,
                "actor_key": actor_key,
                "event_id": event.event_id,
            }
        )

    return JSONResponse(
        {
            "status": "ok",
            "scheduled_count": len(results),
            "results": results,
        }
    )


@router.post("/admin/api/self-evolution/trigger")
async def admin_api_self_evolution_trigger(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()

    payload = await request.json()
    agent_type = str((payload or {}).get("agent_type") or "").strip()
    if agent_type not in {"mention", "auto_review", "daily_audit"}:
        return JSONResponse({"status": "invalid", "error": _t("未知的 agent_type。")}, status_code=400)

    snapshot = get_config_service().get_snapshot()
    target_projects = [str(item).strip() for item in snapshot.get("GITLAB_TARGET_PROJECTS", []) if str(item).strip()]
    if not target_projects:
        return JSONResponse({"status": "invalid", "error": _t("当前没有配置任何 GitLab Projects。")}, status_code=400)

    results: list[dict[str, str]] = []
    timestamp = now_in_open_review_tz()
    for project_id in target_projects:
        default_branch = "main"
        branch_source = "fallback"
        try:
            default_branch = await asyncio.to_thread(get_project_default_branch, project_id)
            branch_source = "gitlab"
        except Exception:
            branch_source = "fallback"
        event = EventEnvelope(
            event_id=f"admin-agent-self-evolution:{agent_type}:{project_id}:{timestamp.strftime('%Y%m%d%H%M%S%f')}",
            event_type="agent_self_evolution",
            project_id=project_id,
            mr_iid=None,
            source_branch=default_branch,
            target_branch=default_branch,
            title=f"Manual {agent_type} self evolution {timestamp.strftime('%Y-%m-%d %H:%M:%S')} 北京时间",
            received_at=timestamp.isoformat(),
            payload={
                "kind": "agent_self_evolution",
                "agent_type": agent_type,
                "default_branch": default_branch,
                "trigger": "admin-manual",
                "trigger_source": "admin-manual",
            },
        )
        actor_key = await enqueue_gitlab_event(event)
        get_config_service().record_self_evolution_manual_trigger(
            agent_type=agent_type,
            project_id=project_id,
            triggered_at=timestamp.isoformat(),
        )
        results.append(
            {
                "project_id": project_id,
                "default_branch": default_branch,
                "branch_source": branch_source,
                "actor_key": actor_key,
                "event_id": event.event_id,
                "agent_type": agent_type,
            }
        )

    return JSONResponse(
        {
            "status": "ok",
            "scheduled_count": len(results),
            "results": results,
            "agent_type": agent_type,
        }
    )


def _extract_model_ids(payload) -> list[str]:
    items = getattr(payload, "data", payload)
    model_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            value = item.get("id")
        else:
            value = getattr(item, "id", None)
        if value and str(value) not in seen:
            normalized = str(value)
            seen.add(normalized)
            model_ids.append(normalized)
    return model_ids


def _resolved_discovery_credentials(provider: str, *, base_url: str, api_key: str) -> tuple[str, str]:
    snapshot = get_config_service().get_snapshot()
    if provider == "openai":
        stored_api_key = str(snapshot.get("OPENAI_API_KEY", "")).strip()
    else:
        stored_api_key = str(snapshot.get("ANTHROPIC_API_KEY", "")).strip()

    resolved_api_key = api_key.strip() or stored_api_key
    resolved_base_url = base_url.strip() or PROVIDER_DEFAULT_BASE_URLS[provider]
    return resolved_base_url, resolved_api_key


def _build_llm_test_snapshot(
    provider: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
) -> dict:
    snapshot = dict(get_config_service().get_snapshot())
    snapshot["LLM_ACTIVE_PROVIDER"] = provider
    if provider == "openai":
        snapshot["OPENAI_BASE_URL"] = base_url.strip() or PROVIDER_DEFAULT_BASE_URLS[provider]
        snapshot["OPENAI_API_KEY"] = api_key.strip() or str(snapshot.get("OPENAI_API_KEY", "")).strip()
        snapshot["OPENAI_MODEL"] = model.strip() or str(snapshot.get("OPENAI_MODEL", "")).strip()
    else:
        snapshot["ANTHROPIC_BASE_URL"] = base_url.strip() or PROVIDER_DEFAULT_BASE_URLS[provider]
        snapshot["ANTHROPIC_API_KEY"] = api_key.strip() or str(snapshot.get("ANTHROPIC_API_KEY", "")).strip()
        snapshot["ANTHROPIC_MODEL"] = model.strip() or str(snapshot.get("ANTHROPIC_MODEL", "")).strip()
    return snapshot


def _build_model_discovery_error(provider: str, resolved_base_url: str, exc: Exception) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    is_404 = "404" in lowered or exc.__class__.__name__.lower() == "notfounderror"
    if (
        provider == "anthropic"
        and resolved_base_url != _DEFAULT_ANTHROPIC_BASE_URL
        and is_404
    ):
        return _t("当前 Anthropic 兼容端点不支持自动获取模型列表，请手动填写模型名；这不影响实际模型调用。")
    return f"{_t('拉取模型列表失败：')}{text}"


@router.post("/admin/api/llm/models")
async def admin_api_llm_models(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    payload = await request.json()
    provider = str(payload.get("provider", "")).strip().lower()
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()

    if provider not in PROVIDER_LABELS:
        return JSONResponse({"error": _t("不支持的 Provider。")}, status_code=400)

    resolved_base_url, resolved_api_key = _resolved_discovery_credentials(
        provider,
        base_url=base_url,
        api_key=api_key,
    )
    if not resolved_api_key:
        return JSONResponse({"error": _t("当前没有可用 API Key，请先填写或先保存 API Key。")}, status_code=400)

    try:
        if provider == "openai":
            client = OpenAI(base_url=resolved_base_url, api_key=resolved_api_key)
        else:
            client = Anthropic(base_url=resolved_base_url, api_key=resolved_api_key)
        models = _extract_model_ids(client.models.list())
        return JSONResponse({"provider": provider, "models": models})
    except Exception as exc:
        return JSONResponse(
            {
                "provider": provider,
                "models": [],
                "error": _build_model_discovery_error(provider, resolved_base_url, exc),
            },
            status_code=200,
        )


@router.post("/admin/api/llm/test")
async def admin_api_llm_test(request: Request):
    if not _is_authenticated(request):
        return _json_auth_error()
    payload = await request.json()
    provider = str(payload.get("provider", "")).strip().lower()
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    model = str(payload.get("model", "")).strip()

    if provider not in PROVIDER_LABELS:
        return JSONResponse({"error": _t("不支持的 Provider。")}, status_code=400)

    snapshot = _build_llm_test_snapshot(
        provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    resolved = resolve_llm_config(snapshot)
    if not resolved.api_key:
        return JSONResponse({"error": _t("当前没有可用 API Key，请先填写或先保存 API Key。")}, status_code=400)

    try:
        chat_model = make_model_from_snapshot(snapshot, temperature=0, max_tokens=400)
        response = await chat_model.ainvoke([HumanMessage(content=_LLM_CONNECTIVITY_TEST_PROMPT)])
        response_text = extract_model_response_text(response)
        return JSONResponse(
            {
                "provider": provider,
                "model_id": resolved.model_id,
                "response_text": response_text or f"({_t('模型已响应，但没有返回可展示的文本内容。')})",
            }
        )
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return JSONResponse(
            {
                "provider": provider,
                "model_id": resolved.model_id,
                "error": f"{_t('模型测试失败：')}{message}",
            },
            status_code=502,
        )
