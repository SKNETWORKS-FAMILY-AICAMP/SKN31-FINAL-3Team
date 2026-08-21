"""
nodes/get_supplier_quotations.py — 7번 모듈: 공급사 견적 수신 조회

입력: RFQ 이름 (예: PUR-RFQ-2026-00270)
하는 일: 해당 RFQ에 대해 공급사들이 포털에서 제출한 "Supplier Quotation" 문서들을 조회
출력: 공급사별 견적가·납기·수량 등을 터미널에 정리해서 출력

⚠️ 이 파일은 이 기능(견적 수신 조회) 하나만 담당함. RFQ 생성, Material Request
검토/생성 같은 다른 기능은 각자 독립된 모듈 파일에서 처리하고, 여기서는
erp_client.py의 공통 함수(erp_get / erp_get_one)만 가져다 씀.
이 파일만 단독으로 실행해도 동작함 (다른 모듈에 의존하지 않음).

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/get_supplier_quotations.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_get, erp_get_one, ERPNextAPIError


def get_supplier_quotations(rfq_name):
    """
    주어진 RFQ(Request for Quotation)에 연결된 Supplier Quotation 문서들을
    조회해서, 품목 단위(공급사 x 품목)로 펼친 리스트를 반환.

    반환 형식: [
        {
            "quotation_name": "SQ-2026-00031",
            "supplier": "SUP-0012",
            "supplier_name": "삼진상사",
            "transaction_date": "2026-08-15",
            "status": "Submitted",
            "item_code": "ITEM-001",
            "item_name": "베어링 6205",
            "qty": 100,
            "rate": 3200.0,
            "amount": 320000.0,
            "lead_time_days": 7,
        },
        ...
    ]
    """
    # 1) 이 RFQ를 참조(request_for_quotation)하는 Supplier Quotation 문서 목록 조회
    #    (자식 테이블 Supplier Quotation Item의 필드로 필터링)
    quotations = erp_get(
        "Supplier Quotation",
        filters=[["Supplier Quotation Item", "request_for_quotation", "=", rfq_name]],
        fields=["name", "supplier", "supplier_name", "transaction_date", "status"],
    )

    if not quotations:
        return []

    results = []
    for sq in quotations:
        # 2) 목록 조회만으로는 자식 테이블(items)이 안 나오므로, 문서 하나씩
        #    name으로 상세 조회해서 items(품목별 견적가/수량/납기)까지 가져옴
        detail = erp_get_one("Supplier Quotation", sq["name"])
        items = detail.get("items") or []

        for item in items:
            results.append({
                "quotation_name": detail.get("name"),
                "supplier": detail.get("supplier"),
                "supplier_name": detail.get("supplier_name"),
                "transaction_date": detail.get("transaction_date"),
                "status": detail.get("status"),
                "item_code": item.get("item_code"),
                "item_name": item.get("item_name"),
                "qty": item.get("qty"),
                "rate": item.get("rate"),
                "amount": item.get("amount"),
                "lead_time_days": item.get("lead_time_days"),
            })

    return results


def print_quotations_summary(rfq_name, quotations):
    """조회된 견적 내역을 터미널에 표 형태로 정리해서 출력"""
    print(f"\n=== RFQ '{rfq_name}' 공급사 견적 수신 현황 ===")

    if not quotations:
        print("(아직 제출된 Supplier Quotation이 없습니다)\n")
        return

    header = f"{'공급사':<16} {'품목':<18} {'수량':>8} {'단가':>12} {'금액':>14} {'납기(일)':>9} {'상태':<10}"
    print(header)
    print("-" * len(header))

    for q in quotations:
        supplier_disp = q["supplier_name"] or q["supplier"] or "-"
        item_disp = q["item_name"] or q["item_code"] or "-"
        qty_disp = q["qty"] if q["qty"] is not None else "-"
        rate_disp = f"{q['rate']:,.0f}" if q["rate"] is not None else "-"
        amount_disp = f"{q['amount']:,.0f}" if q["amount"] is not None else "-"
        lead_disp = q["lead_time_days"] if q["lead_time_days"] is not None else "-"
        status_disp = q["status"] or "-"

        print(
            f"{supplier_disp:<16} {item_disp:<18} {qty_disp:>8} "
            f"{rate_disp:>12} {amount_disp:>14} {lead_disp:>9} {status_disp:<10}"
        )

    n_suppliers = len({q["quotation_name"] for q in quotations})
    print("-" * len(header))
    print(f"총 {n_suppliers}건의 Supplier Quotation, {len(quotations)}개 품목 라인\n")


def main():
    rfq_name = input("RFQ 이름 입력 (예: PUR-RFQ-2026-00270): ").strip()

    if not rfq_name:
        print("RFQ 이름이 비어있습니다.")
        sys.exit(1)

    try:
        quotations = get_supplier_quotations(rfq_name)
    except ERPNextAPIError as e:
        print(f"[에러] ERPNext API 호출 실패: {e}")
        sys.exit(1)

    print_quotations_summary(rfq_name, quotations)


if __name__ == "__main__":
    main()