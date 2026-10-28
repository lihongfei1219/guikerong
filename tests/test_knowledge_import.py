import json
from pathlib import Path

import pytest

from guikerong_finance.config import ROOT, Settings
from guikerong_finance.db.events import EventStore
from guikerong_finance.models.commands import KnowledgeDecision
from guikerong_finance.service.catalog import load_catalog
from guikerong_finance.service.harness import FinancialHarness
from guikerong_finance.service.knowledge_import import import_bundle, load_bundle
from tests.test_harness import sample_catalog


@pytest.fixture
def bundle(tmp_path):
    catalog = sample_catalog()
    harness = FinancialHarness(Settings(), EventStore(tmp_path / "events.sqlite3"))
    manifest: dict = {"schema_version": 1, "bundle_id": "test-research", "products": []}
    for product in catalog.products:
        filename = f"{product.product_id}.md"
        (tmp_path / filename).write_text("知识解释正文，不是企业证明。", encoding="utf-8")
        manifest["products"].append(
            {
                "product_id": product.product_id,
                "title": product.product_name,
                "file": filename,
                "condition_quote": product.application_conditions,
            }
        )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return harness, catalog, path


def test_bundle_preview_publish_routing_and_repeat_preserve_events(bundle):
    harness, catalog, path = bundle
    preview = import_bundle(harness, catalog, path)
    assert preview["event_count"] == 4
    assert harness.knowledge()["items"] == []
    import_bundle(harness, catalog, path, apply=True)
    before = harness.store.read("knowledge")
    context = harness.knowledge_context(catalog)
    assert {pid: len(items) for pid, items in context.items()} == {"p1": 1, "p2": 1}
    for pid, items in context.items():
        assert items[0]["product_id"] == pid
        assert items[0]["content"] == path.with_name(f"{pid}.md").read_text(encoding="utf-8")
    assert import_bundle(harness, catalog, path, apply=True)["event_count"] == 0
    assert harness.store.read("knowledge") == before


def test_changed_file_retires_only_its_previous_bundle_version(bundle):
    harness, catalog, path = bundle
    import_bundle(harness, catalog, path, apply=True)
    before = harness.store.read("knowledge")
    path.with_name("p1.md").write_text("经过补充查证的新解释。", encoding="utf-8")
    result = import_bundle(harness, catalog, path, apply=True)
    assert result["event_count"] == 3
    assert harness.store.read("knowledge")[: len(before)] == before
    items = harness.knowledge()["items"]
    assert len(items) == 3
    assert len([item for item in items if item["status"] == "RETIRED"]) == 1
    assert len(harness.knowledge_context(catalog)["p1"]) == 1


def test_reimport_does_not_undo_manual_retirement(bundle):
    harness, catalog, path = bundle
    import_bundle(harness, catalog, path, apply=True)
    item = harness.knowledge()["items"][0]
    harness.decide_knowledge(
        item["id"], KnowledgeDecision(expected_version=item["version"], status="RETIRED"), catalog
    )
    assert import_bundle(harness, catalog, path, apply=True)["event_count"] == 0
    assert harness.knowledge_context(catalog)[item["product_id"]] == []


@pytest.mark.parametrize(
    "invalid", ["duplicate_product", "duplicate_file", "outside", "changed_condition", "missing_file"]
)
def test_invalid_bundle_never_partially_publishes(bundle, invalid):
    harness, catalog, path = bundle
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = manifest["products"]
    if invalid == "duplicate_product":
        entries.append(entries[0])
    elif invalid == "duplicate_file":
        entries[1]["file"] = entries[0]["file"]
    elif invalid == "outside":
        entries[1]["file"] = "../outside.md"
    elif invalid == "changed_condition":
        entries[1]["condition_quote"] += "已经变化"
    else:
        entries[1]["file"] = "missing.md"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        import_bundle(harness, catalog, path, apply=True)
    assert harness.knowledge()["items"] == []


def test_researched_bundle_has_one_sourced_file_per_product():
    path = ROOT / "knowledge" / "2026-10-27" / "manifest.json"
    _, drafts = load_bundle(path, load_catalog(Settings().csv_path))
    assert len(drafts) >= 10
    files = {path.parent / draft["bundle_file"] for draft in drafts}
    assert files == set(path.parent.glob("products/*.md"))
    assert len({draft["product_id"] for draft in drafts}) == len(drafts)
    for draft in drafts:
        assert "https://" in draft["content"] or "http://" in draft["content"]
        assert "2026-10-27" in draft["content"]
        assert len(draft["content"]) <= 12000
        assert Path(draft["bundle_file"]).name == f"{draft['product_id']}.md"
