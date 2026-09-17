"""추출된 외부 견적을 ERPNext Supplier Quotation Draft로 등록한다.

로컬 JSON 저장은 필요하지 않다. ``Quotation`` 객체를 직접 받아 RFQ의 실제
품목/공급사 링크를 복사하고, 이후 ``get_supplier_quotations``에서 조회할 수
있도록 Supplier Quotation Item에 RFQ 연결 필드를 기록한다.
"""

from __future__ import annotations

import argparse
import html
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
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import Quotation, load_json


BACKEND_ROOT = Path(__file__).resolve().parents[4]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.append(str(BACKEND_ROOT))

from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_get, erp_get_one, erp_post, erp_submit  # noqa: E402


GetOne = Callable[[str, str], dict[str, Any] | None]
GetMany = Callable[..., list[dict[str, Any]] | None]
PostOne = Callable[[str, dict[str, Any]], dict[str, Any]]
FingerprintItem = tuple[str, Decimal, Decimal, Decimal]
QuotationFingerprint = tuple[str, str, tuple[FingerprintItem, ...], Decimal]


class SupplierQuotationRegistrationError(RuntimeError):
    """ERP 등록 전에 발견된 RFQ 매핑·중복 충돌 오류."""


class QuotationArithmeticValidationError(SupplierQuotationRegistrationError):
    """Extracted quotation amounts fail deterministic arithmetic checks."""


def _notes_to_terms(notes: str | None) -> str | None:
    """Render extracted plain-text notes safely in ERPNext's rich-text field."""

    normalized = str(notes or "").strip()
    if not normalized:
        return None
    return html.escape(normalized, quote=True).replace("\r\n", "\n").replace(
        "\r", "\n"
    ).replace("\n", "<br>")


def submit_finalized_quotations(rfq_name: str, ranking: list[dict[str, Any]]) -> list[str]:
    """최종 순위에 포함된 RFQ 견적을 제출해 이후 수정을 막는다."""
    quotation_names = {
        str(row.get("name") or row.get("quotation_id") or "").strip()
        for row in ranking
        if str(row.get("name") or row.get("quotation_id") or "").strip()
    }
    if not quotation_names:
        raise ValueError("확정할 Supplier Quotation 문서명이 없습니다.")

    submitted: list[str] = []
    for quotation_name in sorted(quotation_names):
        quotation = erp_get_one("Supplier Quotation", quotation_name)
        if not quotation:
            raise ValueError(f"Supplier Quotation을 찾을 수 없습니다: {quotation_name}")
        if not any(
            item.get("request_for_quotation") == rfq_name
            for item in quotation.get("items") or []
        ):
            raise ValueError(f"{quotation_name}은(는) RFQ {rfq_name}에 연결된 견적이 아닙니다.")
        docstatus = int(quotation.get("docstatus") or 0)
        if docstatus == 0:
            erp_submit("Supplier Quotation", quotation_name)
        elif docstatus != 1:
            raise ValueError(f"확정할 수 없는 견적 상태입니다: {quotation_name}")
        submitted.append(quotation_name)
    return submitted


def _normalized_text(value: Any) -> str:
    return re.sub(r"[\s_\-/()]", "", str(value or "").casefold())


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def validate_quotation_arithmetic(
    quotation_data: Quotation | dict[str, Any],
) -> Quotation:
    """Validate extracted amounts without recalculating or replacing them."""

    quotation = (
        quotation_data
        if isinstance(quotation_data, Quotation)
        else Quotation.model_validate(quotation_data)
    )
    errors: list[str] = []
    item_sum = Decimal("0")
    for index, item in enumerate(quotation.items):
        calculated = _money(item.quantity * item.unit_price)
        stated = _money(item.amount)
        if calculated != stated:
            errors.append(
                f"items[{index}]: quantity({item.quantity}) × "
                f"unit_price({item.unit_price})={calculated}, amount={stated}"
            )
        item_sum += item.amount

    stated_subtotal = _money(quotation.subtotal)
    calculated_subtotal = _money(item_sum)
    if calculated_subtotal != stated_subtotal:
        errors.append(
            f"subtotal: item sum={calculated_subtotal}, stated={stated_subtotal}"
        )

    calculated_total = _money(quotation.subtotal + quotation.tax_amount)
    stated_total = _money(quotation.total_amount)
    if calculated_total != stated_total:
        errors.append(
            f"total: subtotal+tax={calculated_total}, stated={stated_total}"
        )

    if errors:
        raise QuotationArithmeticValidationError(
            "견적 금액 산술 검증에 실패하여 자동 등록하지 않습니다: "
            + "; ".join(errors)
        )
    return quotation


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


def _match_rfq_item(
    quotation_item: Any,
    rfq_items: list[dict[str, Any]],
    *,
    allow_single_item_fallback: bool = False,
) -> dict[str, Any]:
    if quotation_item.item_code:
        exact = [row for row in rfq_items if row.get("item_code") == quotation_item.item_code]
        if len(exact) == 1:
            return exact[0]

    wanted = _normalized_text(quotation_item.item_name)
    matches = [row for row in rfq_items if wanted and wanted == _normalized_text(row.get("item_name"))]
    if len(matches) == 1:
        return matches[0]

    # 소형 비전 모델이 표의 Sr 열("1", "2"...)을 item_code로 오인하는 경우가 있다.
    # 실제 RFQ item_code가 품목명/설명 원문에 명시된 경우에만 복구한다.
    evidence = _normalized_text(" ".join(filter(None, [
        quotation_item.item_code,
        quotation_item.item_name,
        quotation_item.description,
        quotation_item.raw_description,
    ])))
    code_matches = [
        row for row in rfq_items
        if _normalized_text(row.get("item_code"))
        and _normalized_text(row.get("item_code")) in evidence
    ]
    if len(code_matches) == 1:
        return code_matches[0]
    # 외부 공급업체는 구매사 ERP의 내부 item_code를 알 수 없다. RFQ와
    # 견적서가 모두 단일 품목인 경우에는 대응 관계가 유일하므로 공급업체의
    # 자체 품목 코드/표기가 달라도 RFQ 품목에 연결한다. 규격 적합성은 등록
    # 이후 reviewer/spec evaluator가 별도로 판정한다.
    if allow_single_item_fallback and len(rfq_items) == 1:
        return rfq_items[0]
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
    charge_type = str(source.get("charge_type") or "On Net Total")
    if charge_type != "On Net Total":
        raise SupplierQuotationRegistrationError(
            "기본 매입세 템플릿의 첫 행이 'On Net Total' 방식이 아닙니다."
        )
    try:
        official_rate = Decimal(str(source.get("rate")))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise SupplierQuotationRegistrationError(
            "기본 매입세 템플릿에 유효한 공식 세율이 없습니다."
        ) from exc
    if official_rate < 0:
        raise SupplierQuotationRegistrationError("기본 매입세 템플릿 세율은 0 이상이어야 합니다.")

    expected_tax = _money(subtotal * official_rate / Decimal("100"))
    stated_tax = _money(tax_amount)
    # ERP/통화별 최소 단위 반올림 차이는 허용하지만, 견적 금액을 역산해
    # 9.8%, 10.3% 같은 새로운 세율을 만들어 저장하지 않는다.
    if abs(stated_tax - expected_tax) > Decimal("1"):
        raise SupplierQuotationRegistrationError(
            "견적 세액이 기본 매입세 템플릿과 일치하지 않습니다: "
            f"기재 세액={stated_tax}, 공식 세율={official_rate}%, 예상 세액={expected_tax}"
        )
    return [{
        "category": source.get("category") or "Total",
        "add_deduct_tax": source.get("add_deduct_tax") or "Add",
        "charge_type": charge_type,
        "account_head": source["account_head"],
        "description": source.get("description") or "매입세",
        "included_in_print_rate": source.get("included_in_print_rate") or 0,
        # Decimal을 float로 바꾸지 않아 JSON 직렬화 전 이진 부동소수 오차를 피한다.
        # Frappe의 Percent 필드는 고정소수 문자열을 숫자로 파싱한다.
        "rate": format(official_rate, "f"),
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
    validate_quotation_arithmetic(quotation)
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
    allow_single_item_fallback = len(rfq_items) == 1 and len(quotation.items) == 1
    for quotation_item in quotation.items:
        rfq_item = _match_rfq_item(
            quotation_item,
            rfq_items,
            allow_single_item_fallback=allow_single_item_fallback,
        )
        lead_time_days = quotation_item.lead_time_days
        if (
            lead_time_days is None
            and quotation_item.expected_delivery_date
            and quotation.quotation_date
        ):
            lead_time_days = max(
                (quotation_item.expected_delivery_date - quotation.quotation_date).days,
                0,
            )

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
            # 프로젝트 ERPNext에 추가된 실제 견적 납기 필드. lead_time_days도
            # 함께 유지해 표준 ERPNext와 기존 평가 모듈 모두 호환한다.
            "expected_delivery_date": (
                quotation_item.expected_delivery_date.isoformat()
                if quotation_item.expected_delivery_date
                else None
            ),
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
    currency = quotation.currency
    company_currency = str(company_doc.get("default_currency") or "KRW").upper()
    payload: dict[str, Any] = {
        "supplier": supplier,
        "company": company,
        "transaction_date": (quotation.quotation_date or date.today()).isoformat(),
        "valid_till": quotation.valid_until.isoformat() if quotation.valid_until else None,
        "quotation_number": quotation.quotation_id,
        "currency": currency,
        # 회사 기준통화와 같을 때만 1이다. 외화는 ERPNext가 거래일의 환율을
        # 조회하도록 생략하며, 조회할 수 없으면 잘못된 1로 저장하는 대신 등록이 실패한다.
        "conversion_rate": 1 if currency.upper() == company_currency else None,
        "price_list_currency": currency,
        "plc_conversion_rate": 1,
        "ignore_pricing_rule": 1,
        "cost_center": company_doc.get("cost_center"),
        # RFQ 포털의 Notes 입력란도 Supplier Quotation.terms에 저장된다.
        # 외부 견적에서 추출한 notes를 같은 필드에 기록해 두 경로를 통일한다.
        "terms": _notes_to_terms(quotation.notes),
        "items": item_payloads,
        "taxes": taxes,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _fingerprint_items(rows: list[dict[str, Any]]) -> tuple[FingerprintItem, ...]:
    """품목 순서와 nullable item_code에 영향받지 않는 비교 키를 만든다."""
    items: list[FingerprintItem] = []
    for row in rows:
        quantity = _money(row.get("qty"))
        rate = _money(row.get("rate"))
        raw_amount = row.get("amount")
        amount = _money(raw_amount) if raw_amount is not None else _money(quantity * rate)
        items.append((
            str(row.get("item_code") or ""),
            quantity,
            rate,
            amount,
        ))
    return tuple(sorted(items))


def _fingerprint_document(document: dict[str, Any], rfq_name: str) -> QuotationFingerprint:
    items = [
        row
        for row in document.get("items") or []
        if row.get("request_for_quotation", rfq_name) == rfq_name
    ]
    return (
        str(document.get("supplier") or ""),
        str(document.get("currency") or "KRW").upper(),
        _fingerprint_items(items),
        _money(document.get("grand_total")),
    )


def _fingerprint_incoming(
    quotation: Quotation,
    payload: dict[str, Any],
) -> QuotationFingerprint:
    # 등록 payload에는 공급사 자체 코드가 RFQ의 ERP item_code로 매핑되어 있다.
    # 기존 ERP 문서와 같은 코드 체계로 비교해야 동일 견적을 정확히 찾을 수 있다.
    return (
        str(payload.get("supplier") or ""),
        str(payload.get("currency") or quotation.currency or "KRW").upper(),
        _fingerprint_items(payload.get("items") or []),
        _money(quotation.total_amount),
    )


def _document_references_rfq(document: dict[str, Any], rfq_name: str) -> bool:
    """Return whether an existing Supplier Quotation belongs to this RFQ.

    Supplier-provided quotation numbers are not globally unique.  Duplicate
    detection must therefore be scoped to the trusted RFQ and supplier pair.
    """
    return any(
        str(row.get("request_for_quotation") or "") == rfq_name
        for row in document.get("items") or []
        if isinstance(row, dict)
    )


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
    incoming_fingerprint = _fingerprint_incoming(quotation, payload)
    for summary in summaries:
        if summary.get("supplier") != payload["supplier"]:
            continue
        detail = get_one("Supplier Quotation", str(summary["name"])) or {}
        if not _document_references_rfq(detail, quotation.rfq_name):
            continue
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
