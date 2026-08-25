"""
nodes/find_substitute_item.py — 대체품 추천 모듈 (전면 재설계)

find_duplicate_stock.py랑 다른 목적:
  - find_duplicate_stock: "이름만 다른 같은 물건" 찾기 (예: 장갑 == 장갑#103)
  - 이 모듈: "진짜 다른 물건인데, 변형(색상·규격 등)만 다르고 용도가 같아서
    대신 쓸 수 있는 것" 찾기

⚠️ 예전 버전(description 완전일치/유사도 비교로 그룹화)은 폐기함 — 실제
   데이터를 보니 모든 품목의 description이 다 고유해서(같은 물건도 치수·
   중량 등이 조금씩 달라 완전일치가 안 됨) 그 접근 자체가 전제부터 틀렸음.
   문자열 유사도로 "AI가 이해하는 것"을 흉내내려다 계속 오판(스펙 좋은 게
   오히려 유사도 낮게 나오는 등)이 나서, 흉내내지 않고 AI한테 후보 전체를
   통째로 보여주고 판단을 맡기는 방식으로 바꿈.

새 흐름:
  ① item_group + 핵심단어(AI 1번)로 후보를 넓게 가져옴
  ② 재고 있는 것만 추림
  ③ AI 1번 호출로 후보 전체(이름+설명+재고)를 보여주고, 실제로 대체
     가능한 것만 순위+이유와 함께 골라달라고 함
  → AI 호출은 총 2번으로 고정 (후보 개수와 무관)

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/find_substitute_item.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from erp_client import erp_get, erp_get_one


def _strip_html(text: str) -> str:
    """description의 HTML 태그를 제거해서 순수 텍스트만 남김"""
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    return plain.strip()


def _get_core_keyword(item_name: str) -> str:
    """
    AI로 품목명에서 핵심 단어(기본 품목명)만 뽑아냄. 회사마다 표기방식이
    다 달라서(괄호·#·등급수식어 등, 위치도 앞/뒤 다양함) 정규식만으로는
    edge case를 다 못 잡아 AI를 씀. 품목당 딱 1번만 호출.
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
        "예시:\n"
        "  안전모(백색) -> 안전모\n"
        "  안전모##1001 -> 안전모\n"
        "  스탠다드 사무용 의자 -> 의자\n"
        "  프리미엄 메쉬 의자 -> 의자\n\n"
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


def _ai_rank_substitutes(item_name, item_description, qty_needed, candidates, max_results=5):
    """
    후보 전체를 AI한테 한 번에 보여주고, 실제로 대체 가능한 것만 골라서
    순위+이유를 매기게 함. 관련성 판단 + 순위 + 이유를 이 한 번의 호출로
    다 처리함 (예전처럼 유사도 계산 → 임계값 필터 → 별도 최종검증, 이렇게
    여러 단계로 흉내내지 않음).

    candidates: [{"item_code", "item_name", "description", "total_qty"}, ...]
    반환: [{"item_code", "rank", "reason"}, ...] (AI가 부적합하다고 판단한 건 아예 목록에서 뺌)
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if not candidates:
        return []

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    candidates_json = [
        {
            "item_code": c["item_code"],
            "item_name": c["item_name"],
            "description": _strip_html(c.get("description") or ""),
            "재고": c["total_qty"],
        }
        for c in candidates
    ]

    prompt = PromptTemplate.from_template(
        "요청품목: {item_name}\n"
        "요청품목 설명: {item_description}\n"
        "요청수량: {qty_needed}\n\n"
        "아래 후보 품목들 중에서, 실제로 요청품목을 대신 쓸 수 있는 것들만 "
        "골라서 적합도 순으로 순위를 매겨주세요 (최대 {max_results}개).\n\n"
        "후보 목록:\n{candidates}\n\n"
        "규칙:\n"
        "- 용도·사용대상이 명확히 다른 물건(예: 화이트보드용 vs 연필용 지우개)은 "
        "이름이 비슷해도 제외하세요.\n"
        "- 스펙이 원본보다 낮아도(다운그레이드) 용도가 같으면 후보에 포함하되, "
        "reason에 그 사실을 명시하세요.\n"
        "- 재고가 요청수량보다 적으면 reason에 '부분충족(N개)'이라고 언급하세요.\n"
        "- 적합한 후보가 하나도 없으면 빈 리스트를 반환하세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"ranking": [{{"item_code": "...", "rank": 1, "reason": "짧은 이유"}}]}}'
    )

    result = (prompt | llm).invoke({
        "item_name": item_name,
        "item_description": _strip_html(item_description),
        "qty_needed": qty_needed,
        "candidates": json.dumps(candidates_json, ensure_ascii=False, indent=2),
        "max_results": max_results,
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned).get("ranking", [])
    except Exception as e:
        print(f"[_ai_rank_substitutes] AI 응답 파싱 실패: {e}")
        return []


def find_substitute_items(item_code: str, qty_needed, max_results: int = 5) -> list:
    """
    특정 품목의 대체품 후보를 찾음.

    ① item_group(있으면) + 핵심단어로 후보를 넓게 가져옴
    ② 재고 있는 것만 추림
    ③ AI 1번 호출로 실제 대체가능한 것만 순위+이유 매겨서 반환

    반환: [{"item_code", "item_name", "description", "total_qty",
            "fulfills_full_qty", "rank", "reason"}, ...]
    """
    item = erp_get_one("Item", item_code)
    if not item:
        return []

    item_group = item.get("item_group")
    item_name = item.get("item_name", item_code)
    item_description = item.get("description") or ""
    base = _get_core_keyword(item_name)  # AI 호출 1번째

    print(f"\n[DEBUG] 요청품목: {item_name} | item_group: {item_group} | 핵심단어: '{base}'")

    # ① 후보 가져오기
    filters = [["item_name", "like", f"%{base}%"]]
    if item_group:
        filters.append(["item_group", "=", item_group])
    else:
        print(f"  '{item_code}'에 item_group이 없어 이름 기준으로만 탐색합니다.")

    raw_candidates = erp_get(
        "Item",
        filters=filters,
        fields=["item_code", "item_name", "description"],
    )
    raw_candidates = [c for c in (raw_candidates or []) if c["item_code"] != item_code]
    print(f"[DEBUG] 후보 조회: {len(raw_candidates)}건")

    # ② 재고 있는 것만 추림
    stocked_candidates = []
    for c in raw_candidates:
        bins = erp_get(
            "Bin",
            filters=[["item_code", "=", c["item_code"]], ["actual_qty", ">", 0]],
            fields=["actual_qty"],
        )
        total_qty = sum(b["actual_qty"] for b in (bins or []))
        if total_qty > qty_needed:
            stocked_candidates.append({**c, "total_qty": total_qty})
    print(f"[DEBUG] 재고가 요청량보다 많은 후보만 추린 후: {len(stocked_candidates)}건")

    if not stocked_candidates:
        return []

    # ③ AI 최종 판단 (호출 2번째)
    ranking = _ai_rank_substitutes(item_name, item_description, qty_needed, stocked_candidates, max_results)
    print(f"[DEBUG] AI가 최종 선정한 대체품: {len(ranking)}건")

    candidates_by_code = {c["item_code"]: c for c in stocked_candidates}
    results = []
    for r in ranking:
        code = r.get("item_code")
        c = candidates_by_code.get(code)
        if not c:
            continue
        results.append({
            "item_code": code,
            "item_name": c["item_name"],
            "description": c.get("description"),
            "total_qty": c["total_qty"],
            "fulfills_full_qty": c["total_qty"] >= qty_needed,
            "rank": r.get("rank"),
            "reason": r.get("reason"),
            "last_rate": _get_last_purchase_rate(code),
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
        print(f"  요청수량: {info['qty_needed']}")
        print(f"{'='*50}")

        if not info["substitutes"]:
            print("  대체품 후보 없음")

        for s in sorted(info["substitutes"], key=lambda x: x.get("rank") or 99):
            rate_disp = f"{s['last_rate']:,.0f}원" if s["last_rate"] is not None else "구매이력 없음"
            fulfill_disp = "✅ 전량충족" if s["fulfills_full_qty"] else f"⚠️ 부분충족({s['total_qty']}개만 있음)"

            print(f"\n  ─────────────────────────────")
            print(f"  #{s['rank']} {s['item_name']} ({s['item_code']})")
            print(f"  재고: {s['total_qty']} | {fulfill_disp}")
            print(f"  최근단가: {rate_disp}")
            print(f"  AI 판단 이유: {s['reason']}")