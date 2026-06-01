"""Deployment-time cleanup for historical MR sandboxes."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent.config import settings
from agent.controlplane import get_tracking_service
from agent.gitlab.client import get_mr
from agent.runtime.queue import get_runtime_store
from agent.sandbox.manager import cleanup_sandbox
from agent.utils.thread_id import generate_thread_id

logger = logging.getLogger(__name__)

TERMINAL_MR_STATES = {"closed", "merged"}


@dataclass(frozen=True)
class MRCandidate:
    project_id: str
    mr_iid: int
    thread_id: str


@dataclass(frozen=True)
class ActorActivity:
    pending_count: int
    inflight_count: int
    leased: bool

    @property
    def active(self) -> bool:
        return self.pending_count > 0 or self.inflight_count > 0 or self.leased


@dataclass
class CleanupResult:
    scanned: int = 0
    cleaned: int = 0
    dry_run_cleanable: int = 0
    skipped_unknown: int = 0
    skipped_active: int = 0
    skipped_open: int = 0
    skipped_error: int = 0


def _sandbox_thread_ids(sandbox_root: str) -> list[str]:
    root = Path(sandbox_root)
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if item.is_dir())


def _candidate_mrs_from_tracking(tracking, limit: int) -> dict[str, MRCandidate]:
    candidates: dict[str, MRCandidate] = {}
    for run in tracking.list_runs(limit=limit):
        project_id = str(run.get("project_id") or "").strip()
        mr_iid = run.get("mr_iid")
        if not project_id or mr_iid is None:
            continue
        try:
            mr_iid_int = int(mr_iid)
        except (TypeError, ValueError):
            continue
        thread_id = generate_thread_id(project_id, mr_iid_int)
        candidates[thread_id] = MRCandidate(project_id, mr_iid_int, thread_id)
    return candidates


async def _actor_activity_async(store) -> dict[str, ActorActivity]:
    activity: dict[str, ActorActivity] = {}
    for status in await store.list_actor_statuses():
        activity[status.actor_key] = ActorActivity(
            pending_count=int(status.pending_count or 0),
            inflight_count=int(status.inflight_count or 0),
            leased=bool(status.lease_owner),
        )
    return activity


def _actor_activity(store) -> dict[str, ActorActivity]:
    import asyncio

    return asyncio.run(_actor_activity_async(store))


def _mr_state(project_id: str, mr_iid: int) -> str:
    mr = get_mr(project_id, mr_iid)
    return str(getattr(mr, "state", "") or "").strip().lower()


def cleanup_historical_mr_sandboxes(
    *,
    sandbox_root: str | None = None,
    tracking=None,
    store=None,
    limit: int = 5000,
    dry_run: bool = False,
) -> CleanupResult:
    """Remove local MR sandboxes whose GitLab MRs are already closed or merged."""
    sandbox_root = sandbox_root or settings.LOCAL_SANDBOX_ROOT_DIR
    tracking = tracking or get_tracking_service()
    if store is None:
        import asyncio

        store = asyncio.run(get_runtime_store())

    result = CleanupResult()
    candidates = _candidate_mrs_from_tracking(tracking, limit)
    activity = _actor_activity(store)

    for thread_id in _sandbox_thread_ids(sandbox_root):
        result.scanned += 1
        candidate = candidates.get(thread_id)
        if candidate is None:
            result.skipped_unknown += 1
            continue

        actor_key = f"{candidate.project_id}!{candidate.mr_iid}"
        actor_activity = activity.get(actor_key)
        if actor_activity is not None and actor_activity.active:
            result.skipped_active += 1
            continue

        try:
            state = _mr_state(candidate.project_id, candidate.mr_iid)
        except Exception:
            logger.warning(
                "Could not fetch MR state for %s!%s during deployment sandbox cleanup",
                candidate.project_id,
                candidate.mr_iid,
                exc_info=True,
            )
            result.skipped_error += 1
            continue

        if state not in TERMINAL_MR_STATES:
            result.skipped_open += 1
            continue

        if dry_run:
            result.dry_run_cleanable += 1
            continue

        cleanup_sandbox(thread_id)
        result.cleaned += 1

    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean historical Open Review MR sandboxes for closed/merged MRs.")
    parser.add_argument("--sandbox-root", default=None)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _format_result(result: CleanupResult) -> str:
    return (
        "historical sandbox cleanup: "
        f"scanned={result.scanned} cleaned={result.cleaned} "
        f"dry_run_cleanable={result.dry_run_cleanable} "
        f"skipped_unknown={result.skipped_unknown} skipped_active={result.skipped_active} "
        f"skipped_open={result.skipped_open} skipped_error={result.skipped_error}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("OPEN_REVIEW_LOG_LEVEL", "INFO"))
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    result = cleanup_historical_mr_sandboxes(
        sandbox_root=args.sandbox_root,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(_format_result(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
