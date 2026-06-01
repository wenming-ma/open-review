"""GitLab bot identity helpers derived from the configured token."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

from agent.controlplane import get_config_service
from agent.gitlab.client import get_gitlab_client
from agent.utils.timezone import iso_now

_CACHE_TTL_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()
_CACHE_RESOLUTION: BotIdentityResolution | None = None
_CACHE_EXPIRES_AT = 0.0
_BACKGROUND_PRIME_TASKS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class GitLabIdentity:
    username: str
    name: str
    avatar_url: str | None = None
    user_id: int | None = None
    state: str | None = None
    bot: bool | None = None


@dataclass(frozen=True)
class BotIdentityResolution:
    identity: GitLabIdentity | None
    source: Literal["live", "cached", "unavailable"]
    error: str | None = None
    fetched_at: str | None = None


def reset_gitlab_identity_cache() -> None:
    global _CACHE_RESOLUTION, _CACHE_EXPIRES_AT
    with _CACHE_LOCK:
        _CACHE_RESOLUTION = None
        _CACHE_EXPIRES_AT = 0.0


def _identity_from_payload(payload: dict) -> GitLabIdentity:
    username = str(payload.get("username") or "").strip()
    if not username:
        raise RuntimeError("GitLab 未返回当前 token 对应的用户名。")
    return GitLabIdentity(
        username=username,
        name=str(payload.get("name") or username),
        avatar_url=payload.get("avatar_url"),
        user_id=payload.get("user_id") or payload.get("id"),
        state=payload.get("state"),
        bot=payload.get("bot"),
    )


def _payload_from_identity(identity: GitLabIdentity, *, fetched_at: str) -> dict:
    return {
        "username": identity.username,
        "name": identity.name,
        "avatar_url": identity.avatar_url,
        "user_id": identity.user_id,
        "state": identity.state,
        "bot": identity.bot,
        "fetched_at": fetched_at,
    }


def _cache_resolution(resolution: BotIdentityResolution) -> BotIdentityResolution:
    global _CACHE_RESOLUTION, _CACHE_EXPIRES_AT
    with _CACHE_LOCK:
        _CACHE_RESOLUTION = resolution
        _CACHE_EXPIRES_AT = time.monotonic() + _CACHE_TTL_SECONDS
    return resolution


def _current_cached_resolution(now: float) -> BotIdentityResolution | None:
    with _CACHE_LOCK:
        if _CACHE_RESOLUTION is None or now >= _CACHE_EXPIRES_AT:
            return None
        return _CACHE_RESOLUTION


def _fetch_live_identity() -> GitLabIdentity:
    payload = get_gitlab_client().http_get("/user")
    return _identity_from_payload(payload)


def _load_persisted_identity() -> BotIdentityResolution | None:
    payload = get_config_service().get_cached_gitlab_identity()
    if not payload:
        return None
    try:
        identity = _identity_from_payload(payload)
    except Exception:
        return None
    return BotIdentityResolution(
        identity=identity,
        source="cached",
        fetched_at=payload.get("fetched_at"),
    )


def resolve_bot_identity(*, force_refresh: bool = False) -> BotIdentityResolution:
    now = time.monotonic()
    if not force_refresh:
        cached = _current_cached_resolution(now)
        if cached is not None:
            return cached

    try:
        identity = _fetch_live_identity()
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        persisted = _load_persisted_identity()
        if persisted is not None:
            return _cache_resolution(
                BotIdentityResolution(
                    identity=persisted.identity,
                    source="cached",
                    error=message,
                    fetched_at=persisted.fetched_at,
                )
            )
        return _cache_resolution(
            BotIdentityResolution(
                identity=None,
                source="unavailable",
                error=message,
            )
        )

    payload = _payload_from_identity(identity, fetched_at=iso_now())
    get_config_service().set_cached_gitlab_identity(payload)
    return _cache_resolution(
        BotIdentityResolution(
            identity=identity,
            source="live",
            fetched_at=payload["fetched_at"],
        )
    )


def get_bot_username(*, force_refresh: bool = False) -> str | None:
    resolved = resolve_bot_identity(force_refresh=force_refresh)
    return resolved.identity.username if resolved.identity else None


def is_bot_username(username: str | None, *, force_refresh: bool = False) -> bool:
    current = get_bot_username(force_refresh=force_refresh)
    return bool(current) and str(username or "").strip().lower() == current.lower()


def get_current_gitlab_identity(*, force_refresh: bool = False) -> GitLabIdentity:
    resolved = resolve_bot_identity(force_refresh=force_refresh)
    if resolved.identity is None:
        raise RuntimeError(resolved.error or "GitLab bot identity unavailable")
    return resolved.identity


def get_current_gitlab_identity_safe(*, force_refresh: bool = False) -> tuple[GitLabIdentity | None, str | None]:
    resolved = resolve_bot_identity(force_refresh=force_refresh)
    return resolved.identity, resolved.error


def prime_gitlab_identity_cache() -> BotIdentityResolution:
    return resolve_bot_identity(force_refresh=True)


def schedule_bot_identity_prime(*, logger: logging.Logger, context: str) -> asyncio.Task[None]:
    async def _runner() -> None:
        try:
            await asyncio.to_thread(prime_gitlab_identity_cache)
        except Exception:
            logger.warning(
                "Could not prime GitLab bot identity cache on %s",
                context,
                exc_info=True,
            )

    task = asyncio.create_task(_runner(), name=f"gitlab-identity-prime:{context}")
    _BACKGROUND_PRIME_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_PRIME_TASKS.discard)
    return task
