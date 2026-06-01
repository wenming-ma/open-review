"""Read-only exploration memory tools for direction discovery."""

from __future__ import annotations

from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore


def build_exploration_memory_tool(
    *,
    project_id: str,
    store: DailyAuditPersistenceStore,
):
    def exploration_memory(query: str | None = None, limit: int = 5) -> dict[str, object]:
        """Read short-term and long-term memory for exploration-time overlap checks."""

        normalized_limit = max(1, min(int(limit), 10))
        summary = store.get_short_term_summary(project_id, "primary")
        if query and query.strip():
            long_term_rows = store.search_long_term_memory(project_id, query.strip(), limit=normalized_limit)
        else:
            long_term_rows = store.list_long_term_memory(project_id, limit=normalized_limit)
        results = [
            {
                "memory_type": row["memory_type"],
                "content": row["content"],
                "source_run_id": row["source_run_id"],
                "updated_at": row["updated_at"],
            }
            for row in long_term_rows
        ]
        return {
            "success": True,
            "query": (query or "").strip(),
            "short_term_summary": summary,
            "results": results,
            "count": len(results),
        }

    exploration_memory.__name__ = "exploration_memory"
    return exploration_memory
