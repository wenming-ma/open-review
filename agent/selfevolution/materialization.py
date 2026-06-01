"""On-demand GitLab materialization for self-evolution evaluation."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Any

from agent.config import settings
from agent.gitlab.mr_info import get_mr_metadata
from agent.utils.gitlab_project_targets import build_gitlab_project_clone_url


@dataclass(frozen=True)
class MaterializedTaskContext:
    temp_root: str
    repo_dir: str
    diff_text: str = ""
    identifier_summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _git_auth_env() -> dict[str, str]:
    token = settings.GITLAB_TOKEN
    env = os.environ.copy()
    if not token:
        return env
    import base64

    auth = base64.b64encode(f"oauth2:{token}".encode()).decode("ascii")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["SSH_ASKPASS"] = "/bin/false"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: Basic {auth}"
    return env


def _run_git(args: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_auth_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_identifier(example, key: str) -> str:
    metadata = getattr(example, "metadata", {}) or {}
    value = metadata.get(key)
    if value:
        return str(value)
    for item in getattr(example, "trigger_events", []) or []:
        if isinstance(item, dict) and item.get(key):
            return str(item.get(key))
    return ""


def _resolved_mr_identifiers(example) -> dict[str, str]:
    project_id = getattr(example, "project_id", "")
    mr_iid = _resolve_identifier(example, "mr_iid")
    data = {
        "project_id": str(project_id),
        "mr_iid": str(mr_iid or ""),
        "source_branch": _resolve_identifier(example, "source_branch"),
        "target_branch": _resolve_identifier(example, "target_branch"),
        "base_sha": _resolve_identifier(example, "base_sha"),
        "start_sha": _resolve_identifier(example, "start_sha"),
        "head_sha": _resolve_identifier(example, "head_sha"),
        "previous_review_head_sha": _resolve_identifier(example, "previous_review_head_sha"),
        "review_mode": _resolve_identifier(example, "review_mode"),
    }
    if data["project_id"] and data["mr_iid"] and not all(
        data[key] for key in ("source_branch", "target_branch", "base_sha", "start_sha", "head_sha")
    ):
        metadata = get_mr_metadata(data["project_id"], int(data["mr_iid"]))
        data["source_branch"] = data["source_branch"] or metadata.source_branch
        data["target_branch"] = data["target_branch"] or metadata.target_branch
        data["base_sha"] = data["base_sha"] or metadata.base_sha
        data["start_sha"] = data["start_sha"] or metadata.start_sha
        data["head_sha"] = data["head_sha"] or metadata.head_sha
    return data


def _clone_url(project_id: str) -> str:
    clone_url = build_gitlab_project_clone_url(project_id, external_url=settings.GITLAB_API_URL)
    if clone_url:
        return clone_url
    return f"{settings.GITLAB_API_URL.rstrip('/')}/{project_id}.git"


@contextmanager
def materialize_task_repository(example) -> Iterator[MaterializedTaskContext]:
    project_id = str(getattr(example, "project_id", "") or "")
    if not project_id:
        raise RuntimeError("missing_project_id")
    with tempfile.TemporaryDirectory(prefix=f"open-review-selfevolution-{getattr(example, 'agent_type', 'agent')}-") as temp_root:
        repo_dir = Path(temp_root) / "repo"
        clone = _run_git(["clone", "--no-checkout", "--quiet", _clone_url(project_id), str(repo_dir)])
        if clone.returncode != 0:
            raise RuntimeError(clone.stderr.strip() or clone.stdout.strip() or "git_clone_failed")

        metadata = dict(getattr(example, "metadata", {}) or {})
        notes: list[str] = []
        diff_text = ""
        head_sha = str(metadata.get("repo_head_sha") or "")
        default_branch = str(metadata.get("default_branch") or "")
        mr_iid = _resolve_identifier(example, "mr_iid")

        if mr_iid:
            ids = _resolved_mr_identifiers(example)
            refs = [ids.get("head_sha"), ids.get("base_sha"), ids.get("start_sha"), ids.get("previous_review_head_sha")]
            refs.extend([ids.get("source_branch"), ids.get("target_branch")])
            fetch_args = ["fetch", "--quiet", "origin", *[ref for ref in refs if ref]]
            fetch = _run_git(fetch_args, cwd=str(repo_dir))
            if fetch.returncode != 0:
                notes.append(fetch.stderr.strip() or fetch.stdout.strip() or "mr_fetch_failed")
            if ids.get("head_sha"):
                checkout = _run_git(["checkout", "--detach", ids["head_sha"]], cwd=str(repo_dir))
                if checkout.returncode != 0:
                    notes.append(checkout.stderr.strip() or checkout.stdout.strip() or "mr_checkout_failed")
            diff_base = ids.get("previous_review_head_sha") if ids.get("review_mode") == "incremental" and ids.get("previous_review_head_sha") else ids.get("start_sha") or ids.get("base_sha")
            if diff_base and ids.get("head_sha"):
                joiner = ".." if ids.get("review_mode") == "incremental" and ids.get("previous_review_head_sha") else "..."
                diff = _run_git(
                    ["diff", "--unified=3", "--find-renames", f"{diff_base}{joiner}{ids['head_sha']}"],
                    cwd=str(repo_dir),
                )
                if diff.returncode == 0:
                    diff_text = diff.stdout
                else:
                    notes.append(diff.stderr.strip() or diff.stdout.strip() or "mr_diff_failed")
            yield MaterializedTaskContext(
                temp_root=temp_root,
                repo_dir=str(repo_dir),
                diff_text=diff_text,
                identifier_summary={key: value for key, value in ids.items() if value},
                notes=notes,
            )
            return

        if head_sha:
            fetch = _run_git(["fetch", "--quiet", "origin", head_sha], cwd=str(repo_dir))
            if fetch.returncode != 0:
                notes.append(fetch.stderr.strip() or fetch.stdout.strip() or "repo_fetch_failed")
            checkout = _run_git(["checkout", "--detach", head_sha], cwd=str(repo_dir))
            if checkout.returncode != 0:
                notes.append(checkout.stderr.strip() or checkout.stdout.strip() or "repo_checkout_failed")
        elif default_branch:
            fetch = _run_git(["fetch", "--quiet", "origin", default_branch], cwd=str(repo_dir))
            if fetch.returncode != 0:
                notes.append(fetch.stderr.strip() or fetch.stdout.strip() or "branch_fetch_failed")
            checkout = _run_git(["checkout", "--detach", f"origin/{default_branch}"], cwd=str(repo_dir))
            if checkout.returncode != 0:
                notes.append(checkout.stderr.strip() or checkout.stdout.strip() or "branch_checkout_failed")
        yield MaterializedTaskContext(
            temp_root=temp_root,
            repo_dir=str(repo_dir),
            diff_text="",
            identifier_summary={
                "project_id": project_id,
                "default_branch": default_branch,
                "repo_head_sha": head_sha,
            },
            notes=notes,
        )
