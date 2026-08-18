"""
resume_pending.py — 밀린 "사람 확인 필요" 건들을 나중에 처리하는 도구

⚠️ 지금은 대시보드가 없어서 만든 임시 CLI 도구. 나중에 웹 대시보드
만들면 이 스크립트는 없어지고, 그 화면에서 같은 일을 하게 됨
(app.invoke(Command(resume=...))를 호출하는 부분은 그대로 재사용 가능).

실행: python resume_pending.py
"""

import json
import os

from langgraph.types import Command
from pipeline_graph import app

PENDING_FILE = "pending_reviews.json"


def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {}
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pending(pending):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def main():
    pending = load_pending()
    if not pending:
        print("대기중인 건이 없습니다.")
        return

    print(f"=== 대기중인 확인 요청 {len(pending)}건 ===\n")
    mr_names = list(pending.keys())
    for idx, mr_name in enumerate(mr_names, start=1):
        info = pending[mr_name]
        print(f"{idx}. [{mr_name}] {info.get('item_name')} 대체품 확인 필요")

    print()
    pick = input("처리할 번호 선택 (그냥 Enter=종료): ").strip()
    if not pick:
        return

    mr_name = mr_names[int(pick) - 1]
    info = pending[mr_name]

    print(f"\n'{info.get('item_name')}' 품절! 대체품을 선택해주세요.")
    for idx, cand in enumerate(info.get("candidates", []), start=1):
        print(f" {idx}. {cand['name']} (잔여 재고: {cand['qty']}개)  *코드: {cand['code']}")
    print(" 0. 적절한 대체품 없음 (신규 발주 진행)")

    choice = int(input(">> 번호 입력: "))

    # 여기서 멈춰있던 그래프를 이어서 실행 (thread_id로 어느 건인지 찾음)
    config = {"configurable": {"thread_id": mr_name}}
    result = app.invoke(Command(resume=choice), config=config)

    print(f"\n✅ 처리 완료: {result.get('result_message', '(완료)')}")

    # 처리된 건은 대기목록에서 제거
    del pending[mr_name]
    save_pending(pending)


if __name__ == "__main__":
    main()