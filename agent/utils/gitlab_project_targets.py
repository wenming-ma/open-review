"""Helpers for parsing and normalizing configured GitLab project targets."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse


def _clean_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _clean_base_url(value: str) -> tuple[str, tuple[str, ...]] | None:
    raw = _clean_url(value)
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    return parsed.netloc.lower(), segments


def _normalize_project_segments(segments: list[str]) -> str:
    parts = [segment.strip() for segment in segments if segment.strip()]
    if not parts:
        raise ValueError("GitLab 项目不能为空。")
    if parts[-1].endswith(".git"):
        suffix_stripped = parts[-1][: -len(".git")]
        if not suffix_stripped:
            raise ValueError("GitLab 项目不能为空。")
        parts[-1] = suffix_stripped
    if len(parts) < 2:
        raise ValueError("只支持 GitLab project path 或当前实例的 HTTPS 仓库 URL。")
    if "-" in parts:
        raise ValueError("只支持 GitLab 项目根 URL，不支持 MR、Issue 或 Wiki 链接。")
    return "/".join(parts)


def parse_gitlab_project_target(value: str, *, api_url: str, external_url: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("GitLab 项目不能为空。")

    if raw.startswith("git@"):
        raise ValueError("暂不支持 SSH 仓库地址；请使用当前 GitLab 实例的 HTTPS 地址。")

    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("只支持当前 GitLab 实例的 HTTPS 仓库 URL。")
        allowed_bases = [base for base in (_clean_base_url(api_url), _clean_base_url(external_url)) if base is not None]
        host = parsed.netloc.lower()
        segments = [segment for segment in parsed.path.split("/") if segment]
        matched_prefix: tuple[str, ...] | None = None
        for allowed_host, allowed_segments in allowed_bases:
            if host != allowed_host:
                continue
            prefix = tuple(segments[: len(allowed_segments)])
            if prefix == allowed_segments and (
                matched_prefix is None or len(allowed_segments) > len(matched_prefix)
            ):
                matched_prefix = allowed_segments
        if matched_prefix is None:
            raise ValueError("只支持当前 GitLab 实例的 HTTPS 仓库 URL。")
        project_segments = segments[len(matched_prefix) :]
        return _normalize_project_segments(project_segments)

    return _normalize_project_segments(raw.strip("/").split("/"))


def normalize_gitlab_project_targets(values: list[str] | tuple[str, ...], *, api_url: str, external_url: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        project_path = parse_gitlab_project_target(
            str(item),
            api_url=api_url,
            external_url=external_url,
        )
        if project_path in seen:
            continue
        seen.add(project_path)
        normalized.append(project_path)
    return normalized


def infer_gitlab_external_url(
    values: Sequence[str],
    *,
    current_external_url: str,
    current_api_url: str,
) -> str:
    current_candidates = [
        url for url in (_clean_url(current_external_url), _clean_url(current_api_url)) if url
    ]
    fallback = current_candidates[0] if current_candidates else ""
    candidates = [str(item).strip() for item in values if str(item).strip()]
    if not candidates:
        return fallback

    first = candidates[0]
    parsed = urlparse(first)
    if not (parsed.scheme and parsed.netloc):
        if fallback:
            return fallback
        raise ValueError("首次配置 GitLab 项目时，请填写当前 GitLab 实例的 HTTPS 仓库 URL。")

    path_segments = tuple(segment for segment in parsed.path.split("/") if segment)
    matched_prefix: tuple[str, ...] = ()
    for base in current_candidates:
        cleaned = _clean_base_url(base)
        if cleaned is None:
            continue
        _, allowed_segments = cleaned
        prefix = tuple(path_segments[: len(allowed_segments)])
        if prefix == allowed_segments and len(allowed_segments) > len(matched_prefix):
            matched_prefix = allowed_segments
    suffix = f"/{'/'.join(matched_prefix)}" if matched_prefix else ""
    return f"{parsed.scheme}://{parsed.netloc}{suffix}"


def build_gitlab_project_clone_url(project_path: str, *, external_url: str) -> str:
    base = _clean_url(external_url)
    project = str(project_path or "").strip().strip("/")
    if not base or not project:
        return project
    return f"{base}/{project}.git"


def build_gitlab_merge_request_url(
    project_path: str,
    mr_iid: int | str,
    *,
    external_url: str,
) -> str:
    base = _clean_url(external_url)
    project = str(project_path or "").strip().strip("/")
    mr = str(mr_iid or "").strip()
    if not base or not project or not mr:
        return ""
    return f"{base}/{project}/-/merge_requests/{mr}"
