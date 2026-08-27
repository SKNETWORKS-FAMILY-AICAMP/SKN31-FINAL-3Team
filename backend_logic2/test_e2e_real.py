import erp_client
import os

def run_real_e2e_test():
    print("🚀 [실전 E2E 테스트 시작] 실제 ERPNext 서버 연동")
    
    # 본인 서버 연결 확인
    try:
        suppliers = erp_client.get_Supplier(fields=["name"]) if hasattr(erp_client, 'get_Supplier') else erp_client.erp_get("Supplier", fields=["name"], limit=1)
        print(f"✅ 서버 연결 성공! (조회된 샘플 공급사 수: {len(suppliers) if suppliers else 0}개)")
    except Exception as e:
        print(f"❌ 서버 연결 실패: {e}")
        return

    # 테스트에 사용할 실제 품목 및 창고 코드 (본인 서버에 있는 값으로 변경하세요)
    TEST_ITEM_CODE = "OFC-BRD-001" 
    TEST_WAREHOUSE = "Stores - SKN31"

    print(f"\n--- [1단계] 실제 재고 확인 및 구매 요청(MR) 생성 ---")
    stock = erp_client.get_stock_level(TEST_ITEM_CODE, TEST_WAREHOUSE)
    print(f"현재고 조회 결과: {stock}")
    
    # 임의로 수량 10개짜리 Material Request 생성 테스트 (실제 서버에 MR이 생성되고 Submit됩니다!)
    print(f"구매 요청서(MR) 생성 시도 중...")
    mr = erp_client.create_material_request(
        item_code=TEST_ITEM_CODE, 
        qty=10, 
        warehouse=TEST_WAREHOUSE, 
        schedule_date="2026-09-30"
    )
    if mr:
        print(f"✅ MR 생성 및 Submit 완료! 문서 이름: {mr['name']}")
    else:
        print("❌ MR 생성 실패")
        return

    print(f"\n--- [2단계] 구매 경로 판별 테스트 ---")
    route, reasons = erp_client.classify_procurement_route(TEST_ITEM_CODE, 10)
    print(f"📌 판별된 경로: {route}")
    print(f"📌 판별 사유: {reasons}")

    print(f"\n--- [3단계] 공급사 선정 및 RFQ(견적요청) 생성 ---")
    selected_suppliers, ranked_info = erp_client.select_suppliers_for_rfq(TEST_ITEM_CODE, max_suppliers=2)
    print(f"📌 선정된 공급사 리스트: {selected_suppliers}")
    print(f"📌 AI/규칙 기반 선정 이유: {ranked_info}")

    if selected_suppliers and mr:
        print(f"견적요청서(RFQ) 생성 시도 중...")
        rfq = erp_client.create_rfq_from_material_request(mr["name"], selected_suppliers)
        if rfq:
            print(f"✅ RFQ 생성 및 Submit 완료! 문서 이름: {rfq['name']}")
            
            # 실제 이메일/내장 발송 테스트 (TEST_MODE가 true면 안전하게 콘솔 출력됨)
            print(f"공급사들에게 RFQ 발송 시도 중...")
            try:
                erp_client.send_rfq_native(rfq["name"])
                print("✅ RFQ 발송 프로세스 완료!")
            except Exception as e:
                print(f"⚠️ RFQ 발송 중 안내/에러 발생 (포털 계정 설정 등 확인 필요): {e}")
        else:
            print("❌ RFQ 생성 실패")

    print("\n🎉 1단계~3단계 실전 테스트 사이클이 완료되었습니다!")
    print("실제 ERPNext UI에 들어가셔서 방금 생성된 MR과 RFQ 문서가 잘 들어와 있는지 확인해 보세요.")

if __name__ == "__main__":
    run_real_e2e_test()