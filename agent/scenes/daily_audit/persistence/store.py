"""Persistence store for the daily audit workflow."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import yaml

from agent.config import ensure_writable_directory, settings
from agent.scenes.daily_audit.persistence.raw_records import (
    find_daily_audit_record,
    iter_daily_audit_agent_records,
    render_messages_transcript,
    transcript_search_matches,
)
from agent.utils.timezone import iso_now


def _now() -> str:
    return iso_now()


def _fts_query(value: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", value)
    return " ".join(terms[:8]) or "daily"


class DailyAuditPersistenceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        ensure_writable_directory(self.db_path.parent)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_audit_short_term (
                    project_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, role)
                );
                CREATE TABLE IF NOT EXISTS daily_audit_long_term (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_run_id TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, memory_type, content)
                );
                CREATE TABLE IF NOT EXISTS daily_audit_direction_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    unit_type TEXT NOT NULL,
                    unit_label TEXT NOT NULL,
                    file_path TEXT,
                    entrypoint_kind TEXT,
                    entrypoint_symbol TEXT,
                    workflow_summary TEXT NOT NULL,
                    selection_reasoning TEXT NOT NULL,
                    direction_brief TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, run_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS daily_audit_direction_archives_fts USING fts5(
                    project_id UNINDEXED,
                    run_id UNINDEXED,
                    unit_label UNINDEXED,
                    file_path UNINDEXED,
                    entrypoint_symbol UNINDEXED,
                    content
                );
                """
            )
            conn.executescript(
                """
                DROP TABLE IF EXISTS daily_audit_run_transcript_chunks_fts;
                DROP TABLE IF EXISTS daily_audit_run_transcript_chunks;
                DROP TABLE IF EXISTS daily_audit_run_transcripts;
                DROP TABLE IF EXISTS daily_audit_history_fts;
                DROP TABLE IF EXISTS daily_audit_history;
                DROP TABLE IF EXISTS daily_audit_recall_documents_fts;
                DROP TABLE IF EXISTS daily_audit_recall_documents;
                DROP TABLE IF EXISTS daily_audit_precompact_insights_fts;
                DROP TABLE IF EXISTS daily_audit_precompact_insights;
                DROP TABLE IF EXISTS daily_audit_builtin_memory;
                DROP TABLE IF EXISTS daily_audit_runtime_skills;
                DROP TABLE IF EXISTS daily_audit_evolution_lineage;
                """
            )

    @staticmethod
    def _parse_skill_metadata(content: str) -> tuple[str, str]:
        text = content.strip()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    data = yaml.safe_load(parts[1]) or {}
                except Exception:
                    data = {}
                name = str(data.get("name") or "").strip()
                description = str(data.get("description") or "").strip()
                return name, description
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0].lstrip("# ").strip() if lines else ""
        return title or "", ""

    def upsert_short_term_summary(self, project_id: str, role: str, summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_audit_short_term(project_id, role, summary, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, role) DO UPDATE SET
                    summary=excluded.summary,
                    updated_at=excluded.updated_at
                """,
                (project_id, role, summary, _now()),
            )

    def get_short_term_summary(self, project_id: str, role: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary FROM daily_audit_short_term WHERE project_id = ? AND role = ?",
                (project_id, role),
            ).fetchone()
        return str(row["summary"]) if row else ""

    def add_long_term_memory(
        self,
        project_id: str,
        *,
        memory_type: str,
        content: str,
        source_run_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO daily_audit_long_term(
                    project_id, memory_type, content, source_run_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, memory_type, content, source_run_id, _now()),
            )

    def list_long_term_memory(self, project_id: str, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_type, content, source_run_id, updated_at
                FROM daily_audit_long_term
                WHERE project_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_long_term_memory(self, project_id: str, query: str, limit: int = 5) -> list[dict]:
        terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_type, content, source_run_id, updated_at
                FROM daily_audit_long_term
                WHERE project_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 100
                """,
                (project_id,),
            ).fetchall()
        if not terms:
            return [dict(row) for row in rows[:limit]]
        matches = [
            dict(row)
            for row in rows
            if all(term in str(row["content"]).lower() for term in terms)
        ]
        return matches[:limit]

    def record_direction_archive(
        self,
        project_id: str,
        *,
        run_id: str,
        unit_type: str,
        unit_label: str,
        file_path: str | None,
        entrypoint_kind: str | None,
        entrypoint_symbol: str | None,
        workflow_summary: str,
        selection_reasoning: str,
        direction_brief: str,
        keywords: list[str],
        metadata: dict | None = None,
    ) -> None:
        direction_brief = direction_brief.strip()
        if not direction_brief:
            return
        created_at = _now()
        keywords_json = json.dumps([item for item in keywords if str(item).strip()], ensure_ascii=True)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=True)
        fts_content = "\n".join(
            part
            for part in (
                unit_label.strip(),
                str(file_path or "").strip(),
                str(entrypoint_symbol or "").strip(),
                workflow_summary.strip(),
                selection_reasoning.strip(),
                direction_brief,
                " ".join(json.loads(keywords_json)),
            )
            if part
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM daily_audit_direction_archives WHERE project_id = ? AND run_id = ?",
                (project_id, run_id),
            ).fetchone()
            if existing is not None:
                conn.execute("DELETE FROM daily_audit_direction_archives_fts WHERE rowid = ?", (existing["id"],))
                conn.execute("DELETE FROM daily_audit_direction_archives WHERE id = ?", (existing["id"],))
            cursor = conn.execute(
                """
                INSERT INTO daily_audit_direction_archives(
                    project_id, run_id, unit_type, unit_label, file_path, entrypoint_kind,
                    entrypoint_symbol, workflow_summary, selection_reasoning, direction_brief,
                    keywords_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    run_id,
                    unit_type,
                    unit_label,
                    file_path,
                    entrypoint_kind,
                    entrypoint_symbol,
                    workflow_summary,
                    selection_reasoning,
                    direction_brief,
                    keywords_json,
                    metadata_json,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_audit_direction_archives_fts(
                    rowid, project_id, run_id, unit_label, file_path, entrypoint_symbol, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    project_id,
                    run_id,
                    unit_label,
                    file_path,
                    entrypoint_symbol,
                    fts_content,
                ),
            )

    def list_recent_direction_archives(
        self,
        project_id: str,
        limit: int = 5,
        *,
        exclude_run_id: str | None = None,
    ) -> list[dict]:
        query = """
            SELECT run_id, unit_type, unit_label, file_path, entrypoint_kind, entrypoint_symbol,
                   workflow_summary, selection_reasoning, direction_brief, keywords_json, metadata_json, created_at
            FROM daily_audit_direction_archives
            WHERE project_id = ?
        """
        params: list[object] = [project_id]
        if exclude_run_id:
            query += " AND run_id != ?"
            params.append(exclude_run_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def search_direction_archives(
        self,
        project_id: str,
        query: str,
        limit: int = 5,
        *,
        exclude_run_id: str | None = None,
    ) -> list[dict]:
        sql = """
            SELECT d.run_id, d.unit_type, d.unit_label, d.file_path, d.entrypoint_kind, d.entrypoint_symbol,
                   d.workflow_summary, d.selection_reasoning, d.direction_brief, d.keywords_json,
                   d.metadata_json, d.created_at
            FROM daily_audit_direction_archives_fts f
            JOIN daily_audit_direction_archives d ON d.id = f.rowid
            WHERE f.project_id = ? AND f.content MATCH ?
        """
        params: list[object] = [project_id, _fts_query(query)]
        if exclude_run_id:
            sql += " AND d.run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY d.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def search_run_transcripts(
        self,
        project_id: str,
        query: str,
        limit: int = 5,
        *,
        exclude_run_id: str | None = None,
    ) -> list[dict]:
        raw_results: list[dict] = []
        for record in iter_daily_audit_agent_records(project_id, record_kind="daily_audit.analysis"):
            metadata = record.get("metadata_json") or {}
            logical_run_id = str(metadata.get("logical_run_id") or record.get("_runtime_run_id") or "").strip()
            if not logical_run_id:
                continue
            if exclude_run_id and logical_run_id == exclude_run_id:
                continue
            messages = record.get("messages_json")
            if not isinstance(messages, list):
                continue
            transcript_text = render_messages_transcript(messages)
            if not transcript_search_matches(transcript_text, query):
                continue
            raw_results.append(
                {
                    "run_id": logical_run_id,
                    "thread_id": str(record.get("thread_id") or ""),
                    "unit_label": str(
                        metadata.get("unit_label")
                        or metadata.get("selected_unit_label")
                        or ""
                    ),
                    "file_path": str(metadata.get("file_path") or ""),
                    "content": transcript_text,
                    "created_at": str(record.get("completed_at") or record.get("started_at") or ""),
                }
            )
        if raw_results:
            return raw_results[:limit]
        return []

    def get_run_transcript(self, project_id: str, run_id: str) -> dict | None:
        record = find_daily_audit_record(
            project_id,
            logical_run_id=run_id,
            record_kind="daily_audit.analysis",
        )
        if record is not None:
            metadata = record.get("metadata_json") or {}
            messages = record.get("messages_json")
            transcript_text = render_messages_transcript(messages if isinstance(messages, list) else [])
            if transcript_text:
                return {
                    "run_id": str(metadata.get("logical_run_id") or run_id),
                    "thread_id": str(record.get("thread_id") or ""),
                    "unit_label": str(
                        metadata.get("unit_label")
                        or metadata.get("selected_unit_label")
                        or ""
                    ),
                    "file_path": str(metadata.get("file_path") or ""),
                    "content": transcript_text,
                    "created_at": str(record.get("completed_at") or record.get("started_at") or ""),
                }
        return None


_PERSISTENCE_STORE: DailyAuditPersistenceStore | None = None
_PERSISTENCE_STORE_PATH: Path | None = None


def get_daily_audit_persistence_store() -> DailyAuditPersistenceStore:
    global _PERSISTENCE_STORE, _PERSISTENCE_STORE_PATH
    db_path = Path(settings.current_snapshot().OPEN_REVIEW_DB_PATH)
    if _PERSISTENCE_STORE is None or _PERSISTENCE_STORE_PATH != db_path:
        _PERSISTENCE_STORE = DailyAuditPersistenceStore(str(db_path))
        _PERSISTENCE_STORE_PATH = db_path
    return _PERSISTENCE_STORE


def reset_daily_audit_persistence_store() -> None:
    global _PERSISTENCE_STORE, _PERSISTENCE_STORE_PATH
    _PERSISTENCE_STORE = None
    _PERSISTENCE_STORE_PATH = None
