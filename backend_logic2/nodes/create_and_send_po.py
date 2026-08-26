"""
create_and_send_po.py — 9번 모듈: PO 생성 + 발송

입력: RFQ 이름, 선정된 공급사 ID
하는 일: 
  1. 7번 모듈(get_supplier_quotations)을 재사용해 견적 데이터를 가져옴
  2. ERP에 Purchase Order 문서(Draft) 생성 (erp_post)   
  3. PO를 Submit 상태로 변경하여 확정 (erp_submit)
  4. 공급사에게 PO 이메일 발송 (erp_send_email) - TEST_MODE 자동 적용됨
출력: 생성된 PO 이름, 처리 결과를 터미널에 출력
"""

import sys
import os
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# erp_client.py에서 실제 구현되어 있는 함수들을 임포트
from erp_client import erp_post, erp_submit, erp_send_email, ERPNextAPIError, erp_get
from get_supplier_quotations import get_supplier_quotations

# ERP 도메인은 환경마다 달라질 수 있으므로 하드코딩하지 않고 환경변수로 관리
ERP_DOMAIN = os.getenv("ERP_DOMAIN", "http://13.209.103.102:8080")
# 확인 필요: 실제 공급사 포털 라우팅 경로가 "/orders/{po_name}"가 맞는지
# ERPNext 웹사이트/포털 설정을 확인해서 맞춰야 함 (추측으로 넣은 값)
ERP_PORTAL_PATH_TEMPLATE = os.getenv("ERP_PORTAL_PATH_TEMPLATE", "/orders/{po_name}")


def _resolve_test_mode():
    """
    TEST_MODE 판정 기준을 erp_client와 통일한다.
    erp_client.py가 자체적으로 TEST_MODE를 판정하는 함수/상수를 노출한다면
    그것을 그대로 써야, 이 스크립트가 출력하는 안내 메시지와
    실제 erp_send_email의 동작이 어긋나지 않는다.
    """
    try:
        from erp_client import is_test_mode as _erp_is_test_mode  # type: ignore
        return _erp_is_test_mode()
    except ImportError:
        pass
    try:
        from erp_client import TEST_MODE as _erp_test_mode_const  # type: ignore
        if isinstance(_erp_test_mode_const, bool):
            return _erp_test_mode_const
        return str(_erp_test_mode_const).lower() != "false"
    except ImportError:
        pass
    # erp_client에 판정 기준이 노출되어 있지 않은 경우의 폴백.
    # 이 경우 실제 발송 차단 여부는 erp_send_email 내부 구현에 달려 있으므로
    # 아래 값은 "안내 메시지"일 뿐 실제 동작과 다를 수 있다는 점을 명시한다.
    print("[경고] erp_client에서 TEST_MODE 판정 기준을 가져오지 못해 로컬 환경변수로 대체합니다. "
          "실제 발송 차단 여부는 erp_client 구현을 반드시 확인하세요.")
    return os.getenv("TEST_MODE", "true").lower() != "false"


def create_and_send_po(rfq_name, supplier_id):
    print(f"\n=== [9번] PO(Purchase Order) 생성 및 발송 ===")
    print(f"RFQ: {rfq_name} / 선정된 공급사: {supplier_id}")
    
    # TEST_MODE 상태 안내 (erp_client와 동일한 기준 사용)
    is_test_mode = _resolve_test_mode()
    print(f"현재 환경: {'테스트 모드 (실제 메일 발송 차단됨)' if is_test_mode else '운영 모드 (실제 메일 발송됨)'}\n")
    
    # 1. 견적 데이터 조회
    try:
        quotations = get_supplier_quotations(rfq_name)
    except ERPNextAPIError as e:
        print(f"[에러] 견적 데이터 조회 실패: {e}")
        sys.exit(1)
        
    supplier_items = [q for q in quotations if q["supplier"] == supplier_id]
    if not supplier_items:
        print(f"[오류] '{supplier_id}' 공급사가 '{rfq_name}'에 대해 제출한 견적이 없습니다.")
        sys.exit(1)

    # --- Supplier Quotation 문서 하나를 정확히 선택 ---
    # 확인 필요: get_supplier_quotations()가 반환하는 각 item dict에
    # 소속 Supplier Quotation 문서명이 어떤 키로 들어있는지 (ERPNext child table
    # export라면 보통 "parent"). 아래는 "parent" 키를 가정한 방어 로직이다.
    # 같은 공급사가 같은 RFQ에 대해 여러 번(재견적 등) Supplier Quotation을
    # 제출했다면, 어느 문서의 항목인지 뒤섞인 채로 PO를 만들면 안 된다.
    quotation_names = {item.get("parent") for item in supplier_items if item.get("parent")}
    quotation_name = None
    if len(quotation_names) > 1:
        print(f"[오류] '{supplier_id}' 공급사의 견적 항목이 서로 다른 Supplier Quotation 문서에 걸쳐 있습니다: "
              f"{sorted(quotation_names)}")
        print("    하나의 PO는 하나의 Supplier Quotation 문서에서만 생성해야 하므로, 어떤 문서를 쓸지 사람이 확인해야 합니다.")
        sys.exit(1)
    elif len(quotation_names) == 1:
        quotation_name = quotation_names.pop()
    else:
        print("[경고] 견적 항목에 소속 Supplier Quotation 문서명이 없어(quotation 연결 불가) "
              "PO에 견적 문서를 연결하지 못합니다. get_supplier_quotations()의 반환 스키마를 확인하세요.")

    # --- 기존 PO 조회를 통한 중복 생성 방지 ---
    # 확인 필요: "Purchase Order Item"에 "supplier_quotation" 필드가 실제로 존재하는지,
    # 그리고 취소(docstatus=2)된 PO는 중복으로 치지 않아야 하는지 등은 실제 ERPNext
    # 커스터마이징에 따라 다를 수 있다. quotation_name이 없으면(위에서 경고) 이 검사는
    # 건너뛸 수밖에 없으므로, 이 경우 중복 방지가 보장되지 않는다는 점을 알린다.
    if quotation_name:
        try:
            existing_po_items = erp_get(
                "Purchase Order Item",
                filters=[["supplier_quotation", "=", quotation_name]],
                fields=["parent"]
            )
        except Exception as e:
            print(f"[경고] 기존 PO 중복 여부 확인 중 오류가 발생했습니다: {e}")
            existing_po_items = []

        if existing_po_items:
            existing_po_names = sorted({row["parent"] for row in existing_po_items})
            print(f"[오류] 이 견적(Supplier Quotation: {quotation_name})에 대해 이미 PO가 존재합니다: {existing_po_names}")
            print("    중복 발주를 막기 위해 새 PO를 생성하지 않습니다.")
            sys.exit(1)
    else:
        print("[경고] Supplier Quotation 문서명을 확인할 수 없어 중복 PO 생성 검사를 건너뜁니다. "
              "수동으로 기존 PO 존재 여부를 확인해 주세요.")

    # 2. Purchase Order 데이터 구성
    # schedule_date는 오직 견적 항목(quotations)에서만 가져온다 (단일 출처).
    # 매직 데이트로 조용히 채우지 않고, 없으면 바로 에러로 알려준다.
    missing_date_items = [item["item_code"] for item in supplier_items if not item.get("schedule_date")]
    if missing_date_items:
        print(f"[오류] 아래 품목에 납기일(schedule_date)이 없어 PO를 생성할 수 없습니다: {missing_date_items}")
        sys.exit(1)

    po_items = []
    for item in supplier_items:
        po_item = {
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item["rate"],
            "schedule_date": item["schedule_date"]
        }
        # --- PO에 Supplier Quotation 연결 ---
        # 확인 필요: ERPNext 표준 필드명이 "supplier_quotation" /
        # "supplier_quotation_item"이 맞는지. 이 연결이 있어야 ERPNext 상에서
        # 추적성이 생기고, 위의 중복 방지 검사도 정상 동작한다.
        if quotation_name:
            po_item["supplier_quotation"] = quotation_name
            if item.get("name"):
                po_item["supplier_quotation_item"] = item["name"]
        po_items.append(po_item)

    po_payload = {
        "supplier": supplier_id,
        "items": po_items,
        # 대표 납기일은 항목들 중 "가장 이른 날짜"로 정한다 (임의로 첫 항목을 쓰지 않는다.
        # 리스트 순서가 항상 날짜순이라는 보장이 없으므로).
        "schedule_date": min(item["schedule_date"] for item in po_items)
    }

    # 3. PO 문서 생성 (Draft)
    try:
        print("1. ERP에 Purchase Order 문서(Draft)를 생성합니다...")
        new_po = erp_post("Purchase Order", po_payload)
        po_name = new_po.get("name")
        print(f"   -> 생성 완료! PO 번호: {po_name}")
    except ERPNextAPIError as e:
        print(f"[에러] PO Draft 생성 중 오류 발생: {e}")
        sys.exit(1)

    # --- Draft 생성 성공 / Submit 실패 상태를 분리 ---
    # Draft 생성까지는 성공했는데 Submit이 실패하면, PO 자체는 이미 ERP에 존재한다.
    # 이 사실을 숨기고 그냥 exit(1) 해버리면, 사용자가 "실패했나보다" 하고
    # 스크립트를 재실행해 또 다른 Draft PO를 만들 위험이 있다.
    try:
        print("2. 생성된 PO를 Submit(확정) 상태로 변경합니다...")
        erp_submit("Purchase Order", po_name)
        print(f"   -> Submit 처리 완료!")
    except ERPNextAPIError as e:
        print("=============================================")
        print(f"⚠️  PO({po_name})는 Draft 상태로 이미 생성되었으나 Submit(확정)에는 실패했습니다: {e}")
        print(f"    ERP에서 '{po_name}' 문서를 직접 확인 후 Submit하거나 삭제해 주세요.")
        print("    이 상태에서 스크립트를 재실행하면 새로운 Draft PO가 또 생성되니 주의하세요.")
        print("=============================================")
        sys.exit(1)

    # 여기부터는 PO가 이미 ERP에 생성/확정된 상태.
    # 이후 이메일 단계가 실패하더라도 "PO 생성 실패"로 오인해 재실행하면
    # 중복 PO가 생성될 수 있으므로, 반드시 별도 블록으로 분리해 처리 상태를 명확히 안내한다.
    print(f"\n3. 공급사({supplier_id})에게 이메일 발송 단계")

    recipient_email = None
    try:
        # 확인 필요: Supplier 문서에서 담당자 이메일이 실제로 저장되는 필드가
        # "email_id"가 맞는지 (Contact 문서와 연결되어 있고 Supplier 자체에는
        # 이메일이 없는 구조일 수도 있음). 실제 스키마와 다르면 항상
        # "이메일 없음" 경로로 빠지게 된다.
        supplier_info_list = erp_get(
            "Supplier",
            filters=[["name", "=", supplier_id]],
            fields=["email_id"]
        )
        if supplier_info_list and supplier_info_list[0].get("email_id"):
            recipient_email = supplier_info_list[0]["email_id"]
        else:
            print(f"[경고] '{supplier_id}'의 이메일 정보가 등록되어 있지 않습니다.")
    except Exception as e:
        print(f"[오류] 공급사 이메일 조회 실패: {e}")

    if not recipient_email:
        # 임의의 기본 관리자 메일로 발주서 링크를 자동 발송하지 않는다.
        # 수신자를 확신할 수 없는 상태에서 문서 링크를 보내면 정보가 잘못된 곳으로
        # 전달될 수 있으므로, 발송을 보류하고 담당자가 직접 확인/처리하도록 안내한다.
        print("=============================================")
        print(f"⚠️  PO({po_name})는 정상적으로 생성/확정되었지만, 공급사 이메일 주소를 확인할 수 없어 자동 발송을 건너뛰었습니다.")
        print(f"    ERP에서 '{supplier_id}' 공급사의 이메일 주소를 등록한 뒤 수동으로 발송해 주세요.")
        print("=============================================\n")
        return

    portal_link = ERP_DOMAIN + ERP_PORTAL_PATH_TEMPLATE.format(po_name=po_name)
    subject = f"발주서(PO) 안내 - {po_name}"
    content = f"""
    <p>안녕하세요.</p>
    <p>귀하의 견적을 바탕으로 발주서(<b>{po_name}</b>)를 송부합니다.</p>
    <p>첨부된 PDF 파일을 확인하시거나, 아래 링크를 클릭하여 상세 내역을 확인해 주세요.</p>
    <p><a href="{portal_link}" target="_blank">👉 발주서 상세 확인하기</a></p>
    """

    try:
        erp_send_email("Purchase Order", po_name, recipient_email, subject, content)
        print(f"   -> 이메일 발송 완료! (수신: {recipient_email})")
    except ERPNextAPIError as e:
        # PO는 이미 생성/확정되었으므로 "실패"로 exit(1) 하면 안 되고,
        # 이메일만 실패했다는 사실을 명확히 알려준다.
        print(f"[오류] PO({po_name})는 생성/확정되었으나 이메일 발송에는 실패했습니다: {e}")
        print("    이메일만 별도로 재시도하거나 수동으로 발송해 주세요.")
        return

    print("\n=============================================")
    print(f"✅ 최종 결과: {po_name} 생성 및 처리 완료")
    print("=============================================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PO 생성 및 공급사 발송 스크립트")
    parser.add_argument("rfq_name", help="RFQ 이름 (예: PUR-RFQ-2026-00270)")
    parser.add_argument("supplier_id", help="선정된 공급사 ID (예: 한빛보호구)")
    
    args = parser.parse_args()
    
    create_and_send_po(args.rfq_name, args.supplier_id)
