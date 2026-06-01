"""Search and history tools for daily audit agents."""

from __future__ import annotations

import json

from agent.scenes.daily_audit.persistence.store import DailyAuditPersistenceStore


def build_session_search_tool(
    *,
    project_id: str,
    session_id: str,
    store: DailyAuditPersistenceStore,
):
    def session_search(query: str, limit: int = 3) -> dict[str, object]:
        """Search prior daily-audit sessions and return compact per-session summaries."""

        terms = query.strip()
        if not terms:
            return {"success": False, "error": "query is required"}
        matches: dict[str, list[str]] = {}
        for row in store.search_run_transcripts(project_id, terms, limit=limit * 4):
            run_id = str(row.get("run_id") or "")
            if not run_id or session_id.endswith(f":{run_id}:primary"):
                continue
            matches.setdefault(run_id, []).append(str(row.get("content") or "").strip())

        results = []
        for run_id, snippets in list(matches.items())[:limit]:
            summary = "\n".join(item for item in snippets if item)[:1200]
            results.append({"run_id": run_id, "summary": summary})
        return {"success": True, "query": query, "results": results, "count": len(results)}

    session_search.__name__ = "session_search"
    return session_search


def build_direction_history_tool(
    *,
    project_id: str,
    run_id: str,
    store: DailyAuditPersistenceStore,
):
    def direction_history(query: str | None = None, limit: int = 5) -> dict[str, object]:
        """List or search previously audited direction choices to avoid repeating the same workflow."""

        normalized_limit = max(1, min(int(limit), 10))
        if query and query.strip():
            rows = store.search_direction_archives(
                project_id,
                query.strip(),
                limit=normalized_limit,
                exclude_run_id=run_id,
            )
        else:
            rows = store.list_recent_direction_archives(
                project_id,
                limit=normalized_limit,
                exclude_run_id=run_id,
            )
        results = []
        for row in rows:
            results.append(
                {
                    "run_id": row["run_id"],
                    "unit_type": row["unit_type"],
                    "unit_label": row["unit_label"],
                    "file_path": row["file_path"],
                    "entrypoint_kind": row["entrypoint_kind"],
                    "entrypoint_symbol": row["entrypoint_symbol"],
                    "workflow_summary": row["workflow_summary"],
                    "direction_brief": row["direction_brief"],
                    "keywords": list(json.loads(str(row["keywords_json"]) or "[]")),
                }
            )
        return {"success": True, "query": query or "", "results": results, "count": len(results)}

    direction_history.__name__ = "direction_history"
    return direction_history
