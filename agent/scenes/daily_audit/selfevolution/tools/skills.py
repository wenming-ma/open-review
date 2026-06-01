"""File-backed skill tools for daily audit agents."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore
from agent.scenes.daily_audit.selfevolution.repo import (
    daily_audit_self_skill_root,
    ensure_daily_audit_self_repo_checkout,
)


def _self_repo_skill_root(default_branch: str | None = None) -> Path:
    repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
    return daily_audit_self_skill_root(repo_root)


def _shared_skill_root(default_branch: str | None = None) -> Path:
    repo_root = ensure_daily_audit_self_repo_checkout(default_branch)
    return Path(repo_root) / "agent" / "scenes" / "skills"


def _shared_skill_source_roots(default_branch: str | None = None) -> list[tuple[str, Path]]:
    root = _shared_skill_root(default_branch=default_branch)
    if not root.is_dir():
        return []
    roots: list[tuple[str, Path]] = []
    for collection_root in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
        if any(collection_root.glob("*/SKILL.md")):
            roots.append((collection_root.name, collection_root))
    return roots


def _skill_source_roots(repo_dir: str, *, default_branch: str | None = None) -> list[tuple[str, Path]]:
    roots = _shared_skill_source_roots(default_branch=default_branch)
    root = _self_repo_skill_root(default_branch)
    roots.append(("self_repo", root))
    del repo_dir
    return roots


def _self_repo_writable_root(repo_dir: str, *, default_branch: str | None = None) -> Path | None:
    for source_name, root in _skill_source_roots(repo_dir, default_branch=default_branch):
        if source_name == "self_repo":
            return root
    return None


def _self_repo_source_roots(repo_dir: str, *, default_branch: str | None = None) -> list[tuple[str, Path]]:
    return [
        (source_name, root)
        for source_name, root in _skill_source_roots(repo_dir, default_branch=default_branch)
        if source_name == "self_repo"
    ]


def _collect_skills(repo_dir: str, *, default_branch: str | None = None) -> list[dict]:
    skills: dict[str, dict] = {}
    for source_name, root in _self_repo_source_roots(repo_dir, default_branch=default_branch):
        for skill_md in root.rglob("SKILL.md"):
            text = skill_md.read_text(encoding="utf-8")
            name, description = DailyAuditPersistenceStore._parse_skill_metadata(text)
            skill_name = name or skill_md.parent.name
            skills[skill_name] = {
                "name": skill_name,
                "description": description,
                "content": text,
                "path": str(skill_md),
                "root": str(root),
                "source": source_name,
            }
    return sorted(skills.values(), key=lambda item: item["name"])


def _find_skill(repo_dir: str, name: str, *, default_branch: str | None = None) -> dict | None:
    for item in _collect_skills(repo_dir, default_branch=default_branch):
        if item["name"] == name:
            return item
    return None


def _find_self_repo_skill(repo_dir: str, name: str, *, default_branch: str | None = None) -> dict | None:
    for source_name, root in _skill_source_roots(repo_dir, default_branch=default_branch):
        if source_name != "self_repo":
            continue
        skill_md = root / name / "SKILL.md"
        if not skill_md.exists():
            return None
        text = skill_md.read_text(encoding="utf-8")
        parsed_name, description = DailyAuditPersistenceStore._parse_skill_metadata(text)
        return {
            "name": parsed_name or name,
            "description": description,
            "content": text,
            "path": str(skill_md),
            "root": str(root),
            "source": source_name,
        }
    return None


def list_skill_descriptors(*, repo_dir: str, default_branch: str | None = None) -> list[dict]:
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "source": item["source"],
            "writable": item["source"] == "self_repo",
        }
        for item in _collect_skills(repo_dir, default_branch=default_branch)
    ]


def build_skill_tools(
    *,
    repo_dir: str,
    default_branch: str | None = None,
    on_write: Callable[[str, str, str], None] | None = None,
):
    def _emit_write(
        callback: Callable[[str, str, str], None] | None,
        *,
        action: str,
        name: str,
        content: str,
    ) -> None:
        if callback is not None:
            callback(action, name, content)

    def skills_list() -> dict[str, object]:
        """List available daily-audit skills from file-backed sources."""

        return {"success": True, "skills": list_skill_descriptors(repo_dir=repo_dir, default_branch=default_branch)}

    def skill_view(name: str, file_path: str | None = None) -> dict[str, object]:
        """Load the current content of a daily-audit skill."""

        item = _find_skill(repo_dir, name, default_branch=default_branch)
        if item is None:
            return {"success": False, "error": f"Skill '{name}' not found"}
        if not file_path:
            return {
                "success": True,
                "name": item["name"],
                "content": item["content"],
                "source": item["source"],
                "linked_files": {},
            }
        target = Path(item["path"]).parent / file_path
        if not target.exists() or not target.is_file():
            return {"success": False, "error": f"File '{file_path}' not found for skill '{name}'"}
        return {
            "success": True,
            "name": item["name"],
            "content": target.read_text(encoding="utf-8"),
            "source": item["source"],
            "file_path": file_path,
        }

    def skill_manage(
        action: str,
        name: str,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
    ) -> dict[str, object]:
        """Create or patch reusable daily-audit skills.

        Actions: `create`, `edit`, `patch`, `delete`.
        """

        normalized = action.strip().lower()
        existing_self = _find_self_repo_skill(repo_dir, name, default_branch=default_branch)
        self_root = _self_repo_writable_root(repo_dir, default_branch=default_branch)
        if self_root is None:
            return {"success": False, "error": "self_repo_not_configured"}
        self_root.mkdir(parents=True, exist_ok=True)
        if normalized in {"create", "edit"}:
            body = (content or "").strip()
            if not body:
                return {"success": False, "error": "content is required"}
            resolved_name, _description = DailyAuditPersistenceStore._parse_skill_metadata(body)
            if resolved_name and resolved_name != name:
                return {"success": False, "error": f"frontmatter name '{resolved_name}' must match '{name}'"}
            skill_dir = self_root / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(body.rstrip() + "\n", encoding="utf-8")
            _emit_write(on_write, action=normalized, name=name, content=body)
            return {"success": True, "message": f"Skill '{name}' saved.", "name": name, "path": str(skill_path)}
        if normalized == "patch":
            if existing_self is None:
                return {"success": False, "error": f"Skill '{name}' not found"}
            if not old_string:
                return {"success": False, "error": "old_string is required"}
            if new_string is None:
                return {"success": False, "error": "new_string is required"}
            current = str(existing_self["content"])
            if old_string not in current:
                return {"success": False, "error": "old_string not found in skill content"}
            updated = current.replace(old_string, new_string, 1)
            skill_path = Path(existing_self["path"])
            skill_path.write_text(updated.rstrip() + "\n", encoding="utf-8")
            _emit_write(on_write, action="patch", name=name, content=updated)
            return {"success": True, "message": f"Skill '{name}' patched.", "name": name, "path": str(skill_path)}
        if normalized == "delete":
            if existing_self is None:
                return {"success": False, "message": f"Skill '{name}' not found."}
            skill_dir = Path(existing_self["path"]).parent
            shutil.rmtree(skill_dir)
            _emit_write(on_write, action="delete", name=name, content="")
            return {"success": True, "message": f"Skill '{name}' deleted.", "name": name}
        return {"success": False, "error": f"Unknown action '{action}'"}

    skills_list.__name__ = "skills_list"
    skill_view.__name__ = "skill_view"
    skill_manage.__name__ = "skill_manage"
    return skills_list, skill_view, skill_manage
