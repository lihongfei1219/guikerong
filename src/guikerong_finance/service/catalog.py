"""Load product names and application conditions; retain other cells as local metadata."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import Catalog, Product

REQUIRED_COLUMNS = ("产品名称", "申请条件")
CUSTOMER_COLUMN = "产品参数_适用客户"
PRODUCT_URL_PATTERN = re.compile(r"^(https?://[^\s]+?/(?:#/)?portal/financial-services/product/)([0-9]+)(?:[/?#].*)?$")


def canonical_product_url(value: str) -> tuple[str | None, str]:
    """Keep identifiers as strings; pagination is not part of product identity."""
    match = PRODUCT_URL_PATTERN.fullmatch(value.strip())
    if not match:
        return None, value.strip()
    return match.group(2), f"{match.group(1)}{match.group(2)}"


def source_record_hash(raw_fields: dict[str, str]) -> str:
    """Hash all CSV fields, ignoring surrounding whitespace and pagination only."""
    fields = {key: value.strip() for key, value in raw_fields.items() if not key.startswith("_source_")}
    if "详情URL" in fields:
        _, fields["详情URL"] = canonical_product_url(fields["详情URL"])
    serialized = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_catalog(csv_path: Path) -> Catalog:
    """Return every distinct product, quarantining malformed/conflicting rows.

    ``source_rows`` counts CSV records including the header as record 1; a quoted
    multiline cell remains one record. ``raw_fields`` retains every original cell.
    Matching requires only product names and application conditions. Optional
    metadata is retained locally and does not add assessment requirements.
    """
    required_columns = REQUIRED_COLUMNS
    try:
        csv_bytes = Path(csv_path).read_bytes()
        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""), strict=True)
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers):
            raise ValueError("CSV 存在重复列名")
        missing = set(required_columns) - set(headers)
        if missing:
            raise ValueError("CSV 缺少必需列：" + "、".join(sorted(missing)))
        records = list(reader)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise DomainError("CATALOG_INVALID", f"无法导入金融产品 CSV：{exc}", 503) from exc

    warnings: list[str] = []
    grouped: dict[str, list[tuple[int, dict[str, str], bool]]] = {}
    for record_number, record in enumerate(records, start=2):
        malformed = None in record or any(record.get(column) is None for column in required_columns)
        raw = {key: value or "" for key, value in record.items() if key is not None}
        if None in record:
            raw["_source_extra_cells"] = json.dumps(record[None], ensure_ascii=False)
        product_id, _ = canonical_product_url(raw.get("详情URL", ""))
        product_id = product_id or f"local-row-{record_number}"
        grouped.setdefault(product_id, []).append((record_number, raw, malformed))

    products: list[Product] = []
    for product_id, sources in grouped.items():
        raw = sources[0][1]
        source_hash = source_record_hash(raw)
        _, canonical_url = canonical_product_url(raw.get("详情URL", ""))
        errors = []
        empty = [column for column in required_columns if not raw.get(column, "").strip()]
        if empty:
            errors.append("关键字段缺失：" + "、".join(empty))
        if any(malformed for _, _, malformed in sources):
            errors.append("CSV 记录列数与表头不一致")
        source_signatures = {
            tuple(item.get(column, "").strip() for column in REQUIRED_COLUMNS) for _, item, _ in sources
        }
        if len(source_signatures) > 1:
            errors.append("相同产品 ID 的源记录存在内容冲突")
        if len(sources) > 1:
            warnings.append(f"产品 {product_id} 合并 {len(sources)} 条相同 ID 记录，保留全部来源。")
        retained = dict(raw)
        retained["_source_record_sha256"] = source_hash
        if len(sources) > 1:
            retained["_source_records_json"] = json.dumps(
                [{"record_number": number, "fields": item} for number, item, _ in sources],
                ensure_ascii=False,
            )
        product = Product(
            product_id=product_id,
            product_name=raw.get("产品名称", ""),
            institution=raw.get("机构名称", ""),
            detail_url=canonical_url,
            introduction=raw.get("产品介绍", ""),
            application_conditions=raw.get("申请条件", ""),
            applicable_customers=raw.get(CUSTOMER_COLUMN, ""),
            raw_fields=retained,
            source_rows=[number for number, _, _ in sources],
            availability_status="UNAVAILABLE" if errors else "SNAPSHOT",
            unavailable_reason="；".join(errors) if errors else None,
        )
        products.append(product)
    csv_digest = hashlib.sha256(csv_bytes).hexdigest()
    return Catalog(
        products=products,
        version=f"csv-sha256:{csv_digest};assessment:application-conditions-1.0",
        raw_record_count=len(records),
        warnings=warnings,
    )
