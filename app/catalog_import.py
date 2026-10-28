"""Explicit CSV -> SQLite migration: preview by default, atomic publication with --apply."""

import argparse
import json
from pathlib import Path

from app.catalog import load_catalog
from app.catalog_store import CatalogStore
from app.config import Settings
from app.errors import DomainError


def main():
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="预览或导入产品 CSV 到版本化 SQLite 产品库")
    parser.add_argument("csv", nargs="?", type=Path, default=settings.csv_path)
    parser.add_argument("--db", type=Path, default=settings.catalog_db_path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = CatalogStore(args.db).import_catalog(load_catalog(args.csv), source_name=args.csv.name, apply=args.apply)
    except DomainError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
