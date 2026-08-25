"""
nodes/find_substitute_item.py — 대체품 추천 모듈

find_duplicate_stock.py랑 다른 목적:
  - find_duplicate_stock: "이름만 다른 같은 물건" 찾기 (예: 장갑 == 장갑#103)
  - 이 모듈: "진짜 다른 물건인데, 변형(색상·규격 등)만 다르고 용도가 같아서
    대신 쓸 수 있는 것" 찾기 (예: 안전모(백색) 품절 → 안전모(황색) 대체가능)

⚠️ 회사마다 표기방식이 다 달라서(괄호, ##, -, No. 등 접미사형 / 등급·품질
   수식어 등 접두사형) 정규식 패턴만으로는 edge case를 다 못 잡음. 그래서
   핵심 물건 이름 추출에만 AI를 품목당 딱 1번 씀. 그 이후 후보 그룹화는
   ① 공백정규화 → ② 문자열 유사도(difflib, AI 아님) 순으로 처리해서
   AI 호출을 최소화함.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/find_substitute_item.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_get, erp_get_one


def _get_core_keyword(item_name: str) -> str:
    """
    AI로 품목명에서 핵심 단어(기본 품목명)만 뽑아냄.
    회사마다 표기방식이 다 달라서(괄호, ##, -, No. 등 접미사형 / 등급·품질
    수식어 등 접두사형) 정규식 패턴만으로는 edge case를 다 못 잡음 —
    AI가 표기방식·위치 상관없이 "이게 진짜 무슨 물건인지" 핵심 명사를
    판단해서 뽑아줌. AI 호출은 품목당 딱 1번으로 제한(비용 절감), 그 이후
    검색·필터링은 순수 문자열 포함여부로만 처리함.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 회사 내부에서 쓰는 품목명입니다. 색상·규격·사이즈·관리번호·등급·"
        "품질수식어 등 부가정보를 다 떼어내고, 핵심 물건 이름(명사)만 뽑아주세요.\n\n"
        "⚠️ 부가정보는 이름 뒤에 붙을 수도, 앞에 붙을 수도 있습니다. 위치와 "
        "상관없이 다 떼어내고 핵심 명사만 남기세요.\n\n"
        "품목명: {item_name}\n\n"
        "예시 (뒤에 붙는 경우):\n"
        "  안전모(백색) -> 안전모\n"
        "  안전모##1001 -> 안전모\n"
        "  장갑-L -> 장갑\n"
        "  연마재 No.132 -> 연마재\n\n"
        "예시 (앞에 붙는 경우, 등급·품질 수식어):\n"
        "  스탠다드 사무용 의자 -> 의자\n"
        "  프리미엄 메쉬 의자 -> 의자\n"
        "  고급 A4 복사용지 -> 복사용지\n\n"
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


def _merge_similar_groups_by_similarity(groups_summary, threshold=0.9):
    """
    1단계(공백정규화 기준 완전일치)로 이미 줄어든 그룹들을, 문자열 유사도
    (difflib, 표준라이브러리 — AI 아님)로 다시 검토해서 "표현만 다르고 실제로
    같은 규격"인 그룹끼리 병합함. 완전한 동의어(예: "고강도"↔"high-strength")
    까지는 못 잡지만, 어순차이·사소한 표현차이는 충분히 잡아내면서 AI 호출
    없이 처리됨.

    groups_summary: [{"group_key": str, "description": str}]
    반환: {group_key: 병합후_group_key} 형태의 매핑 (병합 안 되면 자기 자신에 매핑)
    """
    from difflib import SequenceMatcher

    keys = [g["group_key"] for g in groups_summary]
    descriptions = {g["group_key"]: g["description"] for g in groups_summary}
    mapping = {k: k for k in keys}  # 기본값: 병합 없음(자기 자신)

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            key_i, key_j = keys[i], keys[j]
            # 이미 같은 그룹으로 병합됐으면 스킵
            if mapping[key_i] == mapping[key_j]:
                continue

            desc_i, desc_j = descriptions[key_i], descriptions[key_j]
            if not desc_i or not desc_j:
                continue  # 설명 없는 건 유사도 비교 자체를 안 함 (오판 방지)

            similarity = SequenceMatcher(None, desc_i, desc_j).ratio()
            if similarity >= threshold:
                # key_j가 속한 그룹 전체를 key_i의 그룹으로 병합
                old_target = mapping[key_j]
                new_target = mapping[key_i]
                for k in keys:
                    if mapping[k] == old_target:
                        mapping[k] = new_target

    return mapping


def find_substitute_items(item_code: str, qty_needed, max_groups: int = 5) -> list:
    """
    특정 품목의 대체품 후보를 찾음. 3단계로 좁혀나감:

    1차(item_group): 같은 분류만 대상으로 함. item_group이 없으면
      이 단계는 건너뛰고 다음 단계로 진행 (없다고 탐색을 막지 않음).
    2차(item_name): AI로 뽑은 핵심단어가 포함된 것만.
    3차(description): 규격설명 유사도 0.9 이상인 것들끼리 하나로 묶어서
      재고를 합산함 (예: "안전모#1"~"안전모#133"이 사실 같은 규격이면,
      132줄이 아니라 1줄로 — 사람이 검토 가능한 수준으로).
      description이 없는 항목은 서로 묶지 않음(잘못 합쳐지는 것 방지).

    원본과 설명 유사도 0.8 이상인 것만 후보로 남기고, 그 안에서 유사도
    높은 순으로 상위 max_groups개까지만 반환. 0.8 이상이 하나도 없으면
    빈 리스트 반환.

    반환: [{"item_codes"(list), "item_name", "description", "total_qty",
            "fulfills_full_qty", "duplicate_count", "last_rate"}, ...]
    """
    item = erp_get_one("Item", item_code)
    if not item:
        return []

    item_group = item.get("item_group")
    item_name = item.get("item_name", item_code)
    base = _get_core_keyword(item_name)  # AI 호출은 여기 딱 한 번

    # ── 1차: item_group 필터링 ──
    if item_group:
        group_filtered = erp_get(
            "Item",
            filters=[["item_group", "=", item_group]],
            fields=["item_code", "item_name", "description"],
        )
    else:
        print(f"  '{item_code}'에 item_group이 없어 1차 필터링을 건너뜁니다.")
        group_filtered = erp_get(
            "Item",
            filters=[["item_name", "like", f"%{base}%"]],  # item_group 없으면 최소한 이름으로는 좁혀서 가져옴
            fields=["item_code", "item_name", "description"],
        )

    # ── 2차: item_name(핵심단어) 필터링 ──
    candidates = [c for c in (group_filtered or []) if base in c["item_name"]]

    matched = []

    for c in candidates or []:
        if c["item_code"] == item_code:
            continue  # 요청한 그 품목 자체는 제외
        if c["item_name"] == item_name:
            continue  # 완전히 이름이 똑같으면 대체품이 아니라 그냥 같은 물건
        matched.append(c)

    # description 기준으로 그룹화 — 같은 규격이면 하나로 묶음
    groups = {}
    for c in matched:
        desc = (c.get("description") or "").strip()
        # 공백·줄바꿈 차이는 그룹키 계산에서 무시 (표시용 description은 원본 그대로 둠)
        normalized_key = "".join(desc.split()) if desc else f"__no_desc__{c['item_code']}"
        groups.setdefault(normalized_key, {"description": desc, "items": []})
        groups[normalized_key]["items"].append(c)

    # 2단계: 문자열 유사도(AI 아님)로, 공백정규화로도 안 잡힌 어순·표현
    # 차이를 추가로 병합
    groups_summary = [
        {"group_key": k, "description": v["description"]} for k, v in groups.items()
    ]
    mapping = _merge_similar_groups_by_similarity(groups_summary)

    merged_groups = {}
    for group_key, group in groups.items():
        target_key = mapping.get(group_key, group_key)
        if target_key not in merged_groups:
            merged_groups[target_key] = {"description": group["description"], "items": []}
        merged_groups[target_key]["items"].extend(group["items"])
    groups = merged_groups

    results = []
    for group_key, group in groups.items():
        group_items = group["items"]
        total_qty = 0
        item_codes = []

        for gi in group_items:
            bins = erp_get(
                "Bin",
                filters=[["item_code", "=", gi["item_code"]], ["actual_qty", ">", 0]],
                fields=["actual_qty"],
            )
            group_qty = sum(b["actual_qty"] for b in (bins or []))
            if group_qty > 0:
                total_qty += group_qty
                item_codes.append(gi["item_code"])

        if not item_codes:
            continue  # 이 그룹 전체 재고 0이면 후보에서 제외

        # 원본 요청품목 description이랑 이 후보의 유사도 점수 (0~1)
        from difflib import SequenceMatcher
        original_desc = item.get("description") or ""
        candidate_desc = group["description"] or ""
        similarity = SequenceMatcher(None, original_desc, candidate_desc).ratio() if original_desc and candidate_desc else None

        results.append({
            "item_codes": item_codes,
            "item_name": group_items[0]["item_name"],
            "description": group["description"] or "(설명 없음)",
            "total_qty": total_qty,
            "fulfills_full_qty": total_qty >= qty_needed,
            "duplicate_count": len(item_codes),
            "last_rate": _get_last_purchase_rate(item_codes[0]),
            "similarity_to_original": similarity,
        })

    # 유사도 0.8 이상인 것만 남기고, 그 안에서 유사도 높은 순 상위 max_groups개
    # (0.8 미만이거나 애초에 비교 불가(설명없음)면 후보에서 제외 — 없으면 0개 반환)
    results = [
        r for r in results
        if r["similarity_to_original"] is not None and r["similarity_to_original"] >= 0.8
    ]
    results.sort(key=lambda r: r["similarity_to_original"], reverse=True)
    return results[:max_groups]


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
            fulfill_disp = "✅ 전량충족" if s["fulfills_full_qty"] else f"⚠️ 부분충족({s['total_qty']}개만 있음)"
            dup_note = f" (동일규격 {s['duplicate_count']}개 코드 재고 합산)" if s["duplicate_count"] > 1 else ""

            print(f"\n  ─────────────────────────────")
            print(f"  {s['item_name']}{dup_note}")
            print(f"  코드: {', '.join(s['item_codes'][:5])}" + (" 외..." if len(s['item_codes']) > 5 else ""))
            print(f"  합계재고: {s['total_qty']} | {fulfill_disp}")
            sim_disp = f"{s['similarity_to_original']:.2f}" if s["similarity_to_original"] is not None else "비교불가(설명없음)"
            print(f"  원본과 설명 유사도: {sim_disp}")
            print(f"  최근단가: {rate_disp}")
            print(f"  설명: {s['description']}")