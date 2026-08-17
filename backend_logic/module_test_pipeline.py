"""
erp_client.py 함수 테스트 스크립트

사용법:
1. 이 파일을 erp_client.py와 같은 폴더에 두기
2. 아래 "설정값" 부분을 본인 실제 데이터(품목코드, 창고명, 공급사명)로 수정
3. python test_pipeline.py 실행

각 테스트는 독립적으로 try/except로 감싸져 있어서, 하나 실패해도
나머지 테스트는 계속 진행됨. 끝나면 요약이 마지막에 출력됨.
"""

from erp_client import (
    get_stock_level,
    get_reorder_settings,
    check_reorder_needed,
    create_material_request,
    get_pending_material_requests,
    get_material_request_detail,
    is_bidding_required,
    create_rfq_from_material_request,
    send_rfq_to_suppliers,
    get_quotations_for_rfq,
    get_lowest_quotation,
    create_po_from_quotation,
    send_po_to_supplier,
)

# ===== 설정값: 본인 실제 데이터로 수정하세요 =====
TEST_ITEM_CODE = "SF-001"
TEST_WAREHOUSE = "api_test용 - SKN31"
TEST_SUPPLIERS = ["대한안전산업", "한빛보호구"]
TEST_SUPPLIER_EMAILS = "contact@daehan-safety.test,sales@hanbit-gear.test"
# ==================================================

results = []  # (테스트이름, 성공여부, 비고) 저장


def run_test(name, func):
    """테스트 하나 실행하고 결과 기록. 실패해도 스크립트는 계속 진행됨."""
    print(f"\n{'='*50}")
    print(f"[테스트] {name}")
    print("=" * 50)
    try:
        result = func()
        print(f"결과: {result}")
        results.append((name, True, ""))
        return result
    except Exception as e:
        print(f"❌ 실패: {e}")
        results.append((name, False, str(e)))
        return None


# ---------- 1. 재고 조회 ----------

stock = run_test(
    "1-1. 재고 조회 (get_stock_level)",
    lambda: get_stock_level(TEST_ITEM_CODE, TEST_WAREHOUSE),
)

reorder = run_test(
    "1-2. 재주문 기준 조회 (get_reorder_settings)",
    lambda: get_reorder_settings(TEST_ITEM_CODE, TEST_WAREHOUSE),
)

needed_result = run_test(
    "1-3. 재주문 필요 여부 판단 (check_reorder_needed)",
    lambda: check_reorder_needed(TEST_ITEM_CODE, TEST_WAREHOUSE),
)


# ---------- 2. Material Request ----------

mr = run_test(
    "2-1. Material Request 생성 (create_material_request)",
    lambda: create_material_request(
        TEST_ITEM_CODE, qty=50, warehouse=TEST_WAREHOUSE, schedule_date="2026-09-15"
    ),
)
mr_name = mr["name"] if mr else None

pending_mrs = run_test(
    "2-2. 미처리 Material Request 조회 (get_pending_material_requests)",
    lambda: get_pending_material_requests(),
)

if mr_name:
    mr_detail = run_test(
        "2-3. Material Request 상세조회 (get_material_request_detail)",
        lambda: get_material_request_detail(mr_name),
    )

    if mr_detail:
        run_test(
            "2-4. 카탈로그/비딩 판별 (is_bidding_required)",
            lambda: is_bidding_required(mr_detail["items"][0]),
        )
else:
    print("\n⚠️ 2-1이 실패해서 2-3, 2-4는 건너뜁니다.")


# ---------- 3. RFQ ----------

rfq = None
if mr_name:
    rfq = run_test(
        "3-1. RFQ 생성 (create_rfq_from_material_request)",
        lambda: create_rfq_from_material_request(mr_name, TEST_SUPPLIERS),
    )
else:
    print("\n⚠️ Material Request가 없어서 RFQ 테스트는 건너뜁니다.")

rfq_name = rfq["name"] if rfq else None

if rfq_name:
    run_test(
        "3-2. RFQ 이메일 발송 (send_rfq_to_suppliers)",
        lambda: send_rfq_to_suppliers(rfq_name, TEST_SUPPLIER_EMAILS),
    )


# ---------- 4. 견적 조회/비교 (공급사가 아직 견적을 안 넣었으면 빈 값 나오는 게 정상) ----------

if rfq_name:
    quotations = run_test(
        "4-1. RFQ에 대한 견적 목록 조회 (get_quotations_for_rfq)",
        lambda: get_quotations_for_rfq(rfq_name),
    )

    lowest = run_test(
        "4-2. 최저가 견적 선택 (get_lowest_quotation)",
        lambda: get_lowest_quotation(rfq_name),
    )
else:
    print("\n⚠️ RFQ가 없어서 견적 테스트는 건너뜁니다.")
    lowest = None


# ---------- 5. PO (견적이 실제로 있어야 테스트 가능) ----------

if lowest:
    po = run_test(
        "5-1. PO 생성 (create_po_from_quotation)",
        lambda: create_po_from_quotation(lowest["name"], schedule_date="2026-09-30"),
    )
    if po:
        run_test(
            "5-2. PO 이메일 발송 (send_po_to_supplier)",
            lambda: send_po_to_supplier(po["name"], TEST_SUPPLIER_EMAILS.split(",")[0]),
        )
else:
    print("\n⚠️ 아직 제출된 견적이 없어서 PO 테스트는 건너뜁니다.")
    print("   (공급사 포털에서 견적을 하나 제출한 뒤 이 스크립트를 다시 실행해보세요)")


# ---------- 최종 요약 ----------

print(f"\n\n{'='*50}")
print("전체 테스트 요약")
print("=" * 50)
success_count = sum(1 for _, ok, _ in results if ok)
print(f"성공: {success_count} / {len(results)}\n")

for name, ok, err in results:
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}")
    if err:
        print(f"    → {err[:150]}")