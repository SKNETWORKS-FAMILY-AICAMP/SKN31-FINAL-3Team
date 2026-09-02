"""RFQ에 연결된 모든 ERPNext Supplier Quotation을 조회·정규화한다.

포털 입력 견적과 외부 파일에서 추출 후 등록한 견적을 구분하지 않는다.
품목 단위 평탄화 결과와 review/ranker 공통 ``Quotation`` 모델을 모두 제공한다.
이 모듈에서는 LLM을 사용하지 않는다.

실행:
    python -m backend_logic2.nodes.quotation_filter.get_supplier_quotations PUR-RFQ-2026-00295
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.append(str(BACKEND_ROOT))

from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_get, erp_get_one

try:
    from .quotation_models import Quotation
    from .quotation_reviewer import extract_specifications
except ImportError:  # quotation_filter 폴더에서 직접 실행할 때
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import Quotation
    from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import extract_specifications


GetOne = Callable[[str, str], dict[str, Any] | None]
GetMany = Callable[..., list[dict[str, Any]] | None]


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


def _calculate_schedule_date(transaction_date: Any, lead_time_days: Any) -> str | None:
    """포털의 거래일과 납기 소요일로 PO용 납기일을 계산한다."""
    if not transaction_date or lead_time_days is None:
        return None
    try:
        start = date.fromisoformat(str(transaction_date)[:10])
        return (start + timedelta(days=int(lead_time_days))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


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
        "rate": item.get("rate"),
        "amount": item.get("amount"),
        "net_rate": item.get("net_rate"),
        "net_amount": item.get("net_amount"),
        "lead_time_days": lead_time_days,
        "schedule_date": (
            item.get("expected_delivery_date")
            or _calculate_schedule_date(transaction_date, lead_time_days)
        ),
        "expected_delivery_date": item.get("expected_delivery_date"),
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


def _quotation_from_document(detail: dict[str, Any], rfq_name: str) -> Quotation:
    """ERPNext Supplier Quotation 하나를 reviewer 공통 모델로 변환한다."""
    transaction_date = detail.get("transaction_date")
    items: list[dict[str, Any]] = []
    for item in detail.get("items") or []:
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
            "delivery_date": (
                item.get("expected_delivery_date")
                or _calculate_schedule_date(transaction_date, item.get("lead_time_days"))
            ),
            "lead_time_days": item.get("lead_time_days"),
            "specifications": extract_specifications(description),
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
        "items": items,
        "notes": html_to_text(detail.get("terms")),
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


def print_quotations_summary(rfq_name: str, quotations: list[dict[str, Any]]) -> None:
    """조회 결과를 사람이 확인할 수 있는 평문 표로 출력한다."""
    print(f"\n=== RFQ '{rfq_name}' ERPNext 전체 견적 ===")
    if not quotations:
        print("(제출된 Supplier Quotation이 없습니다)\n")
        return

    header = f"{'공급사':<16} {'품목':<20} {'수량':>8} {'단가':>12} {'금액':>14} {'납기일':<12} {'상태':<10}"
    print(header)
    print("-" * len(header))
    for quotation in quotations:
        supplier = quotation.get("supplier_name") or quotation.get("supplier") or "-"
        item = quotation.get("item_name") or quotation.get("item_code") or "-"
        qty = quotation.get("qty") if quotation.get("qty") is not None else "-"
        rate = f"{quotation['rate']:,.0f}" if quotation.get("rate") is not None else "-"
        amount = f"{quotation['amount']:,.0f}" if quotation.get("amount") is not None else "-"
        schedule_date = quotation.get("schedule_date") or "-"
        status = quotation.get("status") or "-"
        print(f"{supplier:<16} {item:<20} {qty:>8} {rate:>12} {amount:>14} {schedule_date:<12} {status:<10}")

    count = len({row["quotation_name"] for row in quotations})
    print("-" * len(header))
    print(f"총 {count}건의 Supplier Quotation, {len(quotations)}개 품목 라인\n")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="ERPNext Supplier Quotation 통합 조회")
    parser.add_argument("rfq_name", nargs="?", help="예: PUR-RFQ-2026-00295")
    parser.add_argument("--json", action="store_true", help="표 대신 JSON 출력")
    args = parser.parse_args()
    rfq_name = args.rfq_name or input("RFQ 이름 입력: ").strip()
    if not rfq_name:
        parser.error("RFQ 이름이 필요합니다.")

    try:
        quotations = get_supplier_quotations(rfq_name)
    except ERPNextAPIError as exc:
        print(f"[에러] ERPNext API 호출 실패: {exc}")
        raise SystemExit(1) from exc

    if args.json:
        print(json.dumps(quotations, ensure_ascii=False, indent=2, default=str))
    else:
        print_quotations_summary(rfq_name, quotations)


if __name__ == "__main__":
    main()
