"""Tests for GitLab target parsing and normalization."""

from __future__ import annotations

import pytest

from agent.utils.gitlab_project_targets import (
    build_gitlab_merge_request_url,
    build_gitlab_project_clone_url,
    infer_gitlab_external_url,
    normalize_gitlab_project_targets,
    parse_gitlab_project_target,
)


def test_parse_gitlab_project_target_accepts_canonical_path():
    assert (
        parse_gitlab_project_target(
            "team/webapp",
            api_url="https://gitlab-api.example.com",
            external_url="https://gitlab.example.com",
        )
        == "team/webapp"
    )


def test_parse_gitlab_project_target_normalizes_gitlab_clone_url():
    assert (
        parse_gitlab_project_target(
            "https://gitlab.example.com/platform/core/service.git",
            api_url="https://gitlab-api.example.com",
            external_url="https://gitlab.example.com",
        )
        == "platform/core/service"
    )


def test_parse_gitlab_project_target_normalizes_gitlab_project_page_url():
    assert (
        parse_gitlab_project_target(
            "https://gitlab.example.com/platform/core/service/",
            api_url="https://gitlab-api.example.com",
            external_url="https://gitlab.example.com",
        )
        == "platform/core/service"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/wenming-ma/open-review.git",
        "git@gitlab.example.com:platform/core/service.git",
        "https://gitlab.other.example.com/platform/core/service.git",
        "https://gitlab.example.com/platform/core/service/-/merge_requests/1",
    ],
)
def test_parse_gitlab_project_target_rejects_unsupported_inputs(value: str):
    with pytest.raises(ValueError):
        parse_gitlab_project_target(
            value,
            api_url="https://gitlab-api.example.com",
            external_url="https://gitlab.example.com",
        )


def test_normalize_gitlab_project_targets_dedupes_urls_and_paths():
    assert normalize_gitlab_project_targets(
        [
            " platform/core/service ",
            "https://gitlab.example.com/platform/core/service.git",
            "https://gitlab.example.com/team/webapp/",
        ],
        api_url="https://gitlab-api.example.com",
        external_url="https://gitlab.example.com",
    ) == ["platform/core/service", "team/webapp"]


def test_infer_gitlab_external_url_from_first_repo_url():
    assert (
        infer_gitlab_external_url(
            ["https://gitlab.example.com/team/service.git"],
            current_external_url="",
            current_api_url="",
        )
        == "https://gitlab.example.com"
    )


def test_infer_gitlab_external_url_falls_back_to_existing_base_for_path_inputs():
    assert (
        infer_gitlab_external_url(
            ["team/service"],
            current_external_url="https://gitlab.example.com",
            current_api_url="https://gitlab-api.example.com",
        )
        == "https://gitlab.example.com"
    )


def test_infer_gitlab_external_url_updates_existing_repo_url_host():
    assert (
        infer_gitlab_external_url(
            ["https://gitlab-new.example.com/team/service.git"],
            current_external_url="https://gitlab-old.example.com",
            current_api_url="https://gitlab-old.example.com",
        )
        == "https://gitlab-new.example.com"
    )


def test_build_gitlab_project_clone_url_uses_external_url():
    assert (
        build_gitlab_project_clone_url(
            "team/service",
            external_url="https://gitlab.example.com",
        )
        == "https://gitlab.example.com/team/service.git"
    )


def test_build_gitlab_merge_request_url_uses_external_url():
    assert (
        build_gitlab_merge_request_url(
            "team/service",
            44,
            external_url="https://gitlab.example.com",
        )
        == "https://gitlab.example.com/team/service/-/merge_requests/44"
    )
