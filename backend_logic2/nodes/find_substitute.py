"""
nodes/find_substitute_item.py — 대체품 추천 모듈

find_duplicate_stock.py랑 다른 목적:
  - find_duplicate_stock: "이름만 다른 같은 물건" 찾기 (예: 장갑 == 장갑#103)
  - 이 모듈: "진짜 다른 물건인데, 변형(색상·규격 등)만 다르고 용도가 같아서
    대신 쓸 수 있는 것" 찾기 (예: 안전모(백색) 품절 → 안전모(황색) 대체가능)

⚠️ 회사마다 표기방식이 다 달라서(괄호, ##, -, No. 등) 정규식 패턴 하나로는
   커버 불가능함. 그래서 AI를 품목당 딱 1번만 써서 "핵심 물건 이름"을
   뽑아내고, 그 이후 검색·필터링은 순수 문자열 포함여부(정규식 아님, 그냥
   in 연산)로만 처리함 — AI 호출 최소화.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/find_substitute_item.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_get, erp_get_one


def _get_base_name_ai(item_name: str) -> str:
    """
    AI로 품목명에서 핵심 단어(기본 품목명)만 뽑아냄.
    회사마다 표기방식이 다 달라서(괄호, ##, -, No. 등) 정규식 하나로
    커버가 안 됨 — AI가 표기방식 상관없이 "이게 진짜 무슨 물건인지" 핵심
    단어를 뽑아주고, 그 이후 검색은 순수 문자열 포함여부(정규식)로만
    처리해서 AI 호출을 품목당 딱 1번으로 제한함 (비용 절감).
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 회사 내부에서 쓰는 품목명입니다. 색상·규격·사이즈·관리번호 등 "
        "부가정보를 다 떼어내고, 핵심 물건 이름만 뽑아주세요.\n\n"
        "품목명: {item_name}\n\n"
        "예시:\n"
        "  안전모(백색) -> 안전모\n"
        "  안전모##1001 -> 안전모\n"
        "  장갑-L -> 장갑\n"
        "  연마재 No.132 -> 연마재\n\n"
        "핵심 이름만 답하세요, 다른 설명이나 문장부호 없이 단어만."
    )
    result = (prompt | llm).invoke({"item_name": item_name}).content
    return result.strip()


def _get_last_purchase_rate(item_code):
    """이 품목의 가장 최근 구매단가 (decide_bidding.py와 같은 패턴 재사용)"""
    orders = erp_get(
        "Purchase Order",
        filters=[
            ["Purchase Order Item", "item_code", "=", item_code],
            ["docstatus", "=", 1],
        ],
        fields=["name", "transaction_date"],
    )
    if not orders:
        return None

    latest = sorted(orders, key=lambda o: o["transaction_date"])[-1]
    latest_po = erp_get_one("Purchase Order", latest["name"])
    for item_line in latest_po.get("items", []):
        if item_line["item_code"] == item_code:
            return item_line.get("rate")
    return None


def find_substitute_items(item_code: str, qty_needed) -> list:
    """
    특정 품목의 대체품 후보를 찾음.
    같은 기본품목명(base_name)이면서, 변형(전체이름)은 다르고,
    요청수량보다 재고 있는 것들.

    반환: [{"item_code", "item_name", "warehouse", "actual_qty", "last_rate"}, ...]
    """
    item = erp_get_one("Item", item_code)
    if not item:
        return []

    item_name = item.get("item_name", item_code)
    base = _get_base_name_ai(item_name)  # AI 호출은 여기 딱 한 번
    candidates = erp_get(
        "Item",
        filters=[["item_name", "like", f"%{base}%"]],
        fields=["item_code", "item_name", "description"],
    )

    results = []
    for c in candidates or []:
        if c["item_code"] == item_code:
            continue  # 요청한 그 품목 자체는 제외
        if base not in c["item_name"]:
            continue  # AI가 뽑은 핵심단어가 실제로 포함된 것만 (like는 대소문자 등 느슨할 수 있어 재확인)
        if c["item_name"] == item_name:
            continue  # 완전히 이름이 똑같으면 대체품이 아니라 그냥 같은 물건

        bins = erp_get(
            "Bin",
            filters=[["item_code", "=", c["item_code"]], ["actual_qty", ">", 0]],
            fields=["warehouse", "actual_qty"],
        )
        for b in bins or []:
            results.append({
                "item_code": c["item_code"],
                "item_name": c["item_name"],
                "description": c.get("description"),
                "warehouse": b["warehouse"],
                "actual_qty": b["actual_qty"],
                "fulfills_full_qty": b["actual_qty"] >= qty_needed,
                "last_rate": _get_last_purchase_rate(c["item_code"]),
            })

    return results


def find_substitutes_for_mr(mr_name: str) -> dict:
    """MR 안의 각 품목마다 find_substitute_items() 실행"""
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return {}

    results = {}
    for line in mr.get("items", []):
        item_code = line["item_code"]
        qty_needed = line["qty"]
        results[item_code] = {
            "qty_needed": qty_needed,
            "substitutes": find_substitute_items(item_code, qty_needed),
        }

    return results


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    results = find_substitutes_for_mr(mr_name)

    if not results:
        print("해당 MR을 찾을 수 없거나 품목이 없습니다.")

    for item_code, info in results.items():
        original = erp_get_one("Item", item_code)
        print(f"\n{'='*50}")
        print(f"[요청품목] {item_code} — {original.get('item_name') if original else '?'}")
        print(f"  설명: {original.get('description') or '(설명 없음)'}" if original else "")
        print(f"  요청수량: {info['qty_needed']}")
        print(f"{'='*50}")

        if not info["substitutes"]:
            print("  대체품 후보 없음")

        for s in info["substitutes"]:
            rate_disp = f"{s['last_rate']:,.0f}원" if s["last_rate"] is not None else "구매이력 없음"
            fulfill_disp = "✅ 전량충족" if s["fulfills_full_qty"] else f"⚠️ 부분충족({s['actual_qty']}개만 있음)"

            print(f"\n  ─────────────────────────────")
            print(f"  {s['item_name']} ({s['item_code']})")
            print(f"  창고: {s['warehouse']} | 재고: {s['actual_qty']} | {fulfill_disp}")
            print(f"  최근단가: {rate_disp}")
            print(f"  설명: {s['description'] or '(설명 없음)'}")