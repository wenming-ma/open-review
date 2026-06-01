from __future__ import annotations


def test_daily_audit_scene_local_layout_exports_middleware_and_persistence_modules():
    from agent.scenes.daily_audit.middleware.session_lifecycle import DailyAuditSessionMiddleware
    from agent.scenes.daily_audit.persistence.direction import run_daily_audit_direction_persistence
    from agent.scenes.daily_audit.persistence.long_term import run_daily_audit_long_term_persistence
    from agent.scenes.daily_audit.persistence.short_term import (
        run_daily_audit_short_term_persistence,
    )
    from agent.scenes.daily_audit.persistence.skill import run_daily_audit_skill_persistence
    from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore
    from agent.scenes.daily_audit.runtime.backends import DailyAuditBackend
    from agent.scenes.daily_audit.runtime.deepagents import (
        reset_daily_audit_deepagents_runtime,
    )
    from agent.scenes.daily_audit.selfevolution.engine import (
        maybe_run_daily_audit_self_evolution,
    )
    from agent.scenes.daily_audit.selfevolution.evaluation import DailyAuditEvalExample
    from agent.scenes.daily_audit.selfevolution.paths import (
        local_daily_audit_selfevolution_root,
    )
    from agent.scenes.daily_audit.selfevolution.repo import daily_audit_self_repo_root

    assert DailyAuditSessionMiddleware is not None
    assert DailyAuditPersistenceStore is not None
    assert run_daily_audit_direction_persistence is not None
    assert run_daily_audit_short_term_persistence is not None
    assert run_daily_audit_long_term_persistence is not None
    assert run_daily_audit_skill_persistence is not None
    assert DailyAuditBackend is not None
    assert reset_daily_audit_deepagents_runtime is not None
    assert DailyAuditEvalExample is not None
    assert maybe_run_daily_audit_self_evolution is not None
    assert local_daily_audit_selfevolution_root is not None
    assert daily_audit_self_repo_root is not None
