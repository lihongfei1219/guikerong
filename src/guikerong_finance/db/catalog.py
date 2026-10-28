"""Versioned product catalogs; CSV is an explicit import source, never a runtime fallback."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, col, select

from guikerong_finance.db.entities import CatalogActive, CatalogProduct, CatalogRevision
from guikerong_finance.db.session import session_scope
from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import Catalog, Product


class CatalogStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _load(db: Session) -> Catalog:
        revision = db.exec(
            select(CatalogRevision)
            .join(
                CatalogActive,
                col(CatalogActive.revision_id) == col(CatalogRevision.id),
            )
            .where(col(CatalogActive.singleton) == 1)
        ).first()
        if revision is None:
            raise DomainError("CATALOG_UNAVAILABLE", "产品数据库尚未导入，请先运行产品目录导入命令。", 503)
        rows = db.exec(
            select(CatalogProduct)
            .where(
                col(CatalogProduct.revision_id) == revision.id,
            )
            .order_by(col(CatalogProduct.position))
        ).all()
        if len(rows) != revision.product_count:
            raise DomainError("CATALOG_INVALID", "产品数据库记录不完整，请恢复完整目录版本。", 503)
        products = [
            Product.model_validate(
                {
                    **json.loads(row.metadata_json),
                    "product_id": row.product_id,
                    "product_name": row.product_name,
                    "institution": row.institution,
                    "application_conditions": row.application_conditions,
                }
            )
            for row in rows
        ]
        return Catalog(
            version=revision.catalog_version,
            raw_record_count=revision.raw_record_count,
            warnings=json.loads(revision.warnings_json),
            products=products,
        )

    def load(self) -> Catalog:
        try:
            with session_scope(self.path) as db:
                return self._load(db)
        except (SQLAlchemyError, OSError, ValueError, ValidationError) as exc:
            raise DomainError("CATALOG_UNAVAILABLE", "产品数据库不可用，请检查数据库或先导入产品目录。", 503) from exc

    @staticmethod
    def _changes(current, catalog) -> dict:
        old = {p.product_id: p for p in current.products} if current else {}
        new = {p.product_id: p for p in catalog.products}
        # Legacy row-based IDs already link knowledge and history. A reordered CSV
        # must never silently assign that existing ID to another product.
        for identifier in old.keys() & new.keys():
            if identifier.startswith("local-row-") and (
                old[identifier].product_name != new[identifier].product_name
                or old[identifier].institution != new[identifier].institution
            ):
                raise DomainError(
                    "CATALOG_ID_CONFLICT", "无外部编号产品的行号发生冲突，请保留原产品 ID 后再导入。", 400
                )
        for identifier, product in old.items():
            if (
                identifier.startswith("local-row-")
                and identifier not in new
                and any(
                    p.product_name == product.product_name and p.institution == product.institution
                    for p in new.values()
                )
            ):
                raise DomainError("CATALOG_ID_CONFLICT", "无外部编号产品的行位置变化，请保留原产品 ID 后再导入。", 400)
        return {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "updated": sorted(key for key in old.keys() & new.keys() if old[key] != new[key]),
        }

    def import_catalog(self, catalog: Catalog, *, source_name: str, apply: bool = False):
        if not catalog.products or any(p.availability_status == "UNAVAILABLE" for p in catalog.products):
            raise DomainError("CATALOG_IMPORT_INVALID", "产品目录为空或含缺字段、列错位、冲突记录，未导入。", 400)
        ids = [p.product_id for p in catalog.products]
        if len(ids) != len(set(ids)):
            raise DomainError("CATALOG_IMPORT_INVALID", "产品 ID 重复，未导入。", 400)
        serialized = json.dumps(
            catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if not apply:
            current = self.load() if self.path.exists() else None
            return {
                "applied": False,
                "product_count": len(ids),
                "catalog_version": catalog.version,
                "content_hash": digest,
                **self._changes(current, catalog),
            }
        tables = (
            SQLModel.metadata.tables["catalog_revisions"],
            SQLModel.metadata.tables["products"],
            SQLModel.metadata.tables["catalog_active"],
        )
        with session_scope(self.path, write=True, tables=tables) as db:
            active = db.get(CatalogActive, 1)
            current = self._load(db) if active else None
            changes = self._changes(current, catalog)
            revision = db.exec(select(CatalogRevision).where(col(CatalogRevision.content_hash) == digest)).first()
            if revision is None:
                revision = CatalogRevision(
                    content_hash=digest,
                    catalog_version=catalog.version,
                    source_name=Path(source_name).name,
                    raw_record_count=catalog.raw_record_count,
                    warnings_json=json.dumps(catalog.warnings, ensure_ascii=False),
                    product_count=len(ids),
                    created_at=datetime.now(UTC).isoformat(),
                )
                db.add(revision)
                db.flush()
                for position, product in enumerate(catalog.products):
                    metadata = product.model_dump(
                        mode="json",
                        exclude={
                            "product_id",
                            "product_name",
                            "institution",
                            "application_conditions",
                        },
                    )
                    db.add(
                        CatalogProduct(
                            revision_id=revision.id,
                            product_id=product.product_id,
                            position=position,
                            product_name=product.product_name,
                            institution=product.institution,
                            application_conditions=product.application_conditions,
                            metadata_json=json.dumps(metadata, ensure_ascii=False),
                        )
                    )
                db.flush()
            assert revision.id is not None
            changed = not active or active.revision_id != revision.id
            if active:
                active.revision_id = revision.id
            else:
                active = CatalogActive(revision_id=revision.id)
            db.add(active)
            return {
                "applied": True,
                "changed": changed,
                "revision_id": revision.id,
                "product_count": len(ids),
                "catalog_version": catalog.version,
                "content_hash": digest,
                **changes,
            }
