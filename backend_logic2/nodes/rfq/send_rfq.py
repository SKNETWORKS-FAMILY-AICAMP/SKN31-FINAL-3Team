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

import os
import requests
from backend_logic2.integrations.erp_client import erp_get_one, erp_post, erp_submit, ERPNextAPIError, SITE_URL, HEADERS

DEFAULT_MESSAGE = (
    "견적 부탁드립니다.<br><br>"
    "{{ portal_link }}<br><br>"
    "처음 거래하시는 경우, 아래 버튼으로 포털 비밀번호를 설정해주세요.<br>"
    "{{ update_password_link }}"
)


def create_rfq(
    mr_name: str,
    supplier_names: list,
    message: str = DEFAULT_MESSAGE,
    *,
    send_email: bool = True,
    submit: bool = True,
):
    """
    MR을 RFQ 문서로 변환 (Draft 생성 + Submit까지).

    공개 REST API로 MR을 조회하고 RFQ payload를 구성합니다.
    ERPNext 버전에 따라 위치가 달라지는 내부 Python 매퍼 경로에는 의존하지 않습니다.
    """
    try:
        mr = erp_get_one("Material Request", mr_name)
        if not mr:
            print(f"[create_rfq] Material Request를 찾을 수 없습니다: {mr_name}")
            return None
        if int(mr.get("docstatus") or 0) != 1:
            print(f"[create_rfq] Submit된 Material Request만 RFQ로 만들 수 있습니다: {mr_name}")
            return None

        items_payload = []
        for row in mr.get("items", []):
            item_code = row.get("item_code")
            row_name = row.get("name")
            if not item_code or not row_name:
                print(f"[create_rfq] MR 품목에 item_code/name이 없습니다: {row}")
                return None
            items_payload.append({
                "item_code": item_code,
                "item_name": row.get("item_name"),
                "description": row.get("description"),
                "qty": row.get("qty"),
                "stock_uom": row.get("stock_uom") or row.get("uom"),
                "uom": row.get("uom") or row.get("stock_uom"),
                "conversion_factor": row.get("conversion_factor") or 1,
                "schedule_date": row.get("schedule_date") or mr.get("schedule_date"),
                "warehouse": row.get("warehouse"),
                "material_request": mr_name,
                "material_request_item": row_name,
                "project_name": row.get("project"),
                "cost_center": row.get("cost_center"),
            })
        if not items_payload:
            print(f"[create_rfq] RFQ로 변환할 MR 품목이 없습니다: {mr_name}")
            return None

        mapped_rfq = {
            "company": mr.get("company"),
            "transaction_date": mr.get("transaction_date"),
            "schedule_date": mr.get("schedule_date"),
            "subject": mr.get("title") or f"Request for Quotation for {mr_name}",
            "items": items_payload,
        }
    except Exception as e:
        print(f"[create_rfq] MR 조회/변환 에러: {e}")
        return None

    # 2. 공급사(Supplier) 목록 구성 (기존 안전장치 로직 유지)
    # .env에 TEST_RECIPIENT_OVERRIDE 설정되어 있으면, 실제 벤더 이메일 대신 강제 교체
    test_override = os.getenv("TEST_RECIPIENT_OVERRIDE")
    suppliers_payload = []
    
    for s in supplier_names:
        # ERPNext는 Submit 시 이 child-row 체크값을 보고 공급사 메일을 보낸다.
        row = {"supplier": s, "send_email": 1 if send_email else 0}
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
    
    # 5. 선택적으로 제출 (Draft-only이면 생성까지만 수행)
    if rfq and submit:
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


def create_and_send_rfq(
    mr_name: str,
    supplier_names: list,
    *,
    send_email: bool = True,
    submit: bool = True,
):
    """
    RFQ 생성 (+ Submit). 발송은 이 함수가 따로 안 함 — erp_submit()이
    Submit되는 순간 RFQ의 Suppliers 테이블에 있는 "Send Email" 체크박스가
    이미 자동으로 발송을 트리거함. send_rfq()를 여기서 또 부르면 같은
    이메일이 두 번 나가는 문제가 실제로 있었음(원인 확정됨).
    """
    rfq = create_rfq(
        mr_name,
        supplier_names,
        send_email=send_email,
        submit=submit,
    )
    if not rfq:
        print(f"[create_and_send_rfq] '{mr_name}' RFQ 생성 실패")
        return None

    if not submit:
        print(f"[create_and_send_rfq] RFQ Draft 생성 완료: {rfq['name']} (Submit/메일 발송 안 함)")
    elif not send_email:
        print(f"[create_and_send_rfq] RFQ 생성+Submit 완료: {rfq['name']} (공급사 메일 발송 안 함)")
    else:
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
