"""Copy SQLite snapshots into a new deployment without writing to the source."""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(source: Path, destination: Path) -> dict:
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid4().hex}.partial")
    uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(temporary)) as writer:
            try:
                with closing(sqlite3.connect(uri, uri=True)) as reader:
                    reader.backup(writer)
            except sqlite3.OperationalError:
                # A WAL-mode database with no -shm file may try to create one,
                # even for a read-only connection. Never write into the source.
                # Immutable mode is safe only with no pending WAL and an unchanged
                # source file; verify both before and after the entire copy.
                wal = source.with_name(source.name + "-wal")
                if wal.exists() and wal.stat().st_size:
                    raise
                before = file_digest(source)
                with closing(sqlite3.connect(uri + "&immutable=1", uri=True)) as reader:
                    reader.backup(writer)
                if file_digest(source) != before or (wal.exists() and wal.stat().st_size):
                    raise RuntimeError("Source changed during snapshot; stop its writer and retry") from None
            integrity = writer.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("SQLite snapshot integrity check failed")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"file": destination.name, "bytes": destination.stat().st_size, "integrity": integrity}


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy legacy SQLite snapshots; never overwrite existing files")
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, default=Path.cwd() / "data")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    databases = [
        (args.source / "data" / name, args.destination / name) for name in ("products.sqlite3", "harness.sqlite3")
    ]
    for source, destination in databases:
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists():
            raise FileExistsError(destination)
    results = (
        [snapshot(source, destination) for source, destination in databases]
        if args.apply
        else [{"file": source.name, "bytes": source.stat().st_size} for source, _ in databases]
    )
    sys.stdout.write(json.dumps({"applied": args.apply, "databases": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
