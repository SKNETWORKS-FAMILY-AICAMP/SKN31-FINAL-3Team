"""
nodes/find_duplicate_stock.py — 2번 모듈:
Material Request 품목이랑 "사실상 같은 이름"인데 다른 item_code로 등록되어
재고가 숨어있는 걸 찾음. (예: "장갑" == "장갑#103")

⚠️ 진짜 "대체품"(비슷하지만 다른 물건)을 찾는 게 아님 — 그건 다음 모듈에서
따로 다룸. 이건 순수하게 "이름 중복등록"만 잡아내는 용도.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/find_duplicate_stock.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from erp_client import erp_get, erp_get_one


def _core_name(item_name: str) -> str:
    """
    이름 뒤에 붙은 일련번호/코드 성격의 접미사를 떼어내서 핵심 이름만 남김.
    예: "장갑#103" -> "장갑", "연마재 #132" -> "연마재", "장갑(103)" -> "장갑"

    여유있게 잡되(다양한 표기방식 다 커버), 진짜 이름의 일부를 잘라내지
    않도록 "끝에 붙은 숫자 코드"만 대상으로 함 (이름 중간의 숫자는 안 건드림 —
    예: "3M 안전모"의 "3"은 안 지워짐).
    """
    core = item_name
    core = re.sub(r"\s*#\s*\d+\s*$", "", core)         # "장갑#103", "장갑 # 103"
    core = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", core)    # "장갑(103)"
    core = re.sub(r"\s*[-_]\s*\d+\s*$", "", core)       # "장갑-103", "장갑_103"
    core = re.sub(r"\s*No\.?\s*\d+\s*$", "", core, flags=re.IGNORECASE)  # "장갑 No.103"
    return core.strip()


def _get_item_name(item_code: str, fallback_name: str = None) -> str:
    """MR 품목행에 item_name이 없을 수도 있어서, 없으면 Item에서 따로 조회"""
    if fallback_name:
        return fallback_name
    item = erp_get_one("Item", item_code)
    return item.get("item_name", item_code) if item else item_code


def _ai_confirm_same_item(name_a: str, name_b: str) -> tuple:
    """
    정규식으로 '핵심이름 일치'까지 걸러낸 두 품목이, 후보로 보여줄 만큼
    사실상 같은 물건인지 AI로 재확인.

    ⚠️ 완벽히 동일해야만 통과시키는 게 아님 — 최종 선택은 사람이 하니까,
    여기서는 "후보 목록에 넣어줄 만한지" 정도만 적당히 걸러내면 됨.
    종류/용도 자체가 다른 물건만 걸러내고, 애매하면 후보로 포함시킴
    (너무 빡빡하면 사람이 볼 후보 자체가 안 뜨는 문제가 있었음).

    반환: (같음 여부: bool, 이유: str)
    """
    import json
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음 두 품목명이 사실상 같은 물건(이름만 다르게 중복등록됐을 가능성)인지 "
        "판단하세요. 최종 선택은 사람이 하므로, 여기서는 '후보로 보여줄 만한지'만 "
        "적당히 판단하면 됩니다.\n\n"
        "품목명 A: {name_a}\n"
        "품목명 B: {name_b}\n\n"
        "규칙:\n"
        "- 완벽히 동일할 필요는 없습니다. 같은 종류의 물건이고, 이름 차이가 "
        "등록번호·코드·사소한 표기 차이 정도로 보이면 '같음'으로 답하세요.\n"
        "- 종류나 용도 자체가 다른 물건(예: 안전모 vs 안전화)일 때만 '다름'으로 답하세요.\n"
        "- 애매하면 '같음'(후보로 포함) 쪽으로 판단하세요. 사람이 마지막에 걸러낼 겁니다.\n"
        '- 반드시 이 JSON 형식으로만 답하세요: {{"same": true 또는 false, "reason": "짧은 이유"}}'
    )
    result_text = (prompt | llm).invoke({"name_a": name_a, "name_b": name_b}).content

    try:
        cleaned = result_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        return bool(parsed.get("same", False)), parsed.get("reason", "")
    except Exception:
        return True, "AI 응답 파싱 실패 (일단 후보로 포함, 사람이 판단)"


def find_duplicate_stock(mr_name: str) -> dict:
    """
    MR 안의 각 품목마다, 핵심이름(_core_name)이 정확히 같으면서 item_code는
    다르고, 요청수량보다 재고(Bin)가 많은 후보들을 찾아서 돌려줌.

    ⚠️ 2단계 검증: ① 정규식으로 핵심이름 일치 → ② AI로 "진짜 같은 물건인지"
    엄격하게 재확인. 둘 다 통과해야 최종 후보로 인정함.

    반환: {item_code: [{"item_code", "item_name", "warehouse", "actual_qty"}, ...]}
    """
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return {}

    results = {}

    for line in mr.get("items", []):
        item_code = line["item_code"]
        qty_needed = line["qty"]
        item_name = _get_item_name(item_code, line.get("item_name"))
        target_core = _core_name(item_name)

        # ERPNext 자체 필터(like)로 1차 후보를 넓게 좁힘 (Item 전체를 다 안 훑으려고)
        candidates = erp_get(
            "Item",
            filters=[["item_name", "like", f"%{target_core}%"]],
            fields=["item_code", "item_name"],
        )

        matches = []
        for c in candidates or []:
            if c["item_code"] == item_code:
                continue  # 요청한 그 품목 자체는 제외
            if _core_name(c["item_name"]) != target_core:
                continue  # 정규식으로 정리한 핵심이름이 정확히 일치하는 것만

            # 2단계: AI로 "진짜 같은 물건인지" 엄격하게 재확인
            is_same, reason = _ai_confirm_same_item(item_name, c["item_name"])
            if not is_same:
                print(f"  '{c['item_name']}' 이름은 비슷하지만 AI가 다른 물건으로 판단: {reason}")
                continue

            bins = erp_get(
                "Bin",
                filters=[["item_code", "=", c["item_code"]], ["actual_qty", ">", qty_needed]],
                fields=["warehouse", "actual_qty"],
            )
            for b in bins or []:
                matches.append({
                    "item_code": c["item_code"],
                    "item_name": c["item_name"],
                    "warehouse": b["warehouse"],
                    "actual_qty": b["actual_qty"],
                })

        results[item_code] = {
            "item_name": item_name,
            "qty_needed": qty_needed,
            "matches": matches,
        }

    return results


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    results = find_duplicate_stock(mr_name)

    if not results:
        print("해당 MR을 찾을 수 없거나 품목이 없습니다.")

    for item_code, info in results.items():
        print(f"\n[{item_code}] {info['item_name']} (요청수량: {info['qty_needed']})")
        if not info["matches"]:
            print("  숨은 동일품목 재고 없음")
        for m in info["matches"]:
            print(f"  - {m['item_name']} ({m['item_code']}) | 창고: {m['warehouse']} | 재고: {m['actual_qty']}")