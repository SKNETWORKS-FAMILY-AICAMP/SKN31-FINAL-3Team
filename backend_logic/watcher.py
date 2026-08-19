"""
watcher.py — 본인 담당 파트 (interrupt 대응 버전)

새 Material Request를 주기적으로 감지해서 그래프에 넘김.
그래프가 사람 입력이 필요해서 interrupt로 멈추면, 여기서 안 기다리고
pending_reviews.json에 "대기중" 기록만 남긴 뒤 바로 다음 MR로 넘어감.
사람은 나중에 resume_pending.py로 밀린 것들을 따로 처리함.

실행: python watcher.py
"""

import json
import os
import time

from erp_client import get_pending_material_requests, get_material_request_detail
from pipeline_graph import app

POLL_INTERVAL_SECONDS = 300
# ⚠️ 상대경로 대신, 이 파일(watcher.py) 위치 기준 절대경로로 고정
# (resume_pending.py도 똑같이 고정해야 서로 같은 파일을 보게 됨)
PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_reviews.json")

processed_mr_names = set()   # 이미 그래프에 넘긴 MR (완료든 대기중이든 상관없이)


def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {}
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pending(pending):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def watch_for_new_material_requests():
    print("Material Request 감시 시작...")
    while True:
        try:
            pending_mrs = get_pending_material_requests()
        except Exception as e:
            print(f"[에러] 조회 실패, 다음 주기에 재시도: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        for mr in pending_mrs or []:
            if mr["name"] in processed_mr_names:
                continue

            print(f"\n새 Material Request 발견: {mr['name']} → 그래프로 넘김")
            mr_detail = get_material_request_detail(mr["name"])
            if not mr_detail or not mr_detail.get("items"):
                continue

            # 품목이 여러 줄이면 각각 따로 처리. thread_id에 품목코드까지
            # 붙여서, 같은 MR 안 여러 품목이 서로 다른 실행으로 구분되게 함.
            for item in mr_detail["items"]:
                print(f"  → 품목 처리: {item['item_code']} (수량 {item['qty']})")

                initial_state = {
                    "item_code": item["item_code"],
                    "item_name": "",
                    "qty": item["qty"],
                    "warehouse": item["warehouse"],
                    "mr_name": mr["name"],
                    "stock_check": "",
                    "candidates": [],
                    "final_substitute": None,
                    "substitute_check": "",
                    "substitute_item": None,
                    "route": "",
                    "reasons": [],
                    "bidding_decision": {},
                    "result_message": "",
                }

                # thread_id = MR이름 + 품목코드. 품목별로 독립적인 실행/재개가 되게 함.
                thread_id = f"{mr['name']}::{item['item_code']}"
                config = {"configurable": {"thread_id": thread_id}}
                result = app.invoke(initial_state, config=config)

                if "__interrupt__" in result:
                    # 멈췄음 — 기다리지 않고 기록만 남기고 바로 다음 품목으로 넘어감
                    interrupt_payload = result["__interrupt__"][0].value
                    print(f"⏸  '{thread_id}' 사람 입력 대기중 (나중에 resume_pending.py로 처리)")

                    pending = load_pending()
                    pending[thread_id] = interrupt_payload
                    save_pending(pending)
                else:
                    print(f"✅ 처리 완료: {result['result_message']}")

            processed_mr_names.add(mr["name"])

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    watch_for_new_material_requests()