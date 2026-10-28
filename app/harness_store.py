"""SQLite 事件存储：追加写入、乐观版本校验，以及显式整会话事务删除。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError


class EventStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events (stream TEXT NOT NULL, version INTEGER NOT NULL, "
            "kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(stream, version))"
        )
        return connection

    def read(self, stream: str) -> list[dict]:
        with closing(self.connect()) as connection:
            return [dict(version=row[0], kind=row[1], data=json.loads(row[2]), created_at=row[3])
                    for row in connection.execute(
                        "SELECT version, kind, payload, created_at FROM events WHERE stream=? ORDER BY version",
                        (stream,),
                    )]

    def append(self, stream: str, expected: int, events: list[tuple[str, dict]], *, owner_session: str | None = None) -> None:
        """持有写锁后检查所属会话和版本，再一次写入整批事件，避免并发覆盖。"""
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if owner_session and not connection.execute(
                "SELECT 1 FROM events WHERE stream=? AND kind='session/created'", (owner_session,),
            ).fetchone():
                raise DomainError("SESSION_NOT_FOUND", "会话已删除，不能再写入模型调用记录。", 404)
            actual = connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM events WHERE stream=?", (stream,),
            ).fetchone()[0]
            if actual != expected:
                raise DomainError("SESSION_CONFLICT", "记录已更新，请刷新后再操作。", 409)
            now = datetime.now(timezone.utc).isoformat()
            connection.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
                (stream, expected + index + 1, kind, json.dumps(data, ensure_ascii=False), now)
                for index, (kind, data) in enumerate(events)
            ])

    def delete_session(self, identifier: str, expected: int) -> dict:
        """事务删除会话及其拥有的请求流；独立历史分支和公共知识均保留。"""
        with closing(self.connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT version,kind,payload FROM events WHERE stream=? ORDER BY version", (identifier,),
                ).fetchall()
                if not rows or rows[0][1] != "session/created":
                    raise DomainError("SESSION_NOT_FOUND", "会话不存在或已删除。", 404)
                if rows[-1][0] != expected:
                    raise DomainError("SESSION_CONFLICT", "会话已更新，请刷新后确认要删除的最新记录。", 409)
                # 导入基线只是副本，不拥有原会话的模型请求；删除时不能沿 origin 继续追溯。
                run_ids = {json.loads(payload)["id"] for _, kind, payload in rows if kind == "match/requested"}
                traces = 0
                for run_id in run_ids:
                    if not run_id.startswith("run_"):
                        raise DomainError("INVALID_RUN_ID", "匹配记录标识异常，未删除会话。", 409)
                    traces += connection.execute("DELETE FROM events WHERE stream=?", (run_id,)).rowcount
                connection.execute("DELETE FROM events WHERE stream=?", (identifier,))
            # Best effort WAL cleanup; another reader can temporarily pin the WAL.
            # The committed DELETE is authoritative, even if checkpointing is busy.
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        return {"deleted": True, "session_id": identifier, "deleted_events": len(rows),
                "deleted_model_requests": traces}

    def sessions(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT e.stream, e.payload, e.created_at, o.payload FROM events e "
                "LEFT JOIN events o ON o.stream=e.stream AND o.kind='session/continued' "
                "WHERE e.kind='session/created' ORDER BY e.created_at DESC LIMIT 100"
            ).fetchall()
        return [{"id": stream, "company": json.loads(payload)["company"], "created_at": created,
                 **({"origin": json.loads(origin)} if origin else {})}
                for stream, payload, created, origin in rows]
