"""ERPNext Item 문서를 프론트용 동적 규격 컬럼으로 변환한다.

운영 기준은 ``procurement.item_group_spec`` 또는 현재 배포된
``procurement.item_group_spec_requirements``의 품목군별 필수 규격이다.
ERPNext Item DocType 메타데이터를 사용해 실제 표준·custom 필드의 라벨과
섹션을 보존하고, Item Attribute 자식행도 같은 응답에 합친다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import unescape
import json
import re
from typing import Any

from .connection import get_connection


def get_item_group_spec(item_group: str | None) -> Any | None:
    """품목군 규격 정의를 현재 배포된 두 물리 스키마에서 읽는다."""
    if not item_group:
        return None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    to_regclass('procurement.item_group_spec_requirements') IS NOT NULL
                        AS has_requirements,
                    to_regclass('procurement.item_group_spec') IS NOT NULL
                        AS has_legacy
                """
            )
            table_row = cursor.fetchone() or {}
            # 새 검증기가 쓰는 requirements 테이블을 우선하되, 해당 품목군
            # 행이 아직 없으면 과거 item_group_spec 데이터까지 조회한다.
            table_names = []
            if table_row.get("has_requirements"):
                table_names.append("item_group_spec_requirements")
            if table_row.get("has_legacy"):
                table_names.append("item_group_spec")
            row = None
            for table_name in table_names:
                cursor.execute(
                    f"SELECT required_specs FROM procurement.{table_name} WHERE item_group = %s",
                    (item_group,),
                )
                row = cursor.fetchone()
                if row:
                    break
    if not row:
        return None
    value = row["required_specs"]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _humanize(fieldname: str) -> str:
    return fieldname.removeprefix("custom_").replace("_", " ").strip().title()


def _normalize_definition(fieldname: str, metadata: Any, order: int) -> dict[str, Any]:
    if isinstance(metadata, str):
        metadata = {"label": metadata}
    elif not isinstance(metadata, Mapping):
        metadata = {}

    return {
        "fieldname": fieldname,
        "label": metadata.get("label") or _humanize(fieldname),
        "fieldtype": metadata.get("fieldtype") or metadata.get("value_type") or "text",
        "unit": metadata.get("unit"),
        "section": metadata.get("section") or metadata.get("group") or "상세 규격",
        "display_order": metadata.get("display_order", metadata.get("order", order)),
        "required": bool(metadata.get("required", False)),
    }


def normalize_required_specs(required_specs: Any) -> list[dict[str, Any]]:
    """문자열 목록·객체 목록·fieldname 매핑 형식을 모두 표준화한다."""
    if isinstance(required_specs, Mapping) and isinstance(required_specs.get("fields"), Sequence):
        required_specs = required_specs["fields"]

    if isinstance(required_specs, Mapping):
        return [
            _normalize_definition(str(fieldname), metadata, index * 10)
            for index, (fieldname, metadata) in enumerate(required_specs.items(), start=1)
        ]

    if not isinstance(required_specs, Sequence) or isinstance(required_specs, (str, bytes)):
        return []

    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(required_specs, start=1):
        if isinstance(entry, str):
            normalized.append(_normalize_definition(entry, {}, index * 10))
            continue
        if not isinstance(entry, Mapping):
            continue
        fieldname = entry.get("fieldname") or entry.get("field_name") or entry.get("key")
        if fieldname:
            normalized.append(_normalize_definition(str(fieldname), entry, index * 10))
    return normalized


_LAYOUT_FIELD_TYPES = {
    "Section Break", "Column Break", "Tab Break", "Fold", "HTML", "Button",
}
_TABLE_FIELD_TYPES = {"Table", "Table MultiSelect"}

# The Item DocType contains accounting, sales, stock, batch and manufacturing
# controls. They are useful ERP settings, but they are not product
# specifications and made the frontend modal look like a raw DocType dump.
# The request department is already rendered in its own table column.
_NON_SPEC_CUSTOM_FIELDS = {
    "custom_request_department",
    "custom_requester",
    "custom_buyer",
    "custom_approval_status",
    "custom_workflow_status",
}


def _is_visible_custom_spec(fieldname: str, value: Any) -> bool:
    return (
        fieldname.startswith("custom_")
        and fieldname not in _NON_SPEC_CUSTOM_FIELDS
        and value not in (None, "", False)
        and not isinstance(value, (dict, list))
    )


def _frontend_fieldtype(fieldtype: str | None) -> str:
    if fieldtype == "Check":
        return "boolean"
    if fieldtype in {"Int", "Float", "Currency", "Percent"}:
        return "number"
    if fieldtype in {"Date", "Datetime", "Time"}:
        return "date"
    if fieldtype in {"Link", "Dynamic Link"}:
        return "link"
    if fieldtype == "Select":
        return "select"
    return "text"


def _display_value(value: Any, fieldtype: str | None) -> Any:
    if fieldtype == "Check":
        return bool(value)
    if isinstance(value, str) and fieldtype in {"HTML", "Text Editor"}:
        return unescape(re.sub(r"<[^>]+>", " ", value)).strip()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _metadata_definitions(
    item: Mapping[str, Any], metadata_fields: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Return human-facing specifications, not every Item control field.

    ERPNext's ``description`` is the canonical compact specification used in
    the list and RFQ flow. Custom scalar fields and Item Attributes extend it
    when a category needs structured values. Standard accounting/inventory
    settings remain available from ERPNext but are deliberately excluded here.
    """
    if not metadata_fields:
        return []

    definitions: list[dict[str, Any]] = []
    section = "기본 규격"
    order = 0
    for metadata in metadata_fields:
        fieldtype = str(metadata.get("fieldtype") or "Data")
        label = metadata.get("label")
        if fieldtype in {"Tab Break", "Section Break"}:
            if label:
                section = str(label)
            continue
        if fieldtype in _LAYOUT_FIELD_TYPES or fieldtype in _TABLE_FIELD_TYPES:
            continue
        if metadata.get("hidden"):
            continue
        fieldname = str(metadata.get("fieldname") or "").strip()
        if not fieldname or fieldname not in item:
            continue
        value = item.get(fieldname)
        if fieldname == "description":
            if value in (None, ""):
                continue
            label = "규격"
            section = "기본 규격"
        elif not _is_visible_custom_spec(fieldname, value):
            continue
        order += 10
        definitions.append(
            {
                "fieldname": fieldname,
                "label": label or _humanize(fieldname),
                "fieldtype": _frontend_fieldtype(fieldtype),
                "unit": None,
                "section": section,
                "display_order": order,
                "required": bool(metadata.get("reqd")),
                "value": _display_value(value, fieldtype),
            }
        )
    return definitions


def _fallback_definitions(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for fieldname, value in item.items():
        if _is_visible_custom_spec(fieldname, value):
            definitions.append(_normalize_definition(fieldname, {}, (len(definitions) + 1) * 10))

    description = item.get("description")
    if description not in (None, ""):
        definitions.insert(
            0,
            {
                **_normalize_definition(
                    "description",
                    {"label": "규격", "section": "기본 규격", "fieldtype": "text"},
                    10,
                ),
                "attribute_value": _display_value(description, "Text Editor"),
            },
        )

    for attribute in item.get("attributes") or []:
        if not isinstance(attribute, Mapping):
            continue
        label = attribute.get("attribute")
        if not label:
            continue
        fieldname = f"attribute_{str(label).strip().lower().replace(' ', '_')}"
        definitions.append(
            {
                **_normalize_definition(fieldname, {"label": label, "section": "Item Attributes"}, (len(definitions) + 1) * 10),
                "attribute_value": attribute.get("attribute_value"),
            }
        )
    return definitions


def build_item_specification_response(
    item: Mapping[str, Any],
    required_specs: Any | None,
    metadata_fields: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """ERP Item 원문과 규격 정의를 프론트 API 계약으로 변환한다."""
    required_definitions = normalize_required_specs(required_specs)
    definitions = _metadata_definitions(item, metadata_fields)
    schema_source = "erp_doctype_metadata" if definitions else "erp_custom_fields_fallback"
    if not definitions:
        definitions = _fallback_definitions(item)

    # Item Attributes are child rows and therefore absent from scalar metadata.
    existing_names = {definition["fieldname"] for definition in definitions}
    for fallback in _fallback_definitions(item):
        if fallback["fieldname"] not in existing_names:
            definitions.append(fallback)
            existing_names.add(fallback["fieldname"])

    # Requirements can contain ERP fieldnames or human labels. Matching live
    # fields become required; unmatched requirements remain visible as missing.
    by_name = {definition["fieldname"].casefold(): definition for definition in definitions}
    by_label = {str(definition["label"]).casefold(): definition for definition in definitions}
    for requirement in required_definitions:
        match = by_name.get(requirement["fieldname"].casefold()) or by_label.get(
            str(requirement["label"]).casefold()
        )
        if match:
            match["required"] = True
            match["unit"] = requirement.get("unit") or match.get("unit")
            continue
        requirement = dict(requirement)
        requirement["required"] = True
        requirement["value"] = item.get(requirement["fieldname"])
        requirement["display_order"] = max(
            [definition.get("display_order", 0) for definition in definitions] or [0]
        ) + 10
        definitions.append(requirement)

    if required_definitions:
        schema_source = (
            f"{schema_source}+item_group_spec"
            if metadata_fields
            else "item_group_spec"
        )

    fields = []
    for definition in definitions:
        fieldname = definition["fieldname"]
        value = definition.get("value", definition.get("attribute_value", item.get(fieldname)))
        fields.append({
            key: value
            for key, value in definition.items()
            if key not in {"attribute_value", "value"}
        } | {"value": value})

    missing_required_fields = [
        field["fieldname"]
        for field in fields
        if field["required"] and field["value"] in (None, "")
    ]
    fields.sort(key=lambda field: field["display_order"])

    return {
        "item_code": item.get("item_code") or item.get("name"),
        "item_name": item.get("item_name") or item.get("item_code") or item.get("name"),
        "item_group": item.get("item_group"),
        "department": item.get("custom_request_department"),
        "stock_uom": item.get("stock_uom"),
        "description": item.get("description"),
        "maintain_stock": bool(item.get("is_stock_item")),
        "is_fixed_asset": bool(item.get("is_fixed_asset")),
        "registered_date": item.get("creation"),
        "schema_source": schema_source,
        "specification_fields": fields,
        "missing_required_fields": missing_required_fields,
    }
