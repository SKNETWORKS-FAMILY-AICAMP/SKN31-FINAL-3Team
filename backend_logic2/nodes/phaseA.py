"""
nodes/run_intake_pipeline.py — 진입점 A: MR 처리 시작

순서: 이상수치판단 → 승인/반려 또는 대체품추천 → 승인/반려 → 비딩판단
      → 공급사확보 → RFQ발송
각 단계에서 "여기서 끝날 수 있는" 조건을 확인하고, 끝까지 가면 RFQ
발송까지 완료함. (견적 도착은 며칠 걸리므로, 그 이후는 진입점 B에서
별도로 확인 — RFQ명을 꼭 기록해두세요)

⚠️ 각 단계는 이미 만든 모듈들을 그대로 재사용함 — 이 파일 자체는 새 로직
없이 순서만 정하는 오케스트레이터.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/ 안에
이 파일과 나머지 노드 파일들이 다 같이 있어야 함 (서로 import함).

실행: python nodes/run_intake_pipeline.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from find_substitute import find_substitutes_for_mr
from anomaly_detection import detect_material_request_anomalies
from approval_review import review_material_request
from decide_bidding import decide_bidding
from resolve_supplier import resolve_suppliers_for_mr, resolve_suppliers_for_item
from register_candidate_suppliers import register_candidate_suppliers
from send_rfq import create_and_send_rfq
from erp_client import erp_get_one


def run_intake_pipeline(
    mr_name: str,
    review_decision: str | None = None,
    rejection_reason: str | None = None,
):
    print(f"\n{'#'*60}")
    print(f"# MR '{mr_name}' 처리 시작")
    print(f"{'#'*60}")

    # ── 1단계: 이상 수치 판단 ──
    print("\n[1단계] 요청 수량·일자 이상 여부 판단 중...")
    anomaly_result = detect_material_request_anomalies(mr_name)

    if anomaly_result["has_anomaly"]:
        review = review_material_request(
            mr_name,
            anomaly_result=anomaly_result,
            decision=review_decision,
            rejection_reason=rejection_reason,
        )
        if review["decision"] == "rejected":
            print("\n→ 이상 수치 검토 결과 반려되어 구매 프로세스를 종료합니다.")
            return {"status": "rejected", "review": review}
        sub_results = {}
    else:
        # ── 2단계: 대체품 확인 ──
        print("\n[2단계] 대체 가능한 재고 확인 중...")
        sub_results = find_substitutes_for_mr(mr_name)
        review = review_material_request(
            mr_name,
            anomaly_result=anomaly_result,
            substitute_results=sub_results,
            decision=review_decision,
            rejection_reason=rejection_reason,
        )
        if review["decision"] == "rejected":
            print("\n→ 대체품 검토 결과 반려되어 구매 프로세스를 종료합니다.")
            return {
                "status": "rejected",
                "review": review,
                "substitutes": sub_results,
            }

    # ── 3단계: MR 업무 분담 (카테고리/Role Profile 매칭) ──
    print("\n[3단계] MR 업무 분담 및 담당자 자동 배정 중...")
    assignment_info = assign_material_request(mr_name)

    # ── 4단계: 비딩 필요 여부 판단 ──
    print("\n[4단계] 비딩 필요 여부 판단 중...")
    bidding_results = decide_bidding(mr_name)

    bidding_items = []
    for item_code, info in (bidding_results or {}).items():
        status = "비딩 필요" if info["needs_bidding"] else "비딩 불필요(카탈로그 등 기존절차)"
        print(f"  [{item_code}] {status}")
        for r in info["reasons"]:
            print(f"    - {r}")
        if info["needs_bidding"]:
            bidding_items.append(item_code)

    if not bidding_items:
        print("\n→ 비딩 필요 품목이 없습니다. 카탈로그/기존절차로 처리하세요 (이 파이프라인 범위 밖).")
        return None

    # ── 4단계: 공급사 확보 ──
    print("\n[4단계] 공급사 확보 중...")
    supplier_results = resolve_suppliers_for_mr(mr_name)

    final_supplier_names = []
    for item_code, info in (supplier_results or {}).items():
        print(f"\n  [{item_code}] 출처: {info['source']}")

        if info["source"] == "existing":
            for s in info["suppliers"]:
                print(f"    - {s} (기존 승인공급사)")
            final_supplier_names.extend(info["suppliers"])

            # 기존 공급사가 있어도, 더 다양한 후보를 원할 수 있으니 확인
            also_search = input(
                f"    → '{item_code}'는 이미 거래한 공급사가 {len(info['suppliers'])}곳 있습니다. "
                f"그래도 신규 풀 탐색을 진행할까요? (y/n): "
            ).strip().lower()

            if also_search == "y":
                item = erp_get_one("Item", item_code)
                item_name = item.get("item_name", item_code) if item else item_code
                new_result = resolve_suppliers_for_item(item_code, item_name, force_new_search=True)
                print(f"    신규 후보 {len(new_result['suppliers'])}건 발견, 등록 중...")
                registered = register_candidate_suppliers(new_result["suppliers"])
                for r in registered:
                    print(f"      - {r['name']}: {r['status']}")
                    if r["status"] in ("created", "already_exists") and r["name"]:
                        final_supplier_names.append(r["name"])
        else:
            print("  신규 후보 등록 중...")
            registered = register_candidate_suppliers(info["suppliers"])
            for r in registered:
                print(f"    - {r['name']}: {r['status']}")
                if r["status"] in ("created", "already_exists") and r["name"]:
                    final_supplier_names.append(r["name"])

    final_supplier_names = list(set(final_supplier_names))

    if not final_supplier_names:
        print("\n→ 공급사 후보를 찾지 못했습니다. 수동 확인이 필요합니다.")
        return None

    # ── 5단계: RFQ 생성 + 발송 ──
    print(f"\n[5단계] RFQ 발송 대상 공급사: {', '.join(final_supplier_names)}")
    confirm = input(f"\n위 {len(final_supplier_names)}개 공급사에게 RFQ를 발송할까요? (y/n): ").strip().lower()
    if confirm != "y":
        print("\n→ 사용자가 취소했습니다. RFQ 발송 안 함.")
        return None

    rfq = create_and_send_rfq(mr_name, final_supplier_names)
    if rfq:
        print(f"\n{'#'*60}")
        print(f"# 완료: RFQ '{rfq['name']}' 생성 및 발송됨")
        print(f"# 며칠 후 진입점 B(견적확인)에서 이 RFQ 이름으로 진행상황을 확인하세요.")
        print(f"{'#'*60}")
    return rfq


if __name__ == "__main__":
    mr_name = input("처리할 Material Request ID 입력: ").strip()
    run_intake_pipeline(mr_name)
