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

⚠️ notes_raw를 가져오는 필드명(_extract_raw_notes 안의 candidate_fields)이
아직 100% 확정이 아님 — check_sq_fields.py로 실제 Supplier Quotation 문서를
열어서 Notes 텍스트박스가 정확히 어느 필드에 저장되는지 확인 후, 그 필드명을
candidate_fields 맨 앞에 추가할 것.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/get_supplier_quotations.py
"""

import json
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI

from erp_client import erp_get, erp_get_one, ERPNextAPIError

# ⚠️ "gpt-5.4-mini"는 실제 존재 확인이 안 된 모델명이라, 검증된 걸로 기본값 설정.
# 팀에서 다른 모델 쓰기로 확정되면 .env의 OPENAI_MODEL로 덮어쓰면 됨.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")


def _extract_raw_notes(detail):
    """
    Supplier Quotation 문서에서 공급사가 남긴 Notes(자유텍스트) 추출.
    정확한 필드명이 아직 미확인이라, 흔히 쓰이는 후보들을 순서대로 시도함.
    (check_sq_fields.py로 확인되면 이 리스트 맨 앞에 확정된 필드명 추가)
    """
    candidate_fields = ["terms", "tc_name", "notes", "instructions", "remarks"]
    for field in candidate_fields:
        value = detail.get(field)
        if value:
            return value
    return None


def parse_notes_to_json(raw_note, client):
    """
    공급사가 자유롭게 적은 Notes를 AI로 구조화된 JSON으로 변환.
    확신 없는 항목은 억지로 채우지 않고 null로 둠.
    """
    empty_result = {"delivery_date": None, "dimensions": None, "other_notes": None}

    if not raw_note or not raw_note.strip():
        return empty_result

    prompt = (
        "다음은 공급사가 견적서에 자유롭게 남긴 메모입니다. "
        "여기서 납품 가능일(delivery_date), 규격/사이즈(dimensions), "
        "그 외 특이사항(other_notes)을 뽑아서 JSON으로만 답하세요.\n\n"
        f"메모: {raw_note}\n\n"
        "확실하지 않은 항목은 null로 답하세요. 틀린 값을 넣는 것보다 "
        "모른다고 하는 게 낫습니다.\n"
        '반드시 이 형식으로만 답하세요: '
        '{"delivery_date": "값 또는 null", "dimensions": "값 또는 null", "other_notes": "값 또는 null"}'
    )

    try:
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print(f"[parse_notes_to_json] 파싱 실패: {e}")
        return empty_result


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
        detail = erp_get_one("Supplier Quotation", sq["name"])
        items = detail.get("items") or []

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