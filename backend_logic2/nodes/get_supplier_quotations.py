"""
nodes/get_supplier_quotations.py — 7번 모듈: 공급사 견적 수신 조회

입력: RFQ 이름 (예: PUR-RFQ-2026-00270)
하는 일: 해당 RFQ에 대해 공급사들이 포털에서 제출한 "Supplier Quotation" 문서들을 조회.
        공급사가 견적서의 Notes란에 자유 텍스트로 남긴 납기일·규격 등도 함께 가져와서
        AI로 구조화된 JSON으로 파싱함.
출력: 공급사별 견적가·납기·수량 등을 터미널에 정리해서 출력 + Notes 구조화 JSON 출력

⚠️ 이 파일은 이 기능(견적 수신 조회) 하나만 담당함. RFQ 생성, Material Request
검토/생성 같은 다른 기능은 각자 독립된 모듈 파일에서 처리하고, 여기서는
erp_client.py의 공통 함수(erp_get / erp_get_one)만 가져다 씀.
이 파일만 단독으로 실행해도 동작함 (다른 모듈에 의존하지 않음).

<<<<<<< HEAD
⚠️ Notes 필드 관련: ERPNext 화면상 "Notes"로 보이는 부분은 실제로는 Supplier
Quotation의 "Terms" 탭 > "Terms and Conditions" 영역이고, API 필드명은
"terms"로 확인됨. (Supplier Quotation > Terms 탭, 예: PUR-SQTN-2026-00262)
=======
폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/get_supplier_quotations.py
>>>>>>> 7fbd5c8cb0cb5cdbb665fabd405d190cf6eec650
"""

import json
import os
import sys

<<<<<<< HEAD
from openai import OpenAI
from backend_logic2.erp_client import erp_get, erp_get_one, ERPNextAPIError


OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 확인된 실제 필드명(terms)을 최우선으로 두고, 혹시 다른 곳에 값이 있을 수도
# 있으니 나머지는 fallback으로 남겨둠
NOTES_FIELD_CANDIDATES = ["terms", "custom_notes", "notes", "remarks"]


def _extract_raw_notes(detail):
    """Supplier Quotation 문서에서 공급사가 남긴 자유 텍스트 Notes를 뽑아냄.
    후보 필드명들을 순서대로 확인해서 값이 있는 첫 번째 걸 반환."""
    for field in NOTES_FIELD_CANDIDATES:
        val = detail.get(field)
        if val:
            return val
    return None


def parse_notes_to_json(raw_note, client=None):
    """
    공급사가 자유 텍스트로 남긴 Notes(예: "2026/08/25까지 제출 가능합니다.
    25*34*32 cm")를 AI로 구조화된 JSON으로 변환.

    원문에 없는 내용은 추측해서 채우지 않고 null로 남기도록 프롬프트에 명시함.
    """
    if not raw_note:
        return None

    client = client or OpenAI()

    system_prompt = (
        "너는 구매 담당자를 돕는 어시스턴트다. 공급사가 견적서에 자유 텍스트로 "
        "남긴 메모에서 납기(제출/납품 가능 일자)와 규격(치수) 등 구조화 가능한 "
        "정보를 뽑아내라. 원문에 없는 내용을 추측해서 채우지 마라 — 없으면 null. "
        "반드시 JSON으로만 응답하고 다른 텍스트는 포함하지 마라."
    )
    user_prompt = (
        f"원문 메모:\n{raw_note}\n\n"
        "아래 JSON 형식으로만 응답해라:\n"
        "{\n"
        '  "delivery_date": "<납기/제출 가능 일자, 원문 표현 그대로. 없으면 null>",\n'
        '  "dimensions": "<규격/치수, 없으면 null>",\n'
        '  "other_notes": "<위 두 항목 외 특이사항, 없으면 null>"\n'
        "}"
    )

    res = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return json.loads(res.choices[0].message.content)
=======
import os 
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from erp_client import erp_get, erp_get_one, ERPNextAPIError
>>>>>>> 7fbd5c8cb0cb5cdbb665fabd405d190cf6eec650


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
            "notes_raw": "2026/08/25까지 제출 가능합니다.\n25*34*32 cm",
            "notes_structured": {
                "delivery_date": "2026/08/25",
                "dimensions": "25*34*32 cm",
                "other_notes": None,
            },
        },
        ...
    ]
    (notes_raw / notes_structured는 견적 문서 단위 정보라 같은 견적의 모든
    품목 행에 동일하게 반복돼서 들어감 — supplier/quotation_name 등과 같은 패턴)
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

    openai_client = OpenAI()
    results = []
    for sq in quotations:
        # 2) 목록 조회만으로는 자식 테이블(items)이 안 나오므로, 문서 하나씩
        #    name으로 상세 조회해서 items(품목별 견적가/수량/납기)까지 가져옴
        detail = erp_get_one("Supplier Quotation", sq["name"])
        items = detail.get("items") or []

        # 3) 공급사가 자유 텍스트로 남긴 Notes를 뽑아서 AI로 구조화
        #    (문서 하나당 한 번만 호출 — 품목마다 반복 호출하지 않음)
        raw_note = _extract_raw_notes(detail)
        notes_structured = parse_notes_to_json(raw_note, client=openai_client)

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
                "notes_raw": raw_note,
                "notes_structured": notes_structured,
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


def print_notes_as_json(quotations):
    """공급사별로 Notes(원문 + AI가 구조화한 JSON)를 한 번씩만 뽑아서 출력.
    (품목마다 반복 저장돼 있으므로 quotation_name 기준으로 중복 제거)"""
    seen = set()
    notes_list = []
    for q in quotations:
        if q["quotation_name"] in seen:
            continue
        seen.add(q["quotation_name"])
        notes_list.append({
            "quotation_name": q["quotation_name"],
            "supplier": q["supplier"],
            "supplier_name": q["supplier_name"],
            "notes_raw": q.get("notes_raw"),
            "notes_structured": q.get("notes_structured"),
        })

    print("=== 공급사별 Notes (구조화 JSON) ===")
    print(json.dumps(notes_list, ensure_ascii=False, indent=2))
    print()


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
    print_notes_as_json(quotations)


if __name__ == "__main__":
    main()