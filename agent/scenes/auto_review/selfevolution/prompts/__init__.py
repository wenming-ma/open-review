"""Prompt assets for auto-review self-evolution."""

from __future__ import annotations

from agent.selfevolution.assets import load_scene_prompt_asset_text


def load_prompt_asset_text(target_name: str, *, default_branch: str | None = None) -> str:
    return load_scene_prompt_asset_text("auto_review", target_name, default_branch=default_branch)
