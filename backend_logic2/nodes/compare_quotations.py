"""
compare_quotations.py — 견적 비교분석

입력: RFQ 이름 (내부에서 7번 get_supplier_quotations() 재사용해서 견적 목록 가져옴)
하는 일: 도착한 견적들을 "가격이 낮고 납기를 빨리 맞춰줄 수 있는" 기준으로 비교,
        AI(OpenAI)로 순위 + 추천 이유 생성
출력: 순위표 + 추천 이유를 터미널에 출력

⚠️ 여기는 "추천"까지만 함. 최종 공급사 선택은 사람이 하는 거라 여기서
   자동으로 확정(Submit 등)하지 않음 — 순위/추천 이유를 보여주기만 함.
⚠️ 이 파일도 독립적으로 실행 가능. 의존하는 건 erp_client.py(간접) +
   get_supplier_quotations.py(같은 폴더에 있어야 함) 뿐.
"""

import json
import os
import sys

from openai import OpenAI

from get_supplier_quotations import get_supplier_quotations, ERPNextAPIError

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")


def summarize_by_supplier(quotations):
    """
    품목 단위로 펼쳐진 견적 리스트(get_supplier_quotations의 출력)를
    공급사 단위로 묶어서, 비교에 필요한 핵심 지표만 뽑아냄.

    - total_amount: 이 공급사가 제시한 전체 견적 금액 합계
    - avg_rate: 품목별 단가 평균 (참고용)
    - max_lead_time_days: 품목 중 가장 늦은 납기 (전체 납품 완료 기준이므로
      "빠른 납기"를 볼 때는 최댓값으로 보는 게 맞음 — 한 품목이라도 늦으면
      전체 납품이 늦어지니까)
    - item_count: 견적 낸 품목 수 (전체 요청 품목을 다 커버했는지 참고용)
    """
    by_supplier = {}
    for q in quotations:
        key = q["supplier"]
        if key not in by_supplier:
            by_supplier[key] = {
                "supplier": q["supplier"],
                "supplier_name": q["supplier_name"],
                "quotation_name": q["quotation_name"],
                "status": q["status"],
                "amounts": [],
                "rates": [],
                "lead_times": [],
                "item_count": 0,
            }

        entry = by_supplier[key]
        entry["item_count"] += 1
        if q["amount"] is not None:
            entry["amounts"].append(q["amount"])
        if q["rate"] is not None:
            entry["rates"].append(q["rate"])
        if q["lead_time_days"] is not None:
            entry["lead_times"].append(q["lead_time_days"])

    summary = []
    for entry in by_supplier.values():
        total_amount = sum(entry["amounts"]) if entry["amounts"] else None
        avg_rate = sum(entry["rates"]) / len(entry["rates"]) if entry["rates"] else None
        max_lead_time = max(entry["lead_times"]) if entry["lead_times"] else None

        summary.append({
            "supplier": entry["supplier"],
            "supplier_name": entry["supplier_name"],
            "quotation_name": entry["quotation_name"],
            "status": entry["status"],
            "total_amount": total_amount,
            "avg_rate": avg_rate,
            "max_lead_time_days": max_lead_time,
            "item_count": entry["item_count"],
        })

    return summary


def rank_with_ai(rfq_name, supplier_summary):
    """
    OpenAI를 이용해 "가격이 낮을수록, 납기가 빠를수록" 유리한 기준으로
    공급사 순위를 매기고, 순위별 추천 이유를 생성.

    - 순위 산정 기준은 프롬프트에 명시적으로 고정(가격 낮음 + 납기 빠름
      우선)해서 AI가 임의 기준으로 흔들리지 않게 함.
    - 반환은 최종 확정이 아니라 "추천"이라는 점을 결과 dict에도 명시.
    """
    client = OpenAI()

    system_prompt = (
        "너는 구매 담당자를 돕는 견적 비교 어시스턴트다. "
        "주어진 공급사별 견적 요약을 보고, 가격이 낮을수록 그리고 납기가 "
        "빠를수록(max_lead_time_days가 작을수록) 유리하다는 기준으로 순위를 "
        "매겨라. 가격과 납기 중 하나가 크게 나쁘면 순위를 낮춰라. "
        "너의 결과는 참고용 '추천'일 뿐이고 최종 선택은 사람이 한다는 점을 "
        "명심하고, 확정적인 어투(반드시 이걸로 하세요 등) 대신 추천 어투를 써라. "
        "반드시 JSON으로만 응답하고, 다른 텍스트는 절대 포함하지 마라."
    )

    user_prompt = (
        f"RFQ: {rfq_name}\n"
        f"공급사별 견적 요약:\n{json.dumps(supplier_summary, ensure_ascii=False, indent=2)}\n\n"
        "아래 JSON 형식으로만 응답해라:\n"
        "{\n"
        '  "ranking": [\n'
        "    {\n"
        '      "rank": 1,\n'
        '      "supplier": "<supplier id>",\n'
        '      "supplier_name": "<공급사명>",\n'
        '      "reason": "<가격/납기 기준으로 이 순위인 이유, 1~2문장>"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    res = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    return json.loads(res.choices[0].message.content)


def print_ranking(rfq_name, supplier_summary, ranking_result):
    """순위표 + 추천 이유를 터미널에 출력. 확정이 아니라 추천이라는 걸 명시."""
    print(f"\n=== RFQ '{rfq_name}' 견적 비교분석 (가격 낮음 + 납기 빠름 기준) ===")

    if not supplier_summary:
        print("(비교할 견적이 없습니다)\n")
        return

    by_supplier_id = {s["supplier"]: s for s in supplier_summary}

    header = f"{'순위':<4} {'공급사':<16} {'총 견적금액':>14} {'최장 납기(일)':>12} {'품목수':>6}"
    print(header)
    print("-" * len(header))

    for r in ranking_result.get("ranking", []):
        s = by_supplier_id.get(r["supplier"], {})
        amount_disp = f"{s.get('total_amount'):,.0f}" if s.get("total_amount") is not None else "-"
        lead_disp = s.get("max_lead_time_days") if s.get("max_lead_time_days") is not None else "-"
        item_count = s.get("item_count", "-")

        print(
            f"{r['rank']:<4} {r.get('supplier_name') or r['supplier']:<16} "
            f"{amount_disp:>14} {lead_disp:>12} {item_count:>6}"
        )
        print(f"     → {r['reason']}")

    print("-" * len(header))
    print("※ 위 순위는 AI 추천이며, 최종 공급사 선택은 담당자가 직접 확정해야 합니다.\n")


def main():
    if len(sys.argv) < 2:
        print("사용법: python compare_quotations.py <RFQ_이름>")
        print("예:     python compare_quotations.py PUR-RFQ-2026-00270")
        sys.exit(1)

    rfq_name = sys.argv[1]

    try:
        quotations = get_supplier_quotations(rfq_name)
    except ERPNextAPIError as e:
        print(f"[에러] ERPNext API 호출 실패: {e}")
        sys.exit(1)

    if not quotations:
        print(f"[compare_quotations] '{rfq_name}'에 대한 견적이 아직 없어서 비교할 수 없습니다.")
        return

    supplier_summary = summarize_by_supplier(quotations)
    ranking_result = rank_with_ai(rfq_name, supplier_summary)
    print_ranking(rfq_name, supplier_summary, ranking_result)


if __name__ == "__main__":
    main()