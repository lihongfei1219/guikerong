"""SQLite session adapter: Mindforge 0.0.40 SQLManager only supports MySQL.

Keep its SQLModel session-factory contract without patching framework internals.
Driver SQL only configures SQLite transactions; business queries use SQLModel.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine


def sqlite_engine(path: Path, *, readonly: bool = False) -> Engine:
    if readonly:
        url = URL.create("sqlite+pysqlite", database=path.resolve().as_uri(), query={"mode": "ro", "uri": "true"})
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        url = URL.create("sqlite+pysqlite", database=str(path.resolve()))
    engine = create_engine(url, connect_args={"timeout": 10, "check_same_thread": False}, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.isolation_level = None
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA secure_delete=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql(
            "BEGIN IMMEDIATE" if connection.get_execution_options().get("sqlite_write") else "BEGIN"
        )

    return engine


@contextmanager
def session_scope(path: Path, *, write: bool = False, tables: tuple = ()) -> Iterator[Session]:
    engine = sqlite_engine(path, readonly=not write)
    try:
        if tables:
            SQLModel.metadata.create_all(engine, tables=list(tables))
        with Session(engine, expire_on_commit=False) as session:
            session.connection(execution_options={"sqlite_write": write})
            try:
                yield session
                if write:
                    session.commit()
            except BaseException:
                session.rollback()
                raise
    finally:
        engine.dispose()
