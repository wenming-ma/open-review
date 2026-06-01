"""Shared asset path helpers for agent-scoped self-evolution."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from agent.selfevolution.repo import ensure_self_repo_checkout


def scene_repo_relative_root(agent_type: str) -> Path:
    return Path("agent") / "scenes" / str(agent_type or "").strip()


def local_scene_root(agent_type: str) -> Path:
    return Path(__file__).resolve().parents[1] / "scenes" / str(agent_type or "").strip()


def local_selfevolution_root(agent_type: str) -> Path:
    return local_scene_root(agent_type) / "selfevolution"


def repo_selfevolution_root(agent_type: str, repo_root: Path) -> Path:
    return Path(repo_root) / scene_repo_relative_root(agent_type) / "selfevolution"


def shared_skill_root(*, default_branch: str | None = None) -> Path:
    return ensure_self_repo_checkout(default_branch) / "agent" / "scenes" / "skills"


def shared_skill_source_roots(*, default_branch: str | None = None) -> list[tuple[str, Path]]:
    root = shared_skill_root(default_branch=default_branch)
    if not root.is_dir():
        return []
    roots: list[tuple[str, Path]] = []
    for collection_root in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
        if any(collection_root.glob("*/SKILL.md")):
            roots.append((collection_root.name, collection_root))
    return roots


def active_selfevolution_root(agent_type: str, *, default_branch: str | None = None) -> Path:
    return repo_selfevolution_root(agent_type, ensure_self_repo_checkout(default_branch))


def scene_skill_root(agent_type: str, *, default_branch: str | None = None) -> Path:
    return active_selfevolution_root(agent_type, default_branch=default_branch) / "skills"


def scene_prompt_root(agent_type: str, *, default_branch: str | None = None) -> Path:
    return active_selfevolution_root(agent_type, default_branch=default_branch) / "prompts"


def scene_tool_metadata_path(agent_type: str, *, default_branch: str | None = None) -> Path:
    return active_selfevolution_root(agent_type, default_branch=default_branch) / "tools" / "tool_descriptions.json"


def scene_code_targets_path(agent_type: str, *, default_branch: str | None = None) -> Path:
    return active_selfevolution_root(agent_type, default_branch=default_branch) / "code" / "code_targets.json"


def skill_source_roots(agent_type: str, *, default_branch: str | None = None) -> list[tuple[str, Path]]:
    roots = shared_skill_source_roots(default_branch=default_branch)
    scene_root = scene_skill_root(agent_type, default_branch=default_branch)
    if scene_root.exists():
        roots.append(("service_repo", scene_root))
    return roots


def safe_skill_source_name(source_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_name or "").strip("._-")
    return value or "skills"


def _sandbox_visible_path(sandbox: object, path: Path) -> str:
    host_root_value = getattr(sandbox, "host_root_dir", None)
    visible_root_value = getattr(sandbox, "root_dir", None) or getattr(sandbox, "cwd", None)
    if not host_root_value or not visible_root_value:
        return str(path)

    host_root = Path(os.fspath(host_root_value)).resolve()
    candidate = path.resolve(strict=False)
    try:
        relative = candidate.relative_to(host_root)
    except ValueError:
        return str(candidate)

    visible_root = os.fspath(visible_root_value).rstrip("/")
    if not relative.parts:
        return visible_root
    return f"{visible_root}/{relative.as_posix()}"


def _remove_existing_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def visible_skill_source_path(agent_type: str, sandbox: object, source_name: str, root: Path) -> str:
    candidate = root.resolve()
    host_root_value = getattr(sandbox, "host_root_dir", None)
    if not host_root_value:
        return str(candidate)

    host_root = Path(os.fspath(host_root_value)).resolve()
    try:
        candidate.relative_to(host_root)
    except ValueError:
        mirror_root = (
            host_root
            / "runtime"
            / safe_skill_source_name(agent_type)
            / "bundled-skills"
            / safe_skill_source_name(source_name)
        )
        _remove_existing_path(mirror_root)
        mirror_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate, mirror_root)
        return _sandbox_visible_path(sandbox, mirror_root)

    return _sandbox_visible_path(sandbox, candidate)


def visible_skill_source_roots(
    agent_type: str,
    sandbox: object,
    *,
    default_branch: str | None = None,
) -> list[str]:
    sources: list[str] = []
    for source_name, root in skill_source_roots(agent_type, default_branch=default_branch):
        if not root.is_dir():
            continue
        sources.append(visible_skill_source_path(agent_type, sandbox, source_name, root))
    return sources


def find_scene_skill_path(agent_type: str, skill_name: str, *, default_branch: str | None = None) -> Path:
    root = scene_skill_root(agent_type, default_branch=default_branch)
    for skill_md in root.rglob("SKILL.md"):
        if skill_md.parent.name == skill_name:
            return skill_md
    raise FileNotFoundError(f"Could not find {agent_type} skill '{skill_name}'")


def list_scene_skills(agent_type: str, *, default_branch: str | None = None) -> list[str]:
    root = scene_skill_root(agent_type, default_branch=default_branch)
    return sorted({path.parent.name for path in root.rglob("SKILL.md")})


def list_scene_prompt_targets(agent_type: str, *, default_branch: str | None = None) -> list[str]:
    root = scene_prompt_root(agent_type, default_branch=default_branch)
    return sorted(path.stem for path in root.glob("*.md"))


def load_scene_prompt_asset_text(agent_type: str, target_name: str, *, default_branch: str | None = None) -> str:
    path = scene_prompt_root(agent_type, default_branch=default_branch) / f"{target_name}.md"
    return path.read_text(encoding="utf-8")


def load_scene_tool_descriptions(agent_type: str, *, default_branch: str | None = None) -> dict[str, str]:
    return json.loads(scene_tool_metadata_path(agent_type, default_branch=default_branch).read_text(encoding="utf-8"))


def list_scene_tool_description_targets(agent_type: str, *, default_branch: str | None = None) -> list[str]:
    return sorted(load_scene_tool_descriptions(agent_type, default_branch=default_branch))


def list_scene_code_targets(agent_type: str, *, default_branch: str | None = None) -> list[str]:
    return json.loads(scene_code_targets_path(agent_type, default_branch=default_branch).read_text(encoding="utf-8"))
