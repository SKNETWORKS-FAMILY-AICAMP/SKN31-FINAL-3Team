"""ERPNext Item 품목군을 공급사 검색 평가용 query JSON으로 내보낸다."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


EVALUATION_DIR = Path(__file__).resolve().parent
BACKEND_LOGIC2_DIR = EVALUATION_DIR.parent
if str(BACKEND_LOGIC2_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_LOGIC2_DIR))

from erp_client import erp_get  # noqa: E402


DEFAULT_OUTPUT = EVALUATION_DIR / "vendor_retrieval_queries.json"
DEFAULT_GROUPS = ("사무용품", "기계부품")
DEFAULT_RETIRED_GROUPS = ("시약 - 당류", "시약 - 무기염류")
DEFAULT_SELECTION_DESCRIPTION = "안전용품·사무용품·기계부품 대표 품목 각 5개 평가"
DEFAULT_PILOT_CODES = (
    "OFC-BRD-001",
    "OFC-CLP-001",
    "OFC-ERS-011",
    "OFC-PAP-001",
    "OFC-PEN-001",
    "BLT-TMG-5M-500",
    "BRG-6004-2RS",
    "CHN-RS40-1R",
    "FST-HEX-M08025",
    "PNE-POC-0802",
)
ITEM_FIELDS = (
    "item_code",
    "item_name",
    "description",
    "item_group",
    "stock_uom",
    "is_stock_item",
    "include_item_in_manufacturing",
    "disabled",
    "modified",
)


def export_queries(
    output_path: Path = DEFAULT_OUTPUT,
    groups: tuple[str, ...] = DEFAULT_GROUPS,
    pilot_codes: tuple[str, ...] | None = DEFAULT_PILOT_CODES,
    source_path: Path | None = None,
    remove_groups: tuple[str, ...] = (),
) -> dict:
    existing = {}
    if output_path.exists():
        with output_path.open("r", encoding="utf-8") as file:
            existing = json.load(file)

    fetched_items = []
    group_counts = {}

    if source_path:
        with source_path.open("r", encoding="utf-8") as file:
            source_data = json.load(file)
        source_items = source_data.get("items") or []
        for group in groups:
            rows = [item for item in source_items if item.get("item_group") == group]
            group_counts[group] = len(rows)
            fetched_items.extend(dict(row) for row in rows)
    else:
        for group in groups:
            rows = erp_get(
                "Item",
                filters=[["item_group", "=", group]],
                fields=list(ITEM_FIELDS),
                order_by="item_code asc",
                limit=500,
            ) or []
            group_counts[group] = len(rows)
            fetched_items.extend({field: row.get(field) for field in ITEM_FIELDS} for row in rows)

    for item in fetched_items:
        item["enabled"] = not bool(item.get("disabled"))

    available_codes = {item["item_code"] for item in fetched_items}
    missing_pilot_codes = [
        code for code in (pilot_codes or ())
        if code not in available_codes
    ]
    if missing_pilot_codes:
        raise ValueError(
            "ERPNext에서 pilot 품목을 찾을 수 없습니다: "
            + ", ".join(missing_pilot_codes)
        )

    target_groups = set(groups) | set(remove_groups)
    preserved_items = [
        item for item in existing.get("items") or []
        if item.get("item_group") not in target_groups
    ]
    items = preserved_items + fetched_items
    if len({item.get("item_code") for item in items}) != len(items):
        raise ValueError("병합 결과에 중복 item_code가 있습니다.")

    previous_selection = existing.get("evaluation_selection") or {}
    if pilot_codes is None:
        selected_codes = list(previous_selection.get("item_codes") or [])
    elif not pilot_codes:
        selected_codes = []
    else:
        existing_group_by_code = {
            item.get("item_code"): item.get("item_group")
            for item in existing.get("items") or []
        }
        preserved_selection = [
            code
            for code in previous_selection.get("item_codes") or []
            if existing_group_by_code.get(code) not in target_groups
        ]
        selected_codes = list(dict.fromkeys(
            preserved_selection + list(pilot_codes)
        ))

    result = dict(existing)
    for deprecated_key in (
        "erpnext_data_import",
        "erpnext_item_alternative_import",
        "erpnext_stock_reconciliation_import",
    ):
        result.pop(deprecated_key, None)
    result.update({
        "schema_version": max(int(existing.get("schema_version") or 0), 2),
        "description": "ERPNext Item을 사용하는 공급사 검색 평가 품목 목록",
        "erpnext_evaluation_export": {
            "system": "ERPNext",
            "doctype": "Item",
            "item_groups": list(groups),
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "group_counts": group_counts,
        },
        "evaluation_selection": {
            "mode": (
                previous_selection.get("mode", "selected")
                if pilot_codes is None else
                "all_enabled" if not pilot_codes else "multi_group_pilot"
            ),
            "description": (
                previous_selection.get("description", "선택 품목 평가")
                if pilot_codes is None else
                "전체 활성 품목 평가" if not pilot_codes else
                DEFAULT_SELECTION_DESCRIPTION
                if tuple(groups) == DEFAULT_GROUPS else
                "기존 pilot과 ERP 조회 품목군 대표 품목 평가"
            ),
            "item_codes": selected_codes,
            "label_depth": int(previous_selection.get("label_depth") or 5),
        },
        "items": items,
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print(
        f"ERPNext 평가 Query JSON 병합: 기존 {len(preserved_items)}건 + "
        f"조회 {len(fetched_items)}건 = {len(items)}건 → {output_path}"
    )
    for group, count in group_counts.items():
        print(f"  - {group}: {count}건")
    print(f"  - 전체 평가 선택: {len(selected_codes) or len(items)}건")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--source-json",
        type=Path,
        help="ERP 재조회 대신 이전에 내보낸 query JSON의 품목을 재사용",
    )
    parser.add_argument("--groups", nargs="+", default=list(DEFAULT_GROUPS))
    parser.add_argument(
        "--remove-groups",
        nargs="+",
        help="새로 조회하지 않고 기존 query JSON에서 제거할 이전 평가 품목군",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="evaluation_selection.item_codes를 비워 전체 활성 품목을 평가",
    )
    parser.add_argument(
        "--pilot-codes",
        nargs="+",
        help="기존 선택에 추가할 대표 item_code. 미지정 시 기존 선택 보존",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.all:
        pilot_codes = ()
    elif args.pilot_codes:
        pilot_codes = tuple(args.pilot_codes)
    elif tuple(args.groups) == DEFAULT_GROUPS:
        pilot_codes = DEFAULT_PILOT_CODES
    else:
        pilot_codes = None
    remove_groups = (
        tuple(args.remove_groups)
        if args.remove_groups is not None else
        DEFAULT_RETIRED_GROUPS if tuple(args.groups) == DEFAULT_GROUPS else ()
    )
    export_queries(
        args.output,
        tuple(args.groups),
        pilot_codes,
        args.source_json,
        remove_groups=remove_groups,
    )


if __name__ == "__main__":
    main()
