"""추출된 외부 견적을 ERPNext Supplier Quotation Draft로 등록한다.

로컬 JSON 저장은 필요하지 않다. ``Quotation`` 객체를 직접 받아 RFQ의 실제
품목/공급사 링크를 복사하고, 이후 ``get_supplier_quotations``에서 조회할 수
있도록 Supplier Quotation Item에 RFQ 연결 필드를 기록한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

try:
    from .quotation_models import Quotation, load_json
except ImportError:  # quotation_filter 폴더에서 직접 실행할 때
    from quotation_models import Quotation, load_json


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.append(str(BACKEND_ROOT))

from erp_client import ERPNextAPIError, erp_get, erp_get_one, erp_post  # noqa: E402


GetOne = Callable[[str, str], dict[str, Any] | None]
GetMany = Callable[..., list[dict[str, Any]] | None]
PostOne = Callable[[str, dict[str, Any]], dict[str, Any]]


class SupplierQuotationRegistrationError(RuntimeError):
    """ERP 등록 전에 발견된 RFQ 매핑·중복 충돌 오류."""


def _normalized_text(value: Any) -> str:
    return re.sub(r"[\s_\-/()]", "", str(value or "").casefold())


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _resolve_supplier(rfq: dict[str, Any], quotation: Quotation) -> str:
    suppliers = rfq.get("suppliers") or []
    if quotation.supplier_id:
        exact = [row for row in suppliers if row.get("supplier") == quotation.supplier_id]
        if len(exact) == 1:
            return str(exact[0]["supplier"])

    wanted = _normalized_text(quotation.supplier_name)
    matches = [
        row for row in suppliers
        if wanted and wanted in {
            _normalized_text(row.get("supplier")),
            _normalized_text(row.get("supplier_name")),
        }
    ]
    if len(matches) == 1 and matches[0].get("supplier"):
        return str(matches[0]["supplier"])
    if not matches:
        raise SupplierQuotationRegistrationError(
            f"공급사 '{quotation.supplier_name}'가 RFQ {quotation.rfq_name}의 공급사 목록에 없습니다."
        )
    raise SupplierQuotationRegistrationError(
        f"공급사 '{quotation.supplier_name}'가 RFQ에서 여러 건으로 매칭됩니다. supplier_id를 지정하세요."
    )


def _match_rfq_item(quotation_item: Any, rfq_items: list[dict[str, Any]]) -> dict[str, Any]:
    if quotation_item.item_code:
        exact = [row for row in rfq_items if row.get("item_code") == quotation_item.item_code]
        if len(exact) == 1:
            return exact[0]

    wanted = _normalized_text(quotation_item.item_name)
    matches = [row for row in rfq_items if wanted and wanted == _normalized_text(row.get("item_name"))]
    if len(matches) == 1:
        return matches[0]
    raise SupplierQuotationRegistrationError(
        f"견적 품목 '{quotation_item.item_code or quotation_item.item_name}'을 "
        "RFQ의 단일 품목과 연결할 수 없습니다."
    )


def _default_tax_row(
    company: str,
    tax_amount: Decimal,
    subtotal: Decimal,
    *,
    get_many: GetMany,
    get_one: GetOne,
) -> list[dict[str, Any]]:
    if tax_amount == 0:
        return []
    if subtotal <= 0:
        raise SupplierQuotationRegistrationError("공급가액이 0 이하라 세액을 ERPNext에 등록할 수 없습니다.")

    templates = get_many(
        "Purchase Taxes and Charges Template",
        filters=[["company", "=", company], ["is_default", "=", 1], ["disabled", "=", 0]],
        fields=["name"],
        limit=10,
    ) or []
    if len(templates) != 1:
        raise SupplierQuotationRegistrationError(
            f"회사 '{company}'의 기본 Purchase Taxes and Charges Template을 하나로 결정할 수 없습니다."
        )
    template = get_one("Purchase Taxes and Charges Template", str(templates[0]["name"])) or {}
    template_rows = template.get("taxes") or []
    if not template_rows or not template_rows[0].get("account_head"):
        raise SupplierQuotationRegistrationError("기본 매입세 템플릿에 세금 계정이 없습니다.")

    source = template_rows[0]
    rate = tax_amount / subtotal * Decimal("100")
    return [{
        "category": source.get("category") or "Total",
        "add_deduct_tax": source.get("add_deduct_tax") or "Add",
        "charge_type": "On Net Total",
        "account_head": source["account_head"],
        "description": source.get("description") or "매입세",
        "included_in_print_rate": source.get("included_in_print_rate") or 0,
        "rate": float(rate),
        "cost_center": source.get("cost_center"),
    }]


def build_supplier_quotation_payload(
    quotation_data: Quotation | dict[str, Any],
    *,
    get_one: GetOne | None = None,
    get_many: GetMany | None = None,
) -> dict[str, Any]:
    """RFQ 원본 링크를 사용해 ERPNext Supplier Quotation POST payload를 만든다."""
    quotation = (
        quotation_data
        if isinstance(quotation_data, Quotation)
        else Quotation.model_validate(quotation_data)
    )
    get_one = get_one or erp_get_one
    get_many = get_many or erp_get

    rfq = get_one("Request for Quotation", quotation.rfq_name)
    if not rfq:
        raise SupplierQuotationRegistrationError(f"RFQ '{quotation.rfq_name}'를 ERPNext에서 찾을 수 없습니다.")
    if int(rfq.get("docstatus") or 0) == 2:
        raise SupplierQuotationRegistrationError(f"RFQ '{quotation.rfq_name}'는 취소된 문서입니다.")

    company = rfq.get("company")
    if not company:
        raise SupplierQuotationRegistrationError(f"RFQ '{quotation.rfq_name}'에 company가 없습니다.")
    company_doc = get_one("Company", str(company)) or {}
    supplier = _resolve_supplier(rfq, quotation)
    rfq_items = rfq.get("items") or []

    item_payloads: list[dict[str, Any]] = []
    for quotation_item in quotation.items:
        rfq_item = _match_rfq_item(quotation_item, rfq_items)
        lead_time_days = quotation_item.lead_time_days
        if (
            lead_time_days is None
            and quotation_item.delivery_date
            and quotation.quotation_date
        ):
            lead_time_days = max((quotation_item.delivery_date - quotation.quotation_date).days, 0)

        row = {
            "item_code": rfq_item.get("item_code"),
            "item_name": rfq_item.get("item_name"),
            "description": quotation_item.raw_description or quotation_item.description or rfq_item.get("description"),
            "qty": float(quotation_item.quantity),
            "uom": rfq_item.get("uom"),
            "stock_uom": rfq_item.get("stock_uom"),
            "conversion_factor": rfq_item.get("conversion_factor") or 1,
            "warehouse": rfq_item.get("warehouse"),
            "rate": float(quotation_item.unit_price),
            "price_list_rate": float(quotation_item.unit_price),
            "lead_time_days": lead_time_days,
            "request_for_quotation": quotation.rfq_name,
            "request_for_quotation_item": rfq_item.get("name"),
            "material_request": rfq_item.get("material_request"),
            "material_request_item": rfq_item.get("material_request_item"),
        }
        item_payloads.append({key: value for key, value in row.items() if value is not None})

    taxes = _default_tax_row(
        str(company),
        quotation.tax_amount,
        quotation.subtotal,
        get_many=get_many,
        get_one=get_one,
    )
    currency = quotation.currency or company_doc.get("default_currency") or "KRW"
    payload: dict[str, Any] = {
        "supplier": supplier,
        "company": company,
        "transaction_date": (quotation.quotation_date or date.today()).isoformat(),
        "valid_till": quotation.valid_until.isoformat() if quotation.valid_until else None,
        "quotation_number": quotation.quotation_id,
        "currency": currency,
        "conversion_rate": 1,
        "price_list_currency": currency,
        "plc_conversion_rate": 1,
        "ignore_pricing_rule": 1,
        "cost_center": company_doc.get("cost_center"),
        "items": item_payloads,
        "taxes": taxes,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _fingerprint_document(document: dict[str, Any], rfq_name: str) -> tuple[Any, ...]:
    items = [
        (
            row.get("item_code"),
            _money(row.get("qty")),
            _money(row.get("rate")),
            _money(row.get("amount", _money(row.get("qty")) * _money(row.get("rate")))),
        )
        for row in document.get("items") or []
        if row.get("request_for_quotation", rfq_name) == rfq_name
    ]
    return (
        document.get("supplier"),
        document.get("currency") or "KRW",
        tuple(sorted(items)),
        _money(document.get("grand_total")),
    )


def _fingerprint_incoming(quotation: Quotation, supplier: str) -> tuple[Any, ...]:
    items = tuple(sorted(
        (
            row.item_code,
            _money(row.quantity),
            _money(row.unit_price),
            _money(row.amount),
        )
        for row in quotation.items
    ))
    return supplier, quotation.currency, items, _money(quotation.total_amount)


def register_supplier_quotation(
    quotation_data: Quotation | dict[str, Any],
    *,
    get_one: GetOne | None = None,
    get_many: GetMany | None = None,
    post_one: PostOne | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """동일 견적을 중복 생성하지 않고 Supplier Quotation Draft를 등록한다."""
    quotation = (
        quotation_data
        if isinstance(quotation_data, Quotation)
        else Quotation.model_validate(quotation_data)
    )
    get_one = get_one or erp_get_one
    get_many = get_many or erp_get
    post_one = post_one or erp_post
    payload = build_supplier_quotation_payload(quotation, get_one=get_one, get_many=get_many)

    summary_queries = [
        [["Supplier Quotation Item", "request_for_quotation", "=", quotation.rfq_name]],
        [["quotation_number", "=", quotation.quotation_id]],
        [["name", "=", quotation.quotation_id]],
    ]
    summaries_by_name: dict[str, dict[str, Any]] = {}
    for filters in summary_queries:
        for summary in get_many(
            "Supplier Quotation",
            filters=filters,
            fields=["name", "supplier", "quotation_number"],
            limit=500,
        ) or []:
            if summary.get("name"):
                summaries_by_name[str(summary["name"])] = summary
    summaries = list(summaries_by_name.values())
    incoming_fingerprint = _fingerprint_incoming(quotation, str(payload["supplier"]))
    for summary in summaries:
        if summary.get("supplier") != payload["supplier"]:
            continue
        detail = get_one("Supplier Quotation", str(summary["name"])) or {}
        same_external_number = (
            detail.get("quotation_number") == quotation.quotation_id
            or summary.get("quotation_number") == quotation.quotation_id
        )
        same_values = _fingerprint_document(detail, quotation.rfq_name) == incoming_fingerprint
        if same_external_number and not same_values:
            raise SupplierQuotationRegistrationError(
                f"외부 견적번호 '{quotation.quotation_id}'가 ERPNext 문서 {summary['name']}에 이미 있지만 금액이 다릅니다."
            )
        if same_external_number or same_values:
            return {
                "status": "already_exists",
                "name": summary["name"],
                "quotation_number": quotation.quotation_id,
                "rfq_name": quotation.rfq_name,
                "supplier": payload["supplier"],
            }

    if dry_run:
        return {
            "status": "dry_run",
            "quotation_number": quotation.quotation_id,
            "rfq_name": quotation.rfq_name,
            "supplier": payload["supplier"],
            "payload": payload,
        }

    created = post_one("Supplier Quotation", payload)
    return {
        "status": "created",
        "name": created.get("name"),
        "quotation_number": quotation.quotation_id,
        "rfq_name": quotation.rfq_name,
        "supplier": payload["supplier"],
        "docstatus": created.get("docstatus", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="추출 JSON을 ERPNext Supplier Quotation Draft로 등록")
    parser.add_argument("input", help="quotation_extractor 결과 JSON")
    parser.add_argument("--dry-run", action="store_true", help="ERP POST 없이 payload와 중복 여부만 확인")
    args = parser.parse_args()
    try:
        result = register_supplier_quotation(load_json(args.input), dry_run=args.dry_run)
    except (ERPNextAPIError, SupplierQuotationRegistrationError) as exc:
        parser.exit(1, f"ERPNext Supplier Quotation 등록 실패: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
