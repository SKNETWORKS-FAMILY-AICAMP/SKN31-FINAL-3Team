"""
watcher.py — 본인 담당 파트

새 Material Request를 주기적으로 감지해서, 있으면 바로 그래프(app)에
넘겨서 실행시킴. 분기 판단은 여기서 안 함 — 그냥 넘기기만 함.

실행: python watcher.py
"""

import time

from erp_client import get_pending_material_requests, get_material_request_detail
from pipeline_graph import app

POLL_INTERVAL_SECONDS = 300  # 5분마다 확인. 필요시 조정
processed_mr_names = set()   # 이미 넘긴 MR 기억 (중복 실행 방지)


def watch_for_new_material_requests():
    print("Material Request 감시 시작...")
    while True:
        try:
            pending = get_pending_material_requests()
        except Exception as e:
            print(f"[에러] 조회 실패, 다음 주기에 재시도: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        for mr in pending or []:
            if mr["name"] in processed_mr_names:
                continue

            print(f"\n새 Material Request 발견: {mr['name']} → 그래프로 넘김")
            mr_detail = get_material_request_detail(mr["name"])
            if not mr_detail or not mr_detail.get("items"):
                continue

            # 품목 여러 줄이면 지금은 첫 줄만 처리 (필요시 반복문으로 확장)
            item = mr_detail["items"][0]

            initial_state = {
                "item_code": item["item_code"],
                "qty": item["qty"],
                "warehouse": item["warehouse"],
                "mr_name": mr["name"],
                "substitute_check": "",
                "substitute_item": None,
                "route": "",
                "result_message": "",
            }

            # 분기 판단 없이, 그래프에 그대로 넘겨서 실행시킴
            final_state = app.invoke(initial_state)
            print(f"처리 완료: {final_state['result_message']}")

            processed_mr_names.add(mr["name"])

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    watch_for_new_material_requests()