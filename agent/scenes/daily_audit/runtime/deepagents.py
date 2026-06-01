"""SQLite-backed deepagents runtime support for daily audit."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from agent.config import settings
from agent.scenes.daily_audit.models import (
    AuditCandidate,
    AuditUnit,
    DailyAuditAgentResponse,
    DailyAuditRunResult,
    DailyAuditSelectionResponse,
    DailyFinding,
    SubagentObservation,
)
from agent.scenes.daily_audit.persistence.raw_records import (
    append_daily_audit_agent_record,
    serialize_messages,
)
from agent.scenes.daily_audit.persistence.store import get_daily_audit_persistence_store
from agent.utils.timezone import iso_now

_GLOBAL_LOCK = threading.RLock()
_CHECKPOINTER: SQLiteCheckpointSaver | None = None
_STORE: SQLiteStore | None = None
_DB_PATH: Path | None = None
_DAILY_AUDIT_MSGPACK_ALLOWLIST = (
    AuditCandidate,
    AuditUnit,
    DailyAuditAgentResponse,
    DailyAuditRunResult,
    DailyAuditSelectionResponse,
    DailyFinding,
    SubagentObservation,
)


def _now() -> str:
    return iso_now()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def daily_audit_session_id(project_id: str, run_id: str, role: str = "primary") -> str:
    return f"daily_audit:{project_id}:{run_id}:{role}"


def daily_audit_deepagents_root() -> Path:
    return Path(settings.OPEN_REVIEW_RUNTIME_ROOT) / "daily_audit" / "deepagents"


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(value)


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_render_message_content(item) for item in content]
        return "\n\n".join(part for part in parts if part)
    if isinstance(content, dict):
        block_type = str(content.get("type") or "").strip().lower()
        if block_type == "thinking":
            text = str(content.get("thinking") or content.get("text") or content.get("content") or "").strip()
            return f"[thinking]\n{text}" if text else ""
        if block_type in {"tool_use", "tool_call"}:
            name = str(content.get("name") or content.get("tool_name") or "tool").strip()
            payload = content.get("input", content.get("args"))
            rendered_payload = _json_text(payload) if payload is not None else ""
            return f"[tool_use] {name}\n{rendered_payload}".strip()
        if block_type == "tool_result":
            name = str(content.get("name") or "tool").strip()
            rendered_result = _render_message_content(content.get("content"))
            return f"[tool_result] {name}\n{rendered_result}".strip()
        for key in ("text", "content", "thinking"):
            if key in content:
                rendered = _render_message_content(content[key])
                if rendered:
                    return rendered
        return _json_text(content)
    return str(content).strip()


def _render_checkpoint_message(message: Any) -> tuple[str, str]:
    if isinstance(message, HumanMessage):
        return "User", _render_message_content(message.content)
    if isinstance(message, AIMessage):
        return "Assistant", _render_message_content(message.content)
    if isinstance(message, ToolMessage):
        name = str(message.name or "").strip()
        header = f"Tool Result ({name})" if name else "Tool Result"
        return header, _render_message_content(message.content)
    if isinstance(message, SystemMessage):
        return "System", _render_message_content(message.content)
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "message").strip().lower()
        content = _render_message_content(message.get("content"))
        if role in {"human", "user"}:
            return "User", content
        if role in {"assistant", "ai"}:
            return "Assistant", content
        if role == "tool":
            name = str(message.get("name") or message.get("tool_name") or "").strip()
            header = f"Tool Result ({name})" if name else "Tool Result"
            return header, content
        if role == "system":
            return "System", content
        return role.title() or "Message", content
    return type(message).__name__, _render_message_content(message)


def _render_checkpoint_transcript(messages: list[Any]) -> str:
    sections: list[str] = []
    for message in messages:
        header, content = _render_checkpoint_message(message)
        text = content.strip()
        if not text:
            continue
        sections.append(f"## {header}\n{text}")
    return "\n\n".join(sections).strip()


def _load_checkpoint_messages(thread_id: str) -> list[Any]:
    checkpoint = get_daily_audit_checkpointer().get_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    if checkpoint is None:
        return []
    messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
    return list(messages) if isinstance(messages, list) else []


class SQLiteCheckpointSaver(BaseCheckpointSaver[str]):
    """SQLite-backed LangGraph checkpoint saver."""

    def __init__(self, db_path: str | Path):
        super().__init__(
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=_DAILY_AUDIT_MSGPACK_ALLOWLIST,
            )
        )
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with _connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_audit_langgraph_checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    parent_checkpoint_id TEXT,
                    PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS daily_audit_langgraph_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    PRIMARY KEY(thread_id, checkpoint_ns, channel, version)
                );
                CREATE TABLE IF NOT EXISTS daily_audit_langgraph_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    task_path TEXT NOT NULL,
                    PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                );
                """
            )

    def _load_blobs(self, conn: sqlite3.Connection, thread_id: str, checkpoint_ns: str, versions: ChannelVersions) -> dict[str, Any]:
        channel_values: dict[str, Any] = {}
        for channel, version in versions.items():
            row = conn.execute(
                """
                SELECT value_type, value_blob
                FROM daily_audit_langgraph_blobs
                WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version = ?
                """,
                (thread_id, checkpoint_ns, channel, str(version)),
            ).fetchone()
            if row is None or row["value_type"] == "empty":
                continue
            channel_values[channel] = self.serde.loads_typed((str(row["value_type"]), bytes(row["value_blob"])))
        return channel_values

    def get_tuple(self, config) -> CheckpointTuple | None:  # type: ignore[override]
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        with self._lock, _connect(self.db_path) as conn:
            if checkpoint_id:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daily_audit_langgraph_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daily_audit_langgraph_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY checkpoint_id DESC
                    LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                ).fetchone()
            if row is None:
                return None

            checkpoint = self.serde.loads_typed((str(row["checkpoint_type"]), bytes(row["checkpoint_blob"])))
            metadata = self.serde.loads_typed((str(row["metadata_type"]), bytes(row["metadata_blob"])))
            writes = conn.execute(
                """
                SELECT task_id, channel, value_type, value_blob, task_path
                FROM daily_audit_langgraph_writes
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                ORDER BY task_id, idx
                """,
                (thread_id, checkpoint_ns, str(row["checkpoint_id"])),
            ).fetchall()
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": str(row["checkpoint_id"]),
                    }
                },
                checkpoint={
                    **checkpoint,
                    "channel_values": self._load_blobs(conn, thread_id, checkpoint_ns, checkpoint["channel_versions"]),
                },
                metadata=metadata,
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": str(row["parent_checkpoint_id"]),
                        }
                    }
                    if row["parent_checkpoint_id"]
                    else None
                ),
                pending_writes=[
                    (str(write["task_id"]), str(write["channel"]), self.serde.loads_typed((str(write["value_type"]), bytes(write["value_blob"]))))
                    for write in writes
                ],
            )

    def list(self, config, *, filter=None, before=None, limit=None):  # type: ignore[override]
        with self._lock, _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM daily_audit_langgraph_checkpoints
                ORDER BY checkpoint_id DESC
                """
            ).fetchall()
            remaining = limit
            for row in rows:
                if config and row["thread_id"] != config["configurable"]["thread_id"]:
                    continue
                if config and row["checkpoint_ns"] != config["configurable"].get("checkpoint_ns", ""):
                    continue
                if before and get_checkpoint_id(before) and str(row["checkpoint_id"]) >= get_checkpoint_id(before):
                    continue
                metadata = self.serde.loads_typed((str(row["metadata_type"]), bytes(row["metadata_blob"])))
                if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                    continue
                tuple_config = {
                    "configurable": {
                        "thread_id": str(row["thread_id"]),
                        "checkpoint_ns": str(row["checkpoint_ns"]),
                        "checkpoint_id": str(row["checkpoint_id"]),
                    }
                }
                checkpoint_tuple = self.get_tuple(tuple_config)
                if checkpoint_tuple is None:
                    continue
                yield checkpoint_tuple
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        break

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        with self._lock, _connect(self.db_path) as conn:
            c = checkpoint.copy()
            thread_id = config["configurable"]["thread_id"]
            checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
            values = c.pop("channel_values")
            for channel, version in new_versions.items():
                typed = self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_audit_langgraph_blobs(
                        thread_id, checkpoint_ns, channel, version, value_type, value_blob
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, channel, str(version), typed[0], typed[1]),
                )
            checkpoint_typed = self.serde.dumps_typed(c)
            metadata_typed = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_audit_langgraph_checkpoints(
                    thread_id, checkpoint_ns, checkpoint_id, checkpoint_type, checkpoint_blob,
                    metadata_type, metadata_blob, parent_checkpoint_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    checkpoint_typed[0],
                    checkpoint_typed[1],
                    metadata_typed[0],
                    metadata_typed[1],
                    config["configurable"].get("checkpoint_id"),
                ),
            )
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint["id"],
                }
            }

    def put_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with self._lock, _connect(self.db_path) as conn:
            for idx, (channel, value) in enumerate(writes):
                mapped_idx = WRITES_IDX_MAP.get(channel, idx)
                if mapped_idx >= 0:
                    existing = conn.execute(
                        """
                        SELECT 1
                        FROM daily_audit_langgraph_writes
                        WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? AND task_id = ? AND idx = ?
                        """,
                        (thread_id, checkpoint_ns, checkpoint_id, task_id, mapped_idx),
                    ).fetchone()
                    if existing:
                        continue
                typed = self.serde.dumps_typed(value)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_audit_langgraph_writes(
                        thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, value_type, value_blob, task_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, mapped_idx, channel, typed[0], typed[1], task_path),
                )

    def delete_thread(self, thread_id: str) -> None:  # type: ignore[override]
        with self._lock, _connect(self.db_path) as conn:
            conn.execute("DELETE FROM daily_audit_langgraph_checkpoints WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM daily_audit_langgraph_blobs WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM daily_audit_langgraph_writes WHERE thread_id = ?", (thread_id,))

    async def aget_tuple(self, config):  # type: ignore[override]
        return self.get_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):  # type: ignore[override]
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:  # type: ignore[override]
        self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"


class SQLiteStore(BaseStore):
    """SQLite-backed minimal LangGraph store for daily audit."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with _connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_audit_langgraph_store (
                    namespace_json TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace_json, key)
                );
                """
            )

    def _load_items(self, conn: sqlite3.Connection) -> list[Item]:
        rows = conn.execute(
            """
            SELECT namespace_json, key, value_json, created_at, updated_at
            FROM daily_audit_langgraph_store
            """
        ).fetchall()
        return [
            Item(
                namespace=tuple(json.loads(str(row["namespace_json"]))),
                key=str(row["key"]),
                value=json.loads(str(row["value_json"])),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def batch(self, ops):  # type: ignore[override]
        results: list[Result] = []
        with self._lock, _connect(self.db_path) as conn:
            items = self._load_items(conn)
            for op in ops:
                if isinstance(op, GetOp):
                    item = next((item for item in items if item.namespace == op.namespace and item.key == op.key), None)
                    results.append(item)
                elif isinstance(op, SearchOp):
                    matches = []
                    for item in items:
                        if item.namespace[: len(op.namespace_prefix)] != op.namespace_prefix:
                            continue
                        if op.filter and not all(item.value.get(key) == value for key, value in op.filter.items()):
                            continue
                        haystack = json.dumps(item.value, ensure_ascii=True).lower()
                        if op.query and op.query.lower() not in haystack:
                            continue
                        matches.append(
                            SearchItem(
                                namespace=item.namespace,
                                key=item.key,
                                value=item.value,
                                created_at=item.created_at,
                                updated_at=item.updated_at,
                                score=1.0 if op.query else None,
                            )
                        )
                    results.append(matches[op.offset : op.offset + op.limit])
                elif isinstance(op, ListNamespacesOp):
                    namespaces = sorted({item.namespace for item in items})
                    for condition in op.match_conditions:
                        if condition.match_type == "prefix":
                            namespaces = [ns for ns in namespaces if ns[: len(condition.path)] == condition.path]
                        elif condition.match_type == "suffix":
                            namespaces = [ns for ns in namespaces if ns[-len(condition.path) :] == condition.path]
                    if op.max_depth is not None:
                        namespaces = sorted({ns[: op.max_depth] for ns in namespaces})
                    results.append(namespaces[op.offset : op.offset + op.limit])
                elif isinstance(op, PutOp):
                    namespace_json = json.dumps(list(op.namespace), ensure_ascii=True)
                    if op.value is None:
                        conn.execute(
                            "DELETE FROM daily_audit_langgraph_store WHERE namespace_json = ? AND key = ?",
                            (namespace_json, op.key),
                        )
                    else:
                        timestamp = _now()
                        existing = conn.execute(
                            "SELECT created_at FROM daily_audit_langgraph_store WHERE namespace_json = ? AND key = ?",
                            (namespace_json, op.key),
                        ).fetchone()
                        created_at = str(existing["created_at"]) if existing else timestamp
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO daily_audit_langgraph_store(
                                namespace_json, key, value_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (namespace_json, op.key, json.dumps(op.value, ensure_ascii=True), created_at, timestamp),
                        )
                    results.append(None)
                else:
                    raise ValueError(f"Unsupported store op: {type(op).__name__}")
        return results

    async def abatch(self, ops):  # type: ignore[override]
        return self.batch(list(ops))


def archive_daily_audit_run_transcript(
    *,
    project_id: str,
    runtime_run_id: str | None = None,
    run_id: str,
    unit_label: str | None = None,
    file_path: str | None = None,
    role: str = "primary",
    record_kind: str = "daily_audit.analysis",
    system_prompt: str = "",
    input_messages_json: list[dict[str, Any]] | None = None,
    result_json: Any = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> bool:
    thread_id = daily_audit_session_id(project_id, run_id, role)
    messages = _load_checkpoint_messages(thread_id)
    if not messages:
        return False
    messages_json = serialize_messages(messages)
    content = _render_checkpoint_transcript(messages)
    if not content or not runtime_run_id:
        return False
    metadata = dict(metadata_json or {})
    if unit_label:
        metadata.setdefault("unit_label", unit_label)
    if file_path:
        metadata.setdefault("file_path", file_path)
    metadata.setdefault("project_id", project_id)
    append_daily_audit_agent_record(
        runtime_run_id=runtime_run_id,
        logical_run_id=run_id,
        record_kind=record_kind,
        thread_id=thread_id,
        system_prompt=system_prompt,
        input_messages_json=list(input_messages_json or []),
        messages_json=messages_json,
        result_json=result_json,
        started_at=started_at,
        completed_at=completed_at,
        metadata_json=metadata,
    )
    return True


def _runtime_db_path() -> Path:
    return Path(settings.OPEN_REVIEW_DB_PATH)


def get_daily_audit_checkpointer() -> SQLiteCheckpointSaver:
    global _CHECKPOINTER, _DB_PATH
    with _GLOBAL_LOCK:
        db_path = _runtime_db_path()
        if _CHECKPOINTER is None or _DB_PATH != db_path:
            _CHECKPOINTER = SQLiteCheckpointSaver(db_path)
            _DB_PATH = db_path
        return _CHECKPOINTER


def get_daily_audit_store() -> SQLiteStore:
    global _STORE, _DB_PATH
    with _GLOBAL_LOCK:
        db_path = _runtime_db_path()
        if _STORE is None or _DB_PATH != db_path:
            _STORE = SQLiteStore(db_path)
            _DB_PATH = db_path
        return _STORE

def reset_daily_audit_deepagents_runtime() -> None:
    global _CHECKPOINTER, _STORE, _DB_PATH
    with _GLOBAL_LOCK:
        _CHECKPOINTER = None
        _STORE = None
        _DB_PATH = None
