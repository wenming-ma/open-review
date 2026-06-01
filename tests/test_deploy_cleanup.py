"""Tests for deployment-time sandbox cleanup."""

from __future__ import annotations

from types import SimpleNamespace

from agent.maintenance import sandbox_cleanup


def test_cleanup_historical_mr_sandboxes_removes_only_inactive_closed_or_merged_mrs(tmp_path, monkeypatch):
    closed_thread = "closed-thread"
    opened_thread = "opened-thread"
    active_thread = "active-thread"
    unknown_thread = "unknown-thread"
    for thread_id in (closed_thread, opened_thread, active_thread, unknown_thread):
        (tmp_path / thread_id / "repo").mkdir(parents=True)

    monkeypatch.setattr(
        sandbox_cleanup,
        "_sandbox_thread_ids",
        lambda _root: [closed_thread, opened_thread, active_thread, unknown_thread],
    )
    monkeypatch.setattr(
        sandbox_cleanup,
        "_candidate_mrs_from_tracking",
        lambda _tracking, _limit: {
            closed_thread: sandbox_cleanup.MRCandidate("root/kicad", 11, closed_thread),
            opened_thread: sandbox_cleanup.MRCandidate("root/kicad", 12, opened_thread),
            active_thread: sandbox_cleanup.MRCandidate("root/kicad", 13, active_thread),
        },
    )
    monkeypatch.setattr(
        sandbox_cleanup,
        "_actor_activity",
        lambda _store: {
            "root/kicad!13": sandbox_cleanup.ActorActivity(
                pending_count=1,
                inflight_count=0,
                leased=False,
            )
        },
    )
    states = {11: "closed", 12: "opened", 13: "merged"}
    monkeypatch.setattr(sandbox_cleanup, "_mr_state", lambda project_id, mr_iid: states[mr_iid])
    cleaned: list[str] = []
    monkeypatch.setattr(sandbox_cleanup, "cleanup_sandbox", lambda thread_id: cleaned.append(thread_id))

    result = sandbox_cleanup.cleanup_historical_mr_sandboxes(
        sandbox_root=str(tmp_path),
        tracking=SimpleNamespace(),
        store=SimpleNamespace(),
        limit=100,
    )

    assert cleaned == [closed_thread]
    assert result.scanned == 4
    assert result.cleaned == 1
    assert result.skipped_open == 1
    assert result.skipped_active == 1
    assert result.skipped_unknown == 1


def test_cleanup_historical_mr_sandboxes_dry_run_does_not_delete(tmp_path, monkeypatch):
    thread_id = "merged-thread"
    (tmp_path / thread_id / "repo").mkdir(parents=True)
    monkeypatch.setattr(sandbox_cleanup, "_sandbox_thread_ids", lambda _root: [thread_id])
    monkeypatch.setattr(
        sandbox_cleanup,
        "_candidate_mrs_from_tracking",
        lambda _tracking, _limit: {
            thread_id: sandbox_cleanup.MRCandidate("root/kicad", 21, thread_id),
        },
    )
    monkeypatch.setattr(sandbox_cleanup, "_actor_activity", lambda _store: {})
    monkeypatch.setattr(sandbox_cleanup, "_mr_state", lambda project_id, mr_iid: "merged")
    cleaned: list[str] = []
    monkeypatch.setattr(sandbox_cleanup, "cleanup_sandbox", lambda thread_id: cleaned.append(thread_id))

    result = sandbox_cleanup.cleanup_historical_mr_sandboxes(
        sandbox_root=str(tmp_path),
        tracking=SimpleNamespace(),
        store=SimpleNamespace(),
        dry_run=True,
    )

    assert cleaned == []
    assert result.cleaned == 0
    assert result.dry_run_cleanable == 1
