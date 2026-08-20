"""
nodes/create_and_send_rfq.py — 6번 모듈: RFQ 생성 + 발송

⚠️ .env에 TEST_MODE=true면 실제 발송 안 하고 콘솔에만 "발송했을 내용"을
보여줌. 진짜 발송하려면 .env에서 TEST_MODE=false로 바꿔야 함.

발송은 ERPNext 내장기능(send_supplier_emails)에 맡김 — 계정생성·Contact
연결·포털권한을 우리가 직접 API로 흉내내다가 여러 번 문제(500 에러) 생겨서,
사람이 UI에서 하는 것과 똑같은 내장 로직으로 감. 이 함수가 실행되는 순간
공급사 계정이 자동 생성되고 포털링크·비밀번호설정링크가 담긴 메일이 나감.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/create_and_send_rfq.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from erp_client import erp_get_one, erp_post, erp_submit, ERPNextAPIError, SITE_URL, HEADERS

DEFAULT_MESSAGE = (
    "견적 부탁드립니다.<br><br>"
    "{{ portal_link }}<br><br>"
    "처음 거래하시는 경우, 아래 버튼으로 포털 비밀번호를 설정해주세요.<br>"
    "{{ update_password_link }}"
)


def create_rfq(mr_name: str, supplier_names: list, message: str = DEFAULT_MESSAGE):
    """
    MR을 RFQ 문서로 변환 (Draft 생성 + Submit까지).

    message 안의 {{ portal_link }}, {{ update_password_link }}는 ERPNext가
    실제 발송 시점에 진짜 링크로 자동 치환해줌 — 텍스트 그대로 넣어둬야
    치환이 일어남 (그냥 두면 아무것도 안 붙는 걸 이미 확인함).
    """
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return None

    items_payload = [
        {
            "item_code": item["item_code"],
            "qty": item["qty"],
            "schedule_date": item["schedule_date"],
            "warehouse": item["warehouse"],
            "uom": item.get("uom", "Nos"),
            "conversion_factor": item.get("conversion_factor", 1),
            "material_request": mr_name,
            "material_request_item": item["name"],
        }
        for item in mr["items"]
    ]

    # .env에 TEST_RECIPIENT_OVERRIDE 설정되어 있으면, 실제 벤더 이메일 대신
    # 이 주소로 강제 교체함 — 내용·발송은 진짜로 일어나되(진짜 메일 도착,
    # 버튼도 진짜로 작동), 실제 회사한테는 절대 안 나가게 하는 안전장치.
    test_override = os.getenv("TEST_RECIPIENT_OVERRIDE")

    suppliers_payload = []
    for s in supplier_names:
        row = {"supplier": s}
        supplier_doc = erp_get_one("Supplier", s)
        if supplier_doc:
            if supplier_doc.get("supplier_primary_contact"):
                row["contact"] = supplier_doc["supplier_primary_contact"]
            email = supplier_doc.get("email_id")
            if email:
                row["email_id"] = test_override or email
        if test_override and "email_id" not in row:
            row["email_id"] = test_override
        suppliers_payload.append(row)

    payload = {
        "transaction_date": mr["transaction_date"],
        "message_for_supplier": message,
        "items": items_payload,
        "suppliers": suppliers_payload,
    }

    rfq = erp_post("Request for Quotation", payload)
    if rfq:
        erp_submit("Request for Quotation", rfq["name"])
    return rfq


def send_rfq(rfq_name: str):
    """
    ERPNext 내장기능으로 RFQ 발송. TEST_MODE=true면 실제 호출 자체를 생략함.
    """
    # ⚠️ 기본값을 일부러 "true"(=발송 생략)로 둠. .env 로딩이 무슨 이유로든
    # 실패해서 TEST_MODE 값을 아예 못 읽는 상황이면, "안전하게 멈추는 쪽"이
    # 맞지 "일단 진짜로 보내는 쪽"으로 가면 안 됨 — 실제로 이거 때문에
    # 실제 벤더한테 잘못 나간 사고가 있었음.
    if os.getenv("TEST_MODE", "true").lower() != "false":
        print(f"[TEST_MODE] 실제 발송 생략 — {rfq_name}")
        print(f"  (진짜 발송이었다면 send_supplier_emails가 호출되어, "
              f"RFQ의 Suppliers 목록 전체에게 계정생성+포털링크 메일이 나갔을 것)")
        return {"test_mode": True, "rfq_name": rfq_name}

    res = requests.post(
        f"{SITE_URL}/api/method/erpnext.buying.doctype.request_for_quotation.request_for_quotation.send_supplier_emails",
        headers=HEADERS,
        json={"rfq_name": rfq_name},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"RFQ 발송 실패: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")


def create_and_send_rfq(mr_name: str, supplier_names: list):
    """create_rfq + send_rfq를 이어서 실행"""
    rfq = create_rfq(mr_name, supplier_names)
    if not rfq:
        print(f"[create_and_send_rfq] '{mr_name}' RFQ 생성 실패")
        return None

    print(f"[create_and_send_rfq] RFQ 생성 완료: {rfq['name']}")
    send_rfq(rfq["name"])
    return rfq


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    supplier_input = input("공급사 이름 입력 (여러 개면 콤마로 구분): ").strip()
    supplier_names = [s.strip() for s in supplier_input.split(",") if s.strip()]

    rfq = create_and_send_rfq(mr_name, supplier_names)
    if rfq:
        print(f"\n완료: {rfq['name']}")