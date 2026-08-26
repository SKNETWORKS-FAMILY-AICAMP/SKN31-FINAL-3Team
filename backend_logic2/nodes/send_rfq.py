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

    ERPNext의 표준 매퍼(make_request_for_quotation)를 호출하여
    MR의 모든 데이터(커스텀 필드 포함)를 누락 없이 RFQ로 복사해옵니다.
    """
    try:
        mapping_res = requests.post(
            f"{SITE_URL}/api/method/erpnext.buying.doctype.material_request.material_request.make_request_for_quotation",
            headers=HEADERS,
            json={"source_name": mr_name}
        )
        if mapping_res.status_code != 200:
            print(f"[create_rfq] MR 매핑 실패: {mapping_res.text}")
            return None
            
        mapped_rfq = mapping_res.json().get("message")
        if not mapped_rfq:
            print(f"[create_rfq] 매핑된 데이터를 받아오지 못했습니다.")
            return None
            
    except Exception as e:
        print(f"[create_rfq] API 호출 에러: {e}")
        return None

    # 2. 공급사(Supplier) 목록 구성 (기존 안전장치 로직 유지)
    # .env에 TEST_RECIPIENT_OVERRIDE 설정되어 있으면, 실제 벤더 이메일 대신 강제 교체
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

    # 3. 매핑된 RFQ 데이터에 공급사 및 메시지 정보 추가 덮어쓰기
    mapped_rfq["suppliers"] = suppliers_payload
    mapped_rfq["message_for_supplier"] = message
    
    # 헤더 레벨의 schedule_date가 매핑으로 안 넘어왔을 경우를 대비한 2차 안전장치
    if not mapped_rfq.get("schedule_date") and mapped_rfq.get("items"):
        mapped_rfq["schedule_date"] = mapped_rfq["items"][0].get("schedule_date")

    # 4. ERPNext에 완성된 RFQ 문서 실제 생성 (Draft)
    rfq = erp_post("Request for Quotation", mapped_rfq)
    
    # 5. 제출 (Submit)
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
    """
    RFQ 생성 (+ Submit). 발송은 이 함수가 따로 안 함 — erp_submit()이
    Submit되는 순간 RFQ의 Suppliers 테이블에 있는 "Send Email" 체크박스가
    이미 자동으로 발송을 트리거함. send_rfq()를 여기서 또 부르면 같은
    이메일이 두 번 나가는 문제가 실제로 있었음(원인 확정됨).
    """
    rfq = create_rfq(mr_name, supplier_names)
    if not rfq:
        print(f"[create_and_send_rfq] '{mr_name}' RFQ 생성 실패")
        return None

    print(f"[create_and_send_rfq] RFQ 생성+발송 완료: {rfq['name']} "
          f"(Submit 시 Suppliers의 'Send Email' 체크로 이미 자동 발송됨)")
    return rfq


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    supplier_input = input("공급사 이름 입력 (여러 개면 콤마로 구분): ").strip()
    supplier_names = [s.strip() for s in supplier_input.split(",") if s.strip()]

    rfq = create_and_send_rfq(mr_name, supplier_names)
    if rfq:
        print(f"\n완료: {rfq['name']}")