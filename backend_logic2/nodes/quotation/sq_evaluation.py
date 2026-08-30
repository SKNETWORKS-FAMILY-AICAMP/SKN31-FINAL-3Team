"""
nodes/sq_evaluation.py - RFQ에 대해 포털로 들어온 Supplier Quotation을
읽어서 OpenAI로 간단히 분석 + 순위매김.

주의: 이번 버전은 "포털 견적만" 다룬다는 전제로 단순화함:
   - 외부 이미지/파일 업로드 견적(quotation_filter의 로컬 비전모델 경로)은
     다루지 않음 - 그건 기존 quotation_filter 파이프라인이 별도로 담당
   - 여기서는 순수하게 ERPNext에 이미 Supplier Quotation DocType으로
     존재하는 것만 읽음(포털에서 들어온 순간 이미 구조화된 데이터라
     OCR/추출이 필요 없음)

확인 필요: docstatus 필터를 0(Draft)으로 두었음 - 포털 제출 견적이
   실제로 Draft 상태로 남는지, 아니면 Submit(1)까지 되는지 ERPNext
   실제 동작 확인 후 필요하면 조정.

흐름:
  1. get_rfq_requirements: RFQ 원본 요청내용(품목,수량,희망납기,설명) 조회
  2. get_quotations_for_rfq: 이 RFQ에 달린 Supplier Quotation 전부 조회
  3. AI 1번 호출: 전체 견적을 한 번에 보여주고 규격충족여부+순위+이유 판단
     ⚠️ item_code/수량뿐 아니라 description(사양 텍스트)도 명시적으로
     대조하게 프롬프트에 지시함 - item_code가 같아 보여도 실제 사양이
     다를 수 있고, 반대로 item_code가 없어도 description으로 같은
     품목임을 확인할 수 있는 경우가 있어서.
"""


import json
import argparse
from backend_logic2.integrations.erp_client import erp_get, erp_get_one

# 확인 필요: Supplier Quotation Item에서 RFQ를 연결하는 실제 필드명.
REQUEST_FOR_QUOTATION_LINK_FIELD = "request_for_quotation"


def get_rfq_requirements(rfq_name: str) -> dict:
    """RFQ 원본 요청내용(품목/수량/희망납기/설명)을 조회"""
    rfq = erp_get_one("Request for Quotation", rfq_name)
    if not rfq:
        return {}
    return {
        "rfq_name": rfq_name,
        "schedule_date": rfq.get("schedule_date"),
        "items": [
            {
                "rfq_item_id": item.get("name"),
                "item_code": item.get("item_code"),
                "item_name": item.get("item_name"),
                "description": item.get("description"),
                "qty": item.get("qty"),
                "uom": item.get("uom"),
                "schedule_date": item.get("schedule_date"),
            }
            for item in rfq.get("items", [])
        ],
    }


def get_quotations_for_rfq(rfq_name: str) -> list:
    """
    이 RFQ에 대해 제출된 Supplier Quotation 전부 조회.
    반환: [{"name":..., "supplier":..., "items": [...], "grand_total":...}, ...]
    """
    print(f"[DEBUG] 찾는 RFQ: {rfq_name}")
    rows = erp_get(
        "Supplier Quotation",
        filters=[
            ["Supplier Quotation Item", REQUEST_FOR_QUOTATION_LINK_FIELD, "=", rfq_name],
            ["docstatus", "=", 0],
        ],
        fields=["name"],
    )
    if not rows:
        return []

    quotations = []
    for row in rows:
        doc = erp_get_one("Supplier Quotation", row["name"])
        if not doc:
            continue
        quotations.append({
            "name": doc.get("name"),
            "supplier": doc.get("supplier"),

            "transaction_date": doc.get("transaction_date"),
            "valid_till": doc.get("valid_till"),

            "currency": doc.get("currency"),
            "conversion_rate": doc.get("conversion_rate"),
            "grand_total": doc.get("grand_total"),
            "base_grand_total": doc.get("base_grand_total"),

            "items": [
                {
                    "name": item.get("name"),

                    "request_for_quotation_item":
                        item.get("request_for_quotation_item"),

                    "item_code": item.get("item_code"),
                    "item_name": item.get("item_name"),
                    "description": item.get("description"),

                    "qty": item.get("qty"),
                    "uom": item.get("uom"),

                    "rate": item.get("rate"),
                    "amount": item.get("amount"),
                    "base_rate": item.get("base_rate"),
                    "base_amount": item.get("base_amount"),

                    "expected_delivery_date":
                        item.get("expected_delivery_date"),

                    "lead_time_days":
                        item.get("lead_time_days"),
                }
                for item in doc.get("items", [])
            ],
        })
    return quotations


def _ai_rank_quotations(requirements: dict, quotations: list) -> list:
    """
    전체 견적을 AI한테 한 번에 보여주고, 요청내용 대비 규격/수량 충족여부와
    함께 가격+납기 기준으로 순위를 매기게 함.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if len(quotations) < 2:
        return []

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    prompt = PromptTemplate.from_template(
        "다음은 RFQ 요청내용과, 여기에 대해 여러 공급사가 제출한 견적입니다.\n\n"
        "[RFQ 요청내용]\n{requirements}\n\n"
        "[제출된 견적들]\n{quotations}\n\n"
        "각 견적을 요청내용과 대조해서 판단하세요:\n\n"
        "1. 품목 매칭 및 규격 비교 (가장 중요):\n"
        "   - item_code가 일치하는지 확인하되, item_code만으로 판단하지 "
        "마세요. 각 요청 품목의 description(사양)과 견적 품목의 "
        "description을 실제 내용으로 대조하세요.\n"
        "   - item_code가 같아도 description에 나온 재질,규격,등급,"
        "치수 등이 요청과 다르면 '규격 불일치'로 issues에 명시하세요.\n"
        "   - item_code가 비어있거나 다르더라도, description 내용이 "
        "요청 품목과 실질적으로 동일한 것을 가리키면 매칭된 것으로 "
        "간주하고 그 사실을 reason에 명시하세요.\n"
        "   - 요청보다 낮은 사양(다운그레이드)이면 issues에 구체적으로 "
        "어떤 사양이 부족한지 적으세요.\n\n"
        "2. 수량 충족: 요청수량보다 적게 제출한 견적은 '부분충족'으로 "
        "명시하되 순위에는 포함하세요 (완전배제는 하지 마세요, 담당자가 "
        "판단할 수 있게).\n\n"
        "3. 순위: 규격/수량을 충족하는 것들만 가격(낮을수록 좋음)과 "
        "납기(빠를수록 좋음)를 기준으로 순위를 매기세요. 규격이 명확히 "
        "다르거나 요청 품목 자체가 빠진 견적은 순위 최하위로 두고 "
        "issues에 사유를 남기세요.\n\n"
        "reason은 한두 문장으로 짧게, 왜 이 순위인지(규격 일치여부 포함)를 "
        "포함하세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"ranking": [{{"name": "견적문서명", '
        '"supplier": "공급사명", "rank": 1, "fulfills_qty": true, '
        '"spec_match": true, "reason": "짧은 이유", "issues": []}}]}}'
    )

    result = (prompt | llm).invoke({
        "requirements": json.dumps(requirements, ensure_ascii=False, indent=2, default=str),
        "quotations": json.dumps(quotations, ensure_ascii=False, indent=2, default=str),
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned).get("ranking", [])
    except Exception as e:
        print(f"[_ai_rank_quotations] AI 응답 파싱 실패: {e}")
        return []


def evaluate_quotations(rfq_name: str) -> dict:
    """
    메인 진입점. RFQ 요구사항 조회 -> 제출된 SQ 전부 조회 -> AI로 순위매김.
    반환: {"requirements":..., "quotations":..., "ranking": [...]}
    """
    requirements = get_rfq_requirements(rfq_name)
    if not requirements:
        return {"error": f"RFQ를 찾을 수 없습니다: {rfq_name}"}

    quotations = get_quotations_for_rfq(rfq_name)

    if not quotations:
        return {
            "requirements": requirements,
            "quotations": [],
            "ranking": [],
            "message": "제출된 견적이 아직 없습니다.",
        }

    print(
        f"[견적 수집] '{rfq_name}'에 대해 {len(quotations)}건 견적 수집됨: "
        f"{[q['supplier'] for q in quotations]}"
    )

    # 견적이 1건뿐이면 비교 대상이 없으므로 AI 호출 생략
    if len(quotations) == 1:
        quotation = quotations[0]

        print(
            f"[견적 비교 생략] 제출된 견적이 1건뿐이므로 "
            f"AI 비교분석을 수행하지 않습니다."
        )

        ranking = [
            {
                "name": quotation.get("name"),
                "supplier": quotation.get("supplier"),
                "rank": 1,

                # AI 평가를 하지 않았으므로 임의로 True/False 판단하지 않음
                "fulfills_qty": None,
                "spec_match": None,

                "reason": "제출된 견적이 1건뿐이므로 비교평가를 생략했습니다.",
                "issues": [],
            }
        ]

        return {
            "requirements": requirements,
            "quotations": quotations,
            "ranking": ranking,
        }

    # 2건 이상일 때만 AI 비교평가
    print(f"[AI 견적 비교] {len(quotations)}건 비교분석 시작")

    ranking = _ai_rank_quotations(
        requirements,
        quotations,
    )

    return {
        "requirements": requirements,
        "quotations": quotations,
        "ranking": ranking,
    }


def print_evaluation(result: dict) -> None:
    """평가결과를 사용자에게 보여주기 좋은 형태로 출력"""
    if result.get("error"):
        print(f"오류: {result['error']}")
        return
    if result.get("message"):
        print(result["message"])
        return

    print(f"\n{'=' * 50}")
    print(f"견적 평가 결과 ({len(result['quotations'])}건 제출됨)")
    print(f"{'=' * 50}")

    ranking = sorted(result["ranking"], key=lambda r: r.get("rank") or 999)
    for r in ranking:
        fulfills_qty = r.get("fulfills_qty")
        spec_match = r.get("spec_match")

        if fulfills_qty is True:
            fulfill = "전량충족"
        elif fulfills_qty is False:
            fulfill = "부분충족/미흡"
        else:
            fulfill = "수량비교 생략"

        if spec_match is True:
            spec = "규격일치"
        elif spec_match is False:
            spec = "규격확인필요"
        else:
            spec = "규격비교 생략"
        print(f"\n#{r.get('rank')} {r.get('supplier')} ({r.get('name')})")
        print(f"  {fulfill} | {spec}")
        print(f"  이유: {r.get('reason')}")
        if r.get("issues"):
            print(f"  주의사항: {r.get('issues')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RFQ에 대한 Supplier Quotation 평가")
    parser.add_argument("--rfq", required=True, help="Request for Quotation 이름")
    args = parser.parse_args()

    result = evaluate_quotations(args.rfq)
    print_evaluation(result)