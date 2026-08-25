"""vendor_retrieval_queries.json을 ERPNext Item Data Import CSV로 변환."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SOURCE_PATH = BASE_DIR / "vendor_retrieval_queries.json"
ITEM_CODE_PATTERN = re.compile(r"^[A-Z]{3}-[A-Z]{3}-\d{3}$")

# 현재 ERPNext Item DocType에서 조회한 실제 필드 라벨이다.
CSV_COLUMNS = [
    ("ID", None),
    ("Item Code", "item_code"),
    ("Item Name", "item_name"),
    ("Item Group", "item_group"),
    ("Default Unit of Measure", "stock_uom"),
    ("Maintain Stock", "is_stock_item"),
    ("Include Item In Manufacturing", "include_item_in_manufacturing"),
    ("Disabled", "disabled"),
    ("Description", "description"),
]

ALTERNATIVE_CSV_COLUMNS = [
    ("ID", None),
    ("Item Code", "alternative_for"),
    ("Alternative Item Code", "item_code"),
    ("Two-way", "two_way_alternative"),
]

STOCK_RECONCILIATION_CSV_COLUMNS = [
    ("Item Code", "item_code"),
    ("Warehouse", "warehouse"),
    ("Quantity", "opening_qty"),
    ("Valuation Rate", "valuation_rate"),
]


def validate_items(items: list[dict]) -> None:
    seen_codes = set()
    required_fields = {field for _, field in CSV_COLUMNS if field is not None}
    for index, item in enumerate(items, start=1):
        missing = sorted(field for field in required_fields if field not in item)
        if missing:
            raise ValueError(f"items[{index}] 필수 필드 누락: {', '.join(missing)}")

        item_code = item["item_code"]
        if not ITEM_CODE_PATTERN.fullmatch(item_code):
            raise ValueError(f"item_code 형식 오류: {item_code} (예: SAF-HLM-001)")
        if item_code in seen_codes:
            raise ValueError(f"중복 item_code: {item_code}")
        seen_codes.add(item_code)

        if len(item["item_name"].strip()) < 5:
            raise ValueError(f"품목명이 지나치게 짧습니다: {item_code}")
        if "규격:" not in item["description"]:
            raise ValueError(f"description에 '규격:'이 없습니다: {item_code}")
        for field in ("is_stock_item", "include_item_in_manufacturing", "disabled"):
            if item[field] not in (0, 1):
                raise ValueError(f"{item_code}.{field}는 0 또는 1이어야 합니다")
        if item.get("opening_qty", 0) <= 0:
            raise ValueError(f"{item_code}.opening_qty는 0보다 커야 합니다")
        if item.get("valuation_rate", 0) <= 0:
            raise ValueError(f"{item_code}.valuation_rate는 0보다 커야 합니다")

    quantities = [item["opening_qty"] for item in items]
    if len(quantities) != len(set(quantities)):
        raise ValueError("품목별 opening_qty는 서로 달라야 합니다")

    all_codes = {item["item_code"] for item in items}
    for item in items:
        target = item.get("alternative_for")
        if not target:
            continue
        if target == item["item_code"]:
            raise ValueError(f"자기 자신을 대체품으로 연결할 수 없습니다: {target}")
        if target not in all_codes:
            raise ValueError(f"대체 대상 item_code가 품목 목록에 없습니다: {target}")
        if item.get("two_way_alternative") not in (0, 1):
            raise ValueError(f"{item['item_code']}.two_way_alternative는 0 또는 1이어야 합니다")


def build_csv(source_path: Path = SOURCE_PATH) -> Path:
    with source_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    config = data["erpnext_data_import"]
    if config.get("doctype") != "Item":
        raise ValueError("erpnext_data_import.doctype은 Item이어야 합니다")

    items = [item for item in data.get("items") or [] if item.get("enabled", True)]
    validate_items(items)
    output_path = source_path.parent / config["csv_file"]

    # BOM을 포함한 UTF-8로 저장해 Windows Excel/ERPNext의 한글 깨짐을 줄인다.
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([label for label, _ in CSV_COLUMNS])
        for item in items:
            writer.writerow(["" if field is None else item[field] for _, field in CSV_COLUMNS])

    print(f"ERPNext Item Import CSV 생성: {len(items)}건 → {output_path}")

    alternative_config = data["erpnext_item_alternative_import"]
    alternative_items = [item for item in items if item.get("alternative_for")]
    alternative_output_path = source_path.parent / alternative_config["csv_file"]
    with alternative_output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([label for label, _ in ALTERNATIVE_CSV_COLUMNS])
        for item in alternative_items:
            writer.writerow([
                "" if field is None else item[field]
                for _, field in ALTERNATIVE_CSV_COLUMNS
            ])

    print(
        f"ERPNext Item Alternative Import CSV 생성: {len(alternative_items)}건 "
        f"→ {alternative_output_path}"
    )

    stock_config = data["erpnext_stock_reconciliation_import"]
    if stock_config.get("doctype") != "Stock Reconciliation":
        raise ValueError(
            "erpnext_stock_reconciliation_import.doctype은 Stock Reconciliation이어야 합니다"
        )
    stock_output_path = source_path.parent / stock_config["csv_file"]
    with stock_output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([label for label, _ in STOCK_RECONCILIATION_CSV_COLUMNS])
        for item in items:
            row = []
            for _, field in STOCK_RECONCILIATION_CSV_COLUMNS:
                if field == "warehouse":
                    row.append(stock_config["warehouse"])
                else:
                    row.append(item[field])
            writer.writerow(row)

    print(
        f"ERPNext Stock Reconciliation Import CSV 생성: {len(items)}건 "
        f"→ {stock_output_path}"
    )
    return output_path


if __name__ == "__main__":
    build_csv()
