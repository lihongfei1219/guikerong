"""The deployment copy includes WAL transactions and never overwrites a DB."""

import sqlite3
from contextlib import closing

import pytest

from scripts.migrate_data import snapshot


def test_snapshot_includes_uncheckpointed_wal_and_is_independent(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "new" / "events.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sample (value TEXT)")
        writer.execute("INSERT INTO sample VALUES (?)", ("unchanged source",))
        writer.commit()
        assert snapshot(source, target)["integrity"] == "ok"
        with closing(sqlite3.connect(target)) as reader:
            assert reader.execute("SELECT value FROM sample").fetchall() == [("unchanged source",)]
        writer.execute("INSERT INTO sample VALUES (?)", ("later source change",))
        writer.commit()
    with closing(sqlite3.connect(target)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
    with pytest.raises(FileExistsError):
        snapshot(source, target)
