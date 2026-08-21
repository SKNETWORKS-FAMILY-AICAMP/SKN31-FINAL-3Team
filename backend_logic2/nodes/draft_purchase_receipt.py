"""
nodes/draft_purchase_receipt.py — 10번 모듈: 입고(Purchase Receipt) 초안 생성

입력: PO(Purchase Order) 이름
하는 일: 해당 PO의 품목(items)을 그대로 가져와 Purchase Receipt를
        "Draft" 상태로만 생성함.
        ⚠️ Submit은 절대 안 함 — 실제 입고 확인(수량 검수 등)은 사람이
        물건을 받고 나서 직접 확정해야 하는 부분이라, 여기서는 문서
        초안만 만들어서 담당자가 검토/수정 후 확정하게 함.
출력: 생성된 Draft Purchase Receipt 이름을 터미널에 출력

⚠️ 9번(create_and_send_po.py)에서 실제로 어떤 필드명(warehouse,
schedule_date 등)으로 PO Item을 만들었는지에 따라 아래 items_payload
매핑을 조정해야 할 수 있음. send_rfq.py의 items_payload 매핑 방식을
그대로 참고해서 작성함 — PO 문서를 erp_get_one으로 직접 찍어보고
실제 필드명과 맞는지 확인 권장.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/draft_purchase_receipt.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_get_one, erp_post, ERPNextAPIError


def draft_purchase_receipt(po_name: str):
    """
    PO 문서를 조회해서 그 items를 Purchase Receipt items로 매핑,
    Draft 상태로만 생성(Submit 안 함).

    반환: 생성된 Purchase Receipt 문서(dict) 또는 None(PO 없음/생성 실패)
    """
    po = erp_get_one("Purchase Order", po_name)
    if not po:
        return None

    items_payload = [
        {
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item.get("rate"),
            "uom": item.get("uom", "Nos"),
            "conversion_factor": item.get("conversion_factor", 1),
            "warehouse": item.get("warehouse"),
            # PO 화면의 "Required By" 항목 — ERP 안에서는 schedule_date 라는 이름으로 저장됨
            "schedule_date": item.get("schedule_date"),
            # 이 두 필드로 PR이 원래 PO 라인과 연결됨 (부분입고/수량대사 등에 필요)
            "purchase_order": po_name,
            "purchase_order_item": item["name"],
        }
        for item in po.get("items", [])
    ]

    payload = {
        "supplier": po["supplier"],
        "items": items_payload,
    }

    try:
        pr = erp_post("Purchase Receipt", payload)
    except ERPNextAPIError as e:
        print(f"[draft_purchase_receipt] 생성 실패: {e}")
        return None

    return pr


if __name__ == "__main__":
    po_name = input("Purchase Order 이름 입력: ").strip()

    if not po_name:
        print("PO 이름이 비어있습니다.")
        sys.exit(1)

    pr = draft_purchase_receipt(po_name)

    if pr:
        print(f"\nDraft Purchase Receipt 생성 완료: {pr['name']}")
        print("(Submit은 하지 않았습니다 — 실제 입고 확인 후 담당자가 ERPNext에서 직접 확정해야 합니다)")
    else:
        print("Purchase Receipt 생성 실패, 또는 해당 PO를 찾을 수 없습니다.")
