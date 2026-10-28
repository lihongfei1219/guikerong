"""Append-only SQLModel event store with atomic version checks and deletion."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func
from sqlalchemy.orm import aliased
from sqlmodel import SQLModel, col, select

from guikerong_finance.db.entities import Event
from guikerong_finance.db.session import session_scope
from guikerong_finance.errors import DomainError


class EventStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _session(self, *, write=False):
        if not self.path.exists():
            with session_scope(self.path, write=True, tables=(SQLModel.metadata.tables["events"],)):
                pass
        return session_scope(self.path, write=write, tables=(SQLModel.metadata.tables["events"],) if write else ())

    def read(self, stream: str) -> list[dict]:
        with self._session() as db:
            rows = db.exec(select(Event).where(col(Event.stream) == stream).order_by(col(Event.version))).all()
            return [
                {
                    "version": row.version,
                    "kind": row.kind,
                    "data": json.loads(row.payload),
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def append(
        self, stream: str, expected: int, events: list[tuple[str, dict]], *, owner_session: str | None = None
    ) -> None:
        with self._session(write=True) as db:
            if (
                owner_session
                and db.exec(
                    select(Event).where(
                        col(Event.stream) == owner_session,
                        col(Event.kind) == "session/created",
                    )
                ).first()
                is None
            ):
                raise DomainError("SESSION_NOT_FOUND", "会话已删除，不能再写入模型调用记录。", 404)
            actual = db.exec(
                select(func.coalesce(func.max(col(Event.version)), 0)).where(col(Event.stream) == stream)
            ).one()
            if actual != expected:
                raise DomainError("SESSION_CONFLICT", "记录已更新，请刷新后再操作。", 409)
            now = datetime.now(UTC).isoformat()
            db.add_all(
                [
                    Event(
                        stream=stream,
                        version=expected + index + 1,
                        kind=kind,
                        payload=json.dumps(data, ensure_ascii=False),
                        created_at=now,
                    )
                    for index, (kind, data) in enumerate(events)
                ]
            )

    def delete_session(self, identifier: str, expected: int) -> dict:
        with self._session(write=True) as db:
            rows = db.exec(select(Event).where(col(Event.stream) == identifier).order_by(col(Event.version))).all()
            if not rows or rows[0].kind != "session/created":
                raise DomainError("SESSION_NOT_FOUND", "会话不存在或已删除。", 404)
            if rows[-1].version != expected:
                raise DomainError("SESSION_CONFLICT", "会话已更新，请刷新后确认要删除的最新记录。", 409)
            run_ids = {json.loads(row.payload)["id"] for row in rows if row.kind == "match/requested"}
            if any(not run_id.startswith("run_") for run_id in run_ids):
                raise DomainError("INVALID_RUN_ID", "匹配记录标识异常，未删除会话。", 409)
            traces = 0
            for run_id in run_ids:
                traces += db.exec(select(func.count()).select_from(Event).where(col(Event.stream) == run_id)).one()
                db.execute(delete(Event).where(col(Event.stream) == run_id))
            db.execute(delete(Event).where(col(Event.stream) == identifier))
        return {
            "deleted": True,
            "session_id": identifier,
            "deleted_events": len(rows),
            "deleted_model_requests": traces,
        }

    def sessions(self) -> list[dict]:
        origin = aliased(Event)
        with self._session() as db:
            rows = db.exec(
                select(Event, col(origin.payload))
                .outerjoin(
                    origin,
                    (col(origin.stream) == col(Event.stream)) & (col(origin.kind) == "session/continued"),
                )
                .where(col(Event.kind) == "session/created")
                .order_by(col(Event.created_at).desc())
                .limit(100)
            ).all()
            return [
                {
                    "id": row.stream,
                    "company": json.loads(row.payload)["company"],
                    "created_at": row.created_at,
                    **({"origin": json.loads(payload)} if payload else {}),
                }
                for row, payload in rows
            ]
