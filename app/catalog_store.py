"""Versioned product catalogs; CSV is an explicit import source, never a runtime fallback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.errors import DomainError
from app.models import Catalog, Product


class CatalogStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _reader(self):
        # A missing DB must not silently create an empty file or fall back to CSV.
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)

    @staticmethod
    def _schema(connection):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS catalog_revisions (
                id INTEGER PRIMARY KEY,
                content_hash TEXT NOT NULL UNIQUE,
                catalog_version TEXT NOT NULL,
                source_name TEXT NOT NULL,
                raw_record_count INTEGER NOT NULL,
                warnings_json TEXT NOT NULL,
                product_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                revision_id INTEGER NOT NULL REFERENCES catalog_revisions(id),
                product_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                institution TEXT NOT NULL,
                application_conditions TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (revision_id, product_id),
                UNIQUE (revision_id, position)
            );
            CREATE TABLE IF NOT EXISTS catalog_active (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                revision_id INTEGER NOT NULL REFERENCES catalog_revisions(id)
            );
        """)

    @staticmethod
    def _load(connection):
        revision = connection.execute(
            "SELECT r.id,r.catalog_version,r.raw_record_count,r.warnings_json,r.product_count "
            "FROM catalog_revisions r JOIN catalog_active a ON a.revision_id=r.id WHERE a.singleton=1"
        ).fetchone()
        if revision is None:
            raise DomainError("CATALOG_UNAVAILABLE", "产品数据库尚未导入，请先运行产品目录导入命令。", 503)
        rows = connection.execute(
            "SELECT product_id,product_name,institution,application_conditions,metadata_json "
            "FROM products WHERE revision_id=? ORDER BY position", (revision[0],),
        ).fetchall()
        if len(rows) != revision[4]:
            raise DomainError("CATALOG_INVALID", "产品数据库记录不完整，请恢复完整目录版本。", 503)
        products = [Product.model_validate({**json.loads(row[4]), "product_id": row[0],
                    "product_name": row[1], "institution": row[2], "application_conditions": row[3]}) for row in rows]
        return Catalog(version=revision[1], raw_record_count=revision[2],
                       warnings=json.loads(revision[3]), products=products)

    def load(self) -> Catalog:
        try:
            with closing(self._reader()) as connection:
                # Pin the active pointer and its rows to the same read snapshot.
                connection.execute("BEGIN")
                return self._load(connection)
        except (sqlite3.Error, OSError, ValueError, ValidationError) as exc:
            raise DomainError("CATALOG_UNAVAILABLE", "产品数据库不可用，请检查数据库或先导入产品目录。", 503) from exc

    @staticmethod
    def _changes(current, catalog):
        old = {p.product_id: p for p in current.products} if current else {}
        new = {p.product_id: p for p in catalog.products}
        # Legacy row-based IDs already link knowledge and history. A reordered CSV
        # must never silently assign that existing ID to another product.
        for identifier in old.keys() & new.keys():
            if identifier.startswith("local-row-") and (
                old[identifier].product_name != new[identifier].product_name
                or old[identifier].institution != new[identifier].institution
            ):
                raise DomainError("CATALOG_ID_CONFLICT", "无外部编号产品的行号发生冲突，请保留原产品 ID 后再导入。", 400)
        for identifier, product in old.items():
            if identifier.startswith("local-row-") and identifier not in new and any(
                p.product_name == product.product_name and p.institution == product.institution for p in new.values()
            ):
                raise DomainError("CATALOG_ID_CONFLICT", "无外部编号产品的行位置变化，请保留原产品 ID 后再导入。", 400)
        return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
                "updated": sorted(key for key in old.keys() & new.keys() if old[key] != new[key])}

    def import_catalog(self, catalog: Catalog, *, source_name: str, apply: bool = False):
        if not catalog.products or any(p.availability_status == "UNAVAILABLE" for p in catalog.products):
            raise DomainError("CATALOG_IMPORT_INVALID", "产品目录为空或含缺字段、列错位、冲突记录，未导入。", 400)
        ids = [p.product_id for p in catalog.products]
        if len(ids) != len(set(ids)):
            raise DomainError("CATALOG_IMPORT_INVALID", "产品 ID 重复，未导入。", 400)
        serialized = json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if not apply:
            current = self.load() if self.path.exists() else None
            return {"applied": False, "product_count": len(ids), "catalog_version": catalog.version,
                    "content_hash": digest, **self._changes(current, catalog)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            self._schema(connection)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute("SELECT revision_id FROM catalog_active WHERE singleton=1").fetchone()
                current = self._load(connection) if active else None
                changes = self._changes(current, catalog)
                existing = connection.execute("SELECT id FROM catalog_revisions WHERE content_hash=?", (digest,)).fetchone()
                if existing:
                    revision_id = existing[0]
                else:
                    cursor = connection.execute(
                        "INSERT INTO catalog_revisions(content_hash,catalog_version,source_name,raw_record_count,"
                        "warnings_json,product_count,created_at) VALUES (?,?,?,?,?,?,?)",
                        (digest, catalog.version, Path(source_name).name, catalog.raw_record_count,
                         json.dumps(catalog.warnings, ensure_ascii=False), len(ids), datetime.now(timezone.utc).isoformat()),
                    )
                    revision_id = cursor.lastrowid
                    for position, product in enumerate(catalog.products):
                        metadata = product.model_dump(mode="json", exclude={
                            "product_id", "product_name", "institution", "application_conditions",
                        })
                        connection.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?)", (
                            revision_id, product.product_id, position, product.product_name, product.institution,
                            product.application_conditions, json.dumps(metadata, ensure_ascii=False),
                        ))
                changed = not active or active[0] != revision_id
                connection.execute(
                    "INSERT INTO catalog_active VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET revision_id=excluded.revision_id",
                    (revision_id,),
                )
                return {"applied": True, "changed": changed, "revision_id": revision_id,
                        "product_count": len(ids), "catalog_version": catalog.version, "content_hash": digest, **changes}
