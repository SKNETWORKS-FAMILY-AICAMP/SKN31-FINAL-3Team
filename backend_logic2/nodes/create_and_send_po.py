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


def create_and_send_po(rfq_name, supplier_id):
    print(f"\n=== [9번] PO(Purchase Order) 생성 및 발송 ===")
    print(f"RFQ: {rfq_name} / 선정된 공급사: {supplier_id}")
    
    # TEST_MODE 상태 안내
    is_test_mode = os.getenv("TEST_MODE", "true").lower() != "false"
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

    # 2. Purchase Order 데이터 구성
    po_items = []
    for item in supplier_items:
        item_date = item.get("schedule_date", "2026-08-31") 
        po_items.append({
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item["rate"],
            "schedule_date": item_date
        })
        
    po_payload = {
        "supplier": supplier_id,
        "items": po_items,
        # po_items의 첫 번째 아이템 날짜를 대표 납기일로 사용
        "schedule_date": po_items[0]["schedule_date"] if po_items else "2026-08-31" 
    }

    try:
        # 3. PO 문서 생성 (Draft)
        print("1. ERP에 Purchase Order 문서(Draft)를 생성합니다...")
        new_po = erp_post("Purchase Order", po_payload)
        po_name = new_po.get("name")
        print(f"   -> 생성 완료! PO 번호: {po_name}")

        # 4. PO 문서 Submit (확정)
        print("2. 생성된 PO를 Submit(확정) 상태로 변경합니다...")
        erp_submit("Purchase Order", po_name)
        print(f"   -> Submit 처리 완료!")
        
        # 5. 이메일 발송 처리
        print(f"\n3. 공급사({supplier_id})에게 이메일 발송 단계")
        
        try:
            # 🔴 핵심: 기존 erp_get 함수에 "이름이 김효민인 사람의 이메일만 가져와"라고 정확히 요청
            supplier_info_list = erp_get(
                "Supplier", 
                filters=[["name", "=", supplier_id]], 
                fields=["email_id"]
            )
            
            # 리스트에 데이터가 정상적으로 담겨 왔다면 첫 번째 항목의 이메일을 꺼냄
            if supplier_info_list and len(supplier_info_list) > 0:
                recipient_email = supplier_info_list[0].get("email_id")
            else:
                print(f"[경고] '{supplier_id}'의 이메일 정보가 없어 기본 메일로 대체합니다.")
                recipient_email = "default_admin@company.com"
                
        except Exception as e:
            print(f"[오류] 공급사 이메일 조회 실패: {e}")
            recipient_email = "default_admin@company.com"

        # --- 메일 발송 실행 ---
        erp_domain = "http://13.209.103.102:8080"
        portal_link = f"{erp_domain}/orders/{po_name}"
        
        subject = f"발주서(PO) 안내 - {po_name}"
        
        # HTML 태그를 활용해 링크를 예쁘게 만들어줍니다.
        content = f"""
        <p>안녕하세요.</p>
        <p>귀하의 견적을 바탕으로 발주서(<b>{po_name}</b>)를 송부합니다.</p>
        <p>첨부된 PDF 파일을 확인하시거나, 아래 링크를 클릭하여 상세 내역을 확인해 주세요.</p>
        <p><a href="{portal_link}" target="_blank">👉 발주서 상세 확인하기</a></p>
        """
        
        # 동적으로 찾아낸 recipient_email로 발송
        erp_send_email("Purchase Order", po_name, recipient_email, subject, content)
        
    except ERPNextAPIError as e:
        print(f"[에러] 작업 중 오류 발생: {e}")
        sys.exit(1)

    print("\n=============================================")
    print(f"✅ 최종 결과: {po_name} 생성 및 처리 완료")
    print("=============================================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PO 생성 및 공급사 발송 스크립트")
    parser.add_argument("rfq_name", help="RFQ 이름 (예: PUR-RFQ-2026-00270)")
    parser.add_argument("supplier_id", help="선정된 공급사 ID (예: 한빛보호구)")
    
    args = parser.parse_args()
    
    create_and_send_po(args.rfq_name, args.supplier_id)