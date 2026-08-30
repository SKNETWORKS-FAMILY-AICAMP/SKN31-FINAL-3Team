"""
nodes/request_new_item.py — 신규 품목 승인 관리 (터미널 기반)

⚠️ 전제조건: ERPNext에서 Item의 "Disabled" 필드가 Read Only + 기본값 1로
설정되어 있어야 함. 다른 부서가 ERPNext에서 직접 Item을 만들어도
자동으로 비활성 상태로 생성되고, 화면에서 직접 못 건드림 —
활성화는 오직 이 스크립트(승인)를 통해서만 가능.

흐름:
  ① 요청부서: ERPNext에서 직접 Item 생성 (자동으로 disabled=1)
  ② 이 스크립트 실행 → 대기중인 품목 번호목록으로 조회
  ③ 번호 선택 → 상세정보 보기
  ④ 승인(disabled=0) 또는 보류(그대로 둠) 선택

⚠️ 권한 참고: 지금은 공용 API키 하나로 돌아가서 "진짜 구매부서인지"를
코드가 완벽히 검증 못 함. department 입력은 최소 확인장치일 뿐,
진짜 보안은 실사용자 로그인 붙을 때 ERPNext 권한으로 통제해야 함.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/request_new_item.py
"""

import sys
import os

import requests
from backend_logic2.integrations.erp_client import erp_get, erp_get_one, ERPNextAPIError, SITE_URL, HEADERS


def get_pending_item_requests():
    """비활성(disabled=1) Item들을 조회 — 승인 대기 목록"""
    items = erp_get(
        "Item",
        filters=[["disabled", "=", 1]],
        fields=["item_code", "item_name", "creation"],
    )
    return items or []


def print_item_list(items):
    """번호 매겨서 목록 출력"""
    print(f"\n=== 승인 대기중인 신규 품목 ({len(items)}건) ===")
    for i, item in enumerate(items, start=1):
        print(f"  {i}. {item['item_code']} — {item['item_name']}  (등록일: {item['creation']})")


def print_item_detail(item):
    """선택된 품목의 상세정보를 사람이 읽기 좋게 출력"""
    print(f"\n{'='*55}")
    print(f" 품목코드   : {item.get('item_code')}")
    print(f" 품목명     : {item.get('item_name')}")
    print(f" 품목분류   : {item.get('item_group')}")
    print(f" 단위(UOM)  : {item.get('stock_uom')}")
    print(f" 등록일     : {item.get('creation')}")
    print(f"{'-'*55}")
    print(f" 설명:\n{item.get('description') or '(없음)'}")
    print(f"{'='*55}\n")


def approve_item_request(item_code):
    """구매부서 승인 — disabled=0으로 활성화.
    이 스크립트 자체가 구매부서 전용 도구라, 부서 재확인 안 함."""
    res = requests.put(
        f"{SITE_URL}/api/resource/Item/{item_code}",
        headers=HEADERS,
        json={"disabled": 0},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"승인(활성화) 실패: {res.status_code} - {res.text[:300]}")

    print(f"✅ 승인 완료 — '{item_code}' 활성화됨")
    return res.json().get("data")


if __name__ == "__main__":
    while True:
        pending = get_pending_item_requests()

        if not pending:
            print("\n승인 대기중인 품목이 없습니다. 종료합니다.")
            break

        print_item_list(pending)
        choice = input("\n상세보기할 번호 입력 (종료하려면 그냥 엔터): ").strip()

        if not choice:
            break

        try:
            idx = int(choice) - 1
            selected = pending[idx]
        except (ValueError, IndexError):
            print("잘못된 번호입니다. 다시 선택해주세요.")
            continue

        detail = erp_get_one("Item", selected["item_code"])
        print_item_detail(detail)

        action = input("승인(y) / 보류(n): ").strip().lower()

        if action == "y":
            approve_item_request(selected["item_code"])
        else:
            print("보류되었습니다. (여전히 대기목록에 남아있습니다)")