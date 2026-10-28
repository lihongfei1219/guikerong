"""Validate and publish a researched bundle through append-only knowledge events.

Preview: uv run python -m app.knowledge_import knowledge/2026-10-27/manifest.json
Publish: add --apply (load the workspace .env with uv when needed).
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from app.catalog_store import CatalogStore
from app.config import Settings
from app.harness import FinancialHarness
from app.harness_models import KnowledgeDraft
from app.harness_store import EventStore
from app.models import Catalog


def load_bundle(path: Path, catalog: Catalog) -> tuple[str, list[dict]]:
    root = path.resolve().parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("bundle_id"), str):
        raise ValueError("知识包版本或标识无效")
    bundle_id = manifest["bundle_id"].strip()
    if not bundle_id or not isinstance(manifest.get("products"), list) or not manifest["products"]:
        raise ValueError("知识包必须包含产品")
    products = {p.product_id: p for p in catalog.products}
    seen_products, seen_files, drafts = set(), set(), []
    for entry in manifest["products"]:
        pid = entry["product_id"]
        if pid in seen_products or pid not in products:
            raise ValueError(f"产品重复或不存在：{pid}")
        source = (root / entry["file"]).resolve()
        if not source.is_relative_to(root) or source.suffix != ".md" or source in seen_files:
            raise ValueError("每个产品必须对应知识包目录内独立的一份 Markdown 文件")
        # A multi-topic file is anchored to the entire reviewed product text.
        # Refuse drift rather than silently applying old research to new requirements.
        if entry["condition_quote"] != products[pid].application_conditions:
            raise ValueError(f"产品申请条件已变化，请重新核对知识包：{pid}")
        draft = KnowledgeDraft(
            title=entry["title"], content=source.read_text(encoding="utf-8"),
            product_id=pid, condition_quote=entry["condition_quote"],
        ).model_dump(mode="json")
        if not draft["content"].strip():
            raise ValueError(f"知识文件不能为空：{pid}")
        digest = sha256(json.dumps(draft, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        identifier = sha256(f"{bundle_id}:{pid}:{digest}".encode("utf-8")).hexdigest()[:32]
        drafts.append({**draft, "id": f"knowledge_{identifier}", "bundle_id": bundle_id,
                       "bundle_file": source.relative_to(root).as_posix(), "bundle_sha256": digest,
                       "application_conditions": products[pid].application_conditions})
        seen_products.add(pid)
        seen_files.add(source)
    return bundle_id, drafts


def import_bundle(harness: FinancialHarness, catalog: Catalog, path: Path, *, apply: bool = False) -> dict:
    bundle_id, drafts = load_bundle(path, catalog)
    knowledge = harness.knowledge()
    existing = {item["id"]: item for item in knowledge["items"]}
    events, actions = [], []
    for draft in drafts:
        current = existing.get(draft["id"])
        if current:
            # In particular, do not undo a user's manual retirement on re-import.
            actions.append({"product_id": draft["product_id"], "title": draft["title"],
                            "action": "unchanged", "status": current["status"]})
            continue
        for old in knowledge["items"]:
            if (old.get("bundle_id") == bundle_id and old.get("product_id") == draft["product_id"]
                    and old["status"] != "RETIRED"):
                events.append(("knowledge/decided", {"id": old["id"], "status": "RETIRED"}))
        events.extend([
            ("knowledge/drafted", draft),
            ("knowledge/decided", {"id": draft["id"], "status": "PUBLISHED"}),
        ])
        actions.append({"product_id": draft["product_id"], "title": draft["title"],
                        "action": "publish", "status": "PUBLISHED"})
    if apply and events:
        # Validate everything first; one transaction and OCC check for the whole bundle.
        harness.store.append("knowledge", knowledge["version"], events)
    return {"bundle_id": bundle_id, "applied": apply, "event_count": len(events), "products": actions}


def main() -> None:
    parser = argparse.ArgumentParser(description="校验并导入每产品一份的专业知识包；默认仅预览。")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true", help="追加发布事件，使下次匹配使用这些知识")
    args = parser.parse_args()
    settings = Settings.from_env()
    harness = FinancialHarness(settings, EventStore(settings.harness_db_path))
    result = import_bundle(harness, CatalogStore(settings.catalog_db_path).load(), args.manifest, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
