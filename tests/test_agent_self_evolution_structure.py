from __future__ import annotations

from pathlib import Path

from agent.scenes.auto_review.selfevolution import engine as auto_engine
from agent.scenes.mention.selfevolution import engine as mention_engine


def test_mention_selfevolution_skill_root_is_under_selfevolution():
    root = mention_engine._skill_root()

    assert root == Path(mention_engine.__file__).resolve().parents[1] / "selfevolution" / "skills"


def test_auto_review_selfevolution_skill_root_is_under_selfevolution():
    root = auto_engine._skill_root()

    assert root == Path(auto_engine.__file__).resolve().parents[1] / "selfevolution" / "skills"

