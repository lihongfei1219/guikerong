import csv
import json
from pathlib import Path

import pytest

from app.catalog import CUSTOMER_COLUMN, load_catalog
from app.errors import DomainError

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "科技金融产品.csv"


def source_row(**updates):
    row = {
        "产品名称": "测试产品", "机构名称": "测试银行",
        "详情URL": "https://guikerong.com/#/portal/financial-services/product/1234567890123456789",
        "产品介绍": "面向科技企业提供融资。", "申请条件": "成立满1年；经营合法合规",
        CUSTOMER_COLUMN: "科技型企业", "综合利率": "以实际审批为准",
    }
    row.update(updates)
    return row


def save_csv(tmp_path, rows):
    path = tmp_path / "products.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path








def test_real_snapshot_includes_all_products():
    catalog = load_catalog(CSV)
    assert catalog.raw_record_count == 36
    assert len(catalog.products) == 36
    assert len({p.product_id for p in catalog.products}) == 36
    assert all(p.availability_status == "SNAPSHOT" for p in catalog.products)
    assert catalog.version.startswith("csv-sha256:")


def test_deduplicates_by_id_without_merging_same_name(tmp_path):
    first = source_row()
    duplicate = source_row(详情URL=first["详情URL"] + "?page=2")
    distinct = source_row(详情URL=first["详情URL"] + "1")
    catalog = load_catalog(save_csv(tmp_path, [first, duplicate, distinct]))
    assert catalog.raw_record_count == 3
    assert len(catalog.products) == 2
    product = catalog.products[0]
    assert product.source_rows == [2, 3]
    original_records = json.loads(product.raw_fields["_source_records_json"])
    assert original_records[1]["fields"] == duplicate
    assert "?" not in product.detail_url
    assert catalog.products[1].product_name == product.product_name
    assert catalog.products[1].product_id != product.product_id


@pytest.mark.parametrize("ending", ["与借款人建", "……", "..."])
def test_application_ending_does_not_block_matching_or_change_original(tmp_path, ending):
    original = "企业经营正常；" + ending + "\r\n"
    product = load_catalog(save_csv(tmp_path, [source_row(申请条件=original)])).products[0]
    assert product.availability_status == "SNAPSHOT"
    assert product.unavailable_reason is None
    assert product.application_conditions == original
    assert product.raw_fields["申请条件"] == original


def test_same_id_conflict_is_quarantined_and_retains_both_versions(tmp_path):
    first = source_row()
    second = source_row(申请条件="成立满2年")
    second["详情URL"] += "?page=2"
    catalog = load_catalog(save_csv(tmp_path, [first, second]))
    assert len(catalog.products) == 1
    product = catalog.products[0]
    assert product.product_id == "1234567890123456789"
    assert product.availability_status == "UNAVAILABLE"
    assert "内容冲突" in product.unavailable_reason
    assert len(json.loads(product.raw_fields["_source_records_json"])) == 2


@pytest.mark.parametrize("column", ["产品名称", "申请条件"])
def test_missing_required_cell_is_unavailable(tmp_path, column):
    product = load_catalog(save_csv(tmp_path, [source_row(**{column: ""})])).products[0]
    assert product.availability_status == "UNAVAILABLE"
    assert column in product.unavailable_reason


@pytest.mark.parametrize("column", ["机构名称", "产品介绍", "详情URL", CUSTOMER_COLUMN, "综合利率"])
@pytest.mark.parametrize("missing_header", [False, True])
def test_unused_product_fields_are_not_required_for_matching(tmp_path, column, missing_header):
    row = source_row()
    if missing_header:
        del row[column]
    else:
        row[column] = ""
    product = load_catalog(save_csv(tmp_path, [row])).products[0]
    assert product.availability_status == "SNAPSHOT"
    assert product.unavailable_reason is None
    assert product.application_conditions == row["申请条件"]


def test_catalog_accepts_only_name_and_conditions_without_merging_same_names(tmp_path):
    rows = [
        {"产品名称": "同名产品", "申请条件": "1. 成立满一年。\n2. 经营合法。"},
        {"产品名称": "同名产品", "申请条件": "成立满两年。"},
    ]
    catalog = load_catalog(save_csv(tmp_path, rows))
    assert catalog.raw_record_count == len(catalog.products) == 2
    assert len({product.product_id for product in catalog.products}) == 2
    assert all(product.availability_status == "SNAPSHOT" for product in catalog.products)
    assert catalog.products[0].application_conditions == rows[0]["申请条件"]


def test_optional_metadata_conflict_does_not_block_same_application_conditions(tmp_path):
    first = source_row()
    second = source_row(产品介绍="这段介绍不同，不作为申请条件。", 机构名称="另一个机构名称")
    second[CUSTOMER_COLUMN] = "未纳入申请条件的客群说明"
    second["详情URL"] += "?page=2"
    catalog = load_catalog(save_csv(tmp_path, [first, second]))
    assert len(catalog.products) == 1
    assert catalog.products[0].availability_status == "SNAPSHOT"
    assert len(json.loads(catalog.products[0].raw_fields["_source_records_json"])) == 2


def test_missing_optional_trailing_cells_does_not_block_assessment(tmp_path):
    path = tmp_path / "products.csv"
    path.write_text("产品名称,申请条件,产品介绍\n测试产品,成立满一年\n", encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.products[0].availability_status == "SNAPSHOT"


def test_raw_catalog_retains_all_fields_without_injecting_conditions(tmp_path):
    row = source_row(申请条件="需要全部原文\n第二段有独立要求，不能丢失。")
    catalog = load_catalog(save_csv(tmp_path, [row]))
    product = catalog.products[0]
    assert "rules" not in product.model_dump()
    assert product.application_conditions == row["申请条件"]
    assert product.applicable_customers == row[CUSTOMER_COLUMN]
    assert all(product.raw_fields[key] == value for key, value in row.items())
    assert product.raw_fields["综合利率"] == row["综合利率"]
    assert product.source_rows == [2]
    assert catalog.warnings == []




def test_raw_product_cells_keep_original_whitespace(tmp_path):
    row = source_row(申请条件="  成立满1年；经营合法合规\r\n", 产品介绍="\n  面向科技企业提供融资。  ")
    row["综合利率"] = "  以实际审批为准  "
    row[CUSTOMER_COLUMN] = " 科技型企业\n"
    product = load_catalog(save_csv(tmp_path, [row])).products[0]
    assert all(product.raw_fields[key] == value for key, value in row.items())
    assert product.application_conditions == row["申请条件"]
    assert product.introduction == row["产品介绍"]
    assert product.applicable_customers == row[CUSTOMER_COLUMN]
















def test_missing_required_header_fails_import(tmp_path):
    row = source_row()
    del row["申请条件"]
    with pytest.raises(DomainError, match="缺少必需列"):
        load_catalog(save_csv(tmp_path, [row]))
