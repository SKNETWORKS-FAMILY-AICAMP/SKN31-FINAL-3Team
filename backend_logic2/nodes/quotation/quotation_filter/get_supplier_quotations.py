"""RFQ에 연결된 모든 ERPNext Supplier Quotation을 조회·정규화한다.

포털 입력 견적과 외부 파일에서 추출 후 등록한 견적을 구분하지 않는다.
품목 단위 평탄화 결과와 review/ranker 공통 ``Quotation`` 모델을 모두 제공한다.
이 모듈에서는 LLM을 사용하지 않는다.


"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable

from backend_logic2.integrations.erp_client import erp_get, erp_get_one

from .quotation_models import Quotation
from .quotation_reviewer import extract_specifications
from .quotation_terms import parse_separated_terms


GetOne = Callable[[str, str], dict[str, Any] | None]
GetMany = Callable[..., list[dict[str, Any]] | None]


def _number(value: Any) -> float:
    """ERPNext 숫자/문자열 값을 UI 투영에 안전한 숫자로 바꾼다."""
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _first_non_zero(*values: Any) -> float:
    for value in values:
        number = _number(value)
        if number:
            return number
    return 0.0


class _TextExtractor(HTMLParser):
    """ERPNext Rich Text 필드에서 표시 텍스트만 안전하게 꺼낸다."""

    BLOCK_TAGS = {"br", "div", "li", "p", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str | None:
    """HTML 또는 일반 문자열을 줄바꿈이 정리된 평문으로 변환한다."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parser = _TextExtractor()
    parser.feed(unescape(raw))
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def _parse_json_object(value: Any) -> dict[str, Any]:
    """ERPNext가 문자열로 저장한 JSON 필드를 dict로 변환한다."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _unique_quotation_names(rows: list[dict[str, Any]]) -> list[str]:
    """자식 테이블 필터가 같은 부모를 여러 번 반환해도 한 번만 조회한다."""
    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        name = row.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _normalize_item(detail: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    transaction_date = detail.get("transaction_date")
    lead_time_days = item.get("lead_time_days")
    expected_delivery_date = item.get("expected_delivery_date")
    return {
        # Supplier Quotation 헤더
        "quotation_name": detail.get("name"),
        "external_quotation_number": detail.get("quotation_number"),
        "parent": item.get("parent") or detail.get("name"),
        "supplier": detail.get("supplier"),
        "supplier_name": detail.get("supplier_name"),
        "transaction_date": transaction_date,
        "valid_till": detail.get("valid_till"),
        "status": detail.get("status"),
        "docstatus": detail.get("docstatus"),
        "currency": detail.get("currency"),
        "net_total": detail.get("net_total"),
        "total_taxes_and_charges": detail.get("total_taxes_and_charges"),
        "grand_total": detail.get("grand_total"),
        # Supplier Quotation Item
        "name": item.get("name"),
        "item_code": item.get("item_code"),
        "item_name": item.get("item_name"),
        "description": html_to_text(item.get("description")),
        "qty": item.get("qty"),
        "uom": item.get("uom"),
        "stock_uom": item.get("stock_uom"),
        "conversion_factor": item.get("conversion_factor"),
        "rate": _first_non_zero(item.get("rate"), item.get("net_rate")),
        "amount": _first_non_zero(
            item.get("amount"),
            item.get("net_amount"),
            _number(item.get("qty")) * _number(item.get("rate")),
        ),
        "net_rate": item.get("net_rate"),
        "net_amount": item.get("net_amount"),
        "lead_time_days": lead_time_days,
        "schedule_date": expected_delivery_date,
        "expected_delivery_date": expected_delivery_date,
        "warehouse": item.get("warehouse"),
        "item_tax_rate": _parse_json_object(item.get("item_tax_rate")),
        "request_for_quotation": item.get("request_for_quotation"),
        "request_for_quotation_item": item.get("request_for_quotation_item"),
        "material_request": item.get("material_request"),
        "material_request_item": item.get("material_request_item"),
    }


def get_supplier_quotation_documents(
    rfq_name: str,
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[dict[str, Any]]:
    """RFQ에 연결된 Supplier Quotation 부모 문서를 중복 없이 반환한다."""
    get_many = get_many or erp_get
    get_one = get_one or erp_get_one
    summaries = get_many(
        "Supplier Quotation",
        filters=[["Supplier Quotation Item", "request_for_quotation", "=", rfq_name]],
        fields=["name"],
        limit=500,
    ) or []

    results: list[dict[str, Any]] = []
    for quotation_name in _unique_quotation_names(summaries):
        detail = get_one("Supplier Quotation", quotation_name)
        if not detail:
            continue
        if int(detail.get("docstatus") or 0) == 2:
            continue
        if any(item.get("request_for_quotation") == rfq_name for item in detail.get("items") or []):
            results.append(detail)
    return results


def get_supplier_quotations(
    rfq_name: str,
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[dict[str, Any]]:
    """RFQ의 모든 ERPNext 견적을 기존 호환 품목 단위 dict로 반환한다."""
    results: list[dict[str, Any]] = []
    for detail in get_supplier_quotation_documents(
        rfq_name,
        get_many=get_many,
        get_one=get_one,
    ):
        for item in detail.get("items") or []:
            # 한 견적에 다른 RFQ 품목이 섞여 있어도 요청한 RFQ만 반환한다.
            if item.get("request_for_quotation") != rfq_name:
                continue
            results.append(_normalize_item(detail, item))
    return results


def get_quotations_for_rfq(
    rfq_name: str,
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[dict[str, Any]]:
    """RFQ별 Supplier Quotation을 헤더와 품목이 결합된 호환 형식으로 반환한다.

    워크플로 투영과 PO 생성이 사용하는 구조이며 외부 AI 호출은 수행하지 않는다.
    """
    quotations: list[dict[str, Any]] = []
    for detail in get_supplier_quotation_documents(
        rfq_name,
        get_many=get_many,
        get_one=get_one,
    ):
        items = [
            _normalize_item(detail, item)
            for item in detail.get("items") or []
            if item.get("request_for_quotation") == rfq_name
        ]
        first_item = items[0] if items else {}
        grand_total = _first_non_zero(
            detail.get("grand_total"),
            detail.get("rounded_total"),
            detail.get("net_total"),
            sum(_number(item.get("amount")) for item in items),
        )
        quotations.append({
            "name": detail.get("name"),
            "rfq_name": rfq_name,
            "supplier": detail.get("supplier"),
            "supplier_name": detail.get("supplier_name") or detail.get("supplier"),
            "docstatus": detail.get("docstatus"),
            "status": detail.get("status"),
            "modified": detail.get("modified"),
            # 포털 제출 시각의 근거. 이게 없으면 마감 판정이 기준을 잃는다.
            "creation": detail.get("creation"),
            "transaction_date": detail.get("transaction_date"),
            "valid_till": detail.get("valid_till"),
            "currency": detail.get("currency") or "KRW",
            "conversion_rate": detail.get("conversion_rate"),
            "grand_total": grand_total,
            "rounded_total": detail.get("rounded_total"),
            "net_total": detail.get("net_total"),
            "base_grand_total": detail.get("base_grand_total"),
            "rate": first_item.get("rate"),
            "amount": first_item.get("amount"),
            "expected_delivery_date": first_item.get("expected_delivery_date"),
            "lead_time_days": first_item.get("lead_time_days"),
            "items": items,
        })
    return quotations


def get_quotations_for_rfqs(
    rfq_names: list[str],
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[dict[str, Any]]:
    """Return de-duplicated quotation documents from multiple RFQ rounds."""
    quotations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rfq_name in rfq_names:
        normalized = str(rfq_name or "").strip()
        if not normalized:
            continue
        for quotation in get_quotations_for_rfq(
            normalized,
            get_many=get_many,
            get_one=get_one,
        ):
            quotation_id = str(quotation.get("name") or "").strip()
            if not quotation_id or quotation_id in seen:
                continue
            seen.add(quotation_id)
            quotations.append({**quotation, "rfq_name": normalized})
    return quotations


def _quotation_from_document(detail: dict[str, Any], rfq_name: str) -> Quotation:
    """ERPNext Supplier Quotation 하나를 reviewer 공통 모델로 변환한다."""
    transaction_date = detail.get("transaction_date")
    separated = parse_separated_terms(detail.get('terms'))
    items: list[dict[str, Any]] = []
    for index, item in enumerate(detail.get("items") or [], 1):
        if item.get("request_for_quotation") != rfq_name:
            continue
        description = html_to_text(item.get("description"))
        net_rate = item.get("net_rate")
        net_amount = item.get("net_amount")
        items.append({
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name") or item.get("item_code") or "품목명 미기재",
            "description": description,
            "quantity": item.get("qty"),
            "unit": item.get("uom") or item.get("stock_uom"),
            "unit_price": net_rate if net_rate is not None else item.get("rate"),
            "amount": net_amount if net_amount is not None else item.get("amount"),
            "expected_delivery_date": item.get("expected_delivery_date"),
            "lead_time_days": item.get("lead_time_days"),
            # Classified terms are supplier evidence; do not merge an ERP/RFQ
            # description that may have been copied from the requested spec.
            "specifications": separated[0].get(index, {}) if separated else extract_specifications(description),
            "raw_description": description,
        })

    return Quotation.model_validate({
        # 이후 PO 연결에 사용할 수 있도록 외부 견적번호가 아닌 ERP 문서명을 쓴다.
        "quotation_id": detail.get("name"),
        "rfq_name": rfq_name,
        "supplier_id": detail.get("supplier"),
        "supplier_name": detail.get("supplier_name") or detail.get("supplier"),
        "status": str(detail.get("status") or "received").lower(),
        "business_registration_no": detail.get("tax_id"),
        "quotation_date": transaction_date,
        "valid_until": detail.get("valid_till"),
        "currency": detail.get("currency") or "KRW",
        "subtotal": detail.get("net_total") if detail.get("net_total") is not None else detail.get("total"),
        "tax_amount": detail.get("total_taxes_and_charges") or 0,
        "total_amount": detail.get("grand_total") if detail.get("grand_total") is not None else detail.get("rounded_total"),
        "base_total_amount": detail.get("base_grand_total"),
        "items": items,
        "notes": separated[1] if separated else html_to_text(detail.get("terms")),
        "content_sections_separated": separated is not None,
        "source": {
            "kind": "portal",
            "filename": str(detail.get("name") or "ERPNext Supplier Quotation"),
            "path": None,
            "content_type": "application/vnd.erpnext.supplier-quotation",
        },
        # ERP 저장 이후에는 원문 재추출 대신 사람 검토로 보내야 한다.
        "extraction_attempt": 3,
        "extraction_evidence": [
            f"ERPNext Supplier Quotation 조회: {detail.get('name')}",
            f"외부 견적번호: {detail.get('quotation_number')}" if detail.get("quotation_number") else "ERPNext 포털 입력 견적",
        ],
    })


def get_reviewable_quotations(
    rfq_name: str,
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[Quotation]:
    """포털/외부 출처를 구분하지 않은 ERP 기준 검토 입력을 반환한다."""
    return [
        _quotation_from_document(detail, rfq_name)
        for detail in get_supplier_quotation_documents(
            rfq_name,
            get_many=get_many,
            get_one=get_one,
        )
    ]


def get_reviewable_quotations_for_rfqs(
    rfq_names: list[str],
    *,
    get_many: GetMany | None = None,
    get_one: GetOne | None = None,
) -> list[Quotation]:
    """Build de-duplicated review models from every bidding round."""
    quotations: list[Quotation] = []
    seen: set[str] = set()
    for rfq_name in rfq_names:
        normalized = str(rfq_name or "").strip()
        if not normalized:
            continue
        for quotation in get_reviewable_quotations(
            normalized,
            get_many=get_many,
            get_one=get_one,
        ):
            quotation_id = str(quotation.quotation_id or "").strip()
            if not quotation_id or quotation_id in seen:
                continue
            seen.add(quotation_id)
            quotations.append(quotation)
    return quotations
