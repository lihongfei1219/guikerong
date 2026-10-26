"""Append-only durable events. Version checks prevent lost updates across workers."""

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

    def append(self, stream: str, expected: int, events: list[tuple[str, dict]]) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
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

    def sessions(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT stream, payload, created_at FROM events WHERE kind='session/created' "
                "ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return [{"id": stream, "company": json.loads(payload)["company"], "created_at": created}
                for stream, payload, created in rows]
