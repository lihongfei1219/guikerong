"""Explicit CSV -> SQLite migration: preview by default, atomic publication with --apply."""

import argparse
import json
import sys
from pathlib import Path

from guikerong_finance.config import Settings
from guikerong_finance.db.catalog import CatalogStore
from guikerong_finance.errors import DomainError
from guikerong_finance.service.catalog import load_catalog


def main():
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="预览或导入产品 CSV 到版本化 SQLite 产品库")
    parser.add_argument("csv", nargs="?", type=Path, default=settings.csv_path)
    parser.add_argument("--db", type=Path, default=settings.catalog_db_path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = CatalogStore(args.db).import_catalog(
            load_catalog(args.csv), source_name=args.csv.name, apply=args.apply
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except DomainError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
