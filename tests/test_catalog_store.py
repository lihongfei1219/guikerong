import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import load_catalog
from app.catalog_store import CatalogStore
from app.config import Settings
from app.errors import DomainError
from app.main import create_app
from tests.test_harness import sample_catalog


def test_preview_and_missing_read_never_create_database(tmp_path):
    store = CatalogStore(tmp_path / "nested/products.sqlite3")
    preview = store.import_catalog(sample_catalog(), source_name="products.csv")
    assert preview["applied"] is False
    assert len(preview["added"]) == 2
    with pytest.raises(DomainError, match="数据库"):
        store.load()
    assert not store.path.exists()


def test_real_csv_round_trip_preserves_all_fields_ids_order_and_whitespace(tmp_path):
    catalog = load_catalog(Settings().csv_path)
    store = CatalogStore(tmp_path / "products.sqlite3")
    result = store.import_catalog(catalog, source_name="products.csv", apply=True)
    assert result["product_count"] == 36
    assert store.load().model_dump(mode="json") == catalog.model_dump(mode="json")
    assert store.load().products[-1].product_id == "local-row-37"
    # Original JSON metadata and the condition column retain punctuation/newlines exactly.
    catalog.products[0].application_conditions = "  原始条件\r\n下一段\n "
    store.import_catalog(catalog, source_name="edited.csv", apply=True)
    assert store.load().products[0].application_conditions == "  原始条件\r\n下一段\n "


def test_idempotent_import_and_old_versions_remain_available(tmp_path):
    store = CatalogStore(tmp_path / "products.sqlite3")
    first = sample_catalog()
    a = store.import_catalog(first, source_name="first.csv", apply=True)
    same = store.import_catalog(first, source_name="copied.csv", apply=True)
    assert same["changed"] is False and same["revision_id"] == a["revision_id"]
    second = first.model_copy(deep=True)
    second.products[0].application_conditions += "；其他要求"
    b = store.import_catalog(second, source_name="second.csv", apply=True)
    assert b["updated"] == [first.products[0].product_id]
    assert store.load() == second
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM catalog_revisions").fetchone()[0] == 2
        old = connection.execute("SELECT application_conditions FROM products WHERE revision_id=? AND position=0", (a["revision_id"],)).fetchone()[0]
        assert old == first.products[0].application_conditions
    restored = store.import_catalog(first, source_name="first.csv", apply=True)
    assert restored["revision_id"] == a["revision_id"] and restored["changed"]
    assert store.load() == first


def test_failed_insert_rolls_back_revision_products_and_active_pointer(tmp_path):
    store = CatalogStore(tmp_path / "products.sqlite3")
    first = sample_catalog()
    store.import_catalog(first, source_name="first.csv", apply=True)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER reject_update BEFORE INSERT ON products WHEN NEW.product_name='FAIL_INSERT' BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
    updated = first.model_copy(deep=True)
    updated.products[1].product_name = "FAIL_INSERT"
    with pytest.raises(sqlite3.IntegrityError):
        store.import_catalog(updated, source_name="second.csv", apply=True)
    assert store.load() == first
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM catalog_revisions").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["empty", "unavailable", "duplicate"])
def test_invalid_catalog_cannot_replace_current(tmp_path, damage):
    store = CatalogStore(tmp_path / "products.sqlite3")
    first = sample_catalog()
    store.import_catalog(first, source_name="first.csv", apply=True)
    bad = first.model_copy(deep=True)
    if damage == "empty":
        bad.products = []
    elif damage == "unavailable":
        bad.products[0].availability_status = "UNAVAILABLE"
    else:
        bad.products.append(bad.products[0])
    with pytest.raises(DomainError):
        store.import_catalog(bad, source_name="bad.csv", apply=True)
    assert store.load() == first


@pytest.mark.parametrize("change", ["reassigned", "moved"])
def test_legacy_row_id_cannot_silently_change_identity(tmp_path, change):
    store = CatalogStore(tmp_path / "products.sqlite3")
    first = sample_catalog()
    first.products[0].product_id = "local-row-37"
    store.import_catalog(first, source_name="first.csv", apply=True)
    bad = first.model_copy(deep=True)
    if change == "reassigned":
        bad.products[0].product_name = "另一个产品"
    else:
        bad.products[0].product_id = "local-row-40"
    with pytest.raises(DomainError) as caught:
        store.import_catalog(bad, source_name="reordered.csv", apply=True)
    assert caught.value.code == "CATALOG_ID_CONFLICT"
    assert store.load() == first


def test_database_runtime_ignores_missing_csv_and_sees_atomic_new_version(tmp_path):
    settings = replace(Settings(), catalog_db_path=tmp_path / "products.sqlite3",
                       csv_path=tmp_path / "not-present.csv", harness_db_path=tmp_path / "sessions.sqlite3")
    store = CatalogStore(settings.catalog_db_path)
    first = sample_catalog()
    store.import_catalog(first, source_name="first.csv", apply=True)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/financial-products").json() == first.model_dump(mode="json")
        second = first.model_copy(deep=True)
        second.version = "second"
        second.products[0].application_conditions = "更新后的条件"
        store.import_catalog(second, source_name="second.csv", apply=True)
        assert client.get("/api/v1/financial-products").json() == second.model_dump(mode="json")
        assert client.get("/health").json()["catalog_storage"] == "sqlite"
    assert not settings.csv_path.exists()


def test_empty_db_degrades_then_recovers_after_explicit_import_without_restart(tmp_path):
    settings = replace(Settings(), catalog_db_path=tmp_path / "products.sqlite3",
                       harness_db_path=tmp_path / "sessions.sqlite3")
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").status_code == 503
        assert client.get("/api/v1/financial-products").status_code == 503
        # An existing default CSV must never be loaded implicitly.
        assert not settings.catalog_db_path.exists()
        CatalogStore(settings.catalog_db_path).import_catalog(sample_catalog(), source_name="first.csv", apply=True)
        assert client.get("/health").status_code == 200


def test_settings_accept_catalog_database_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GUIKERONG_CATALOG_DB", str(tmp_path / "custom.sqlite3"))
    assert Settings.from_env().catalog_db_path == Path(tmp_path / "custom.sqlite3")
