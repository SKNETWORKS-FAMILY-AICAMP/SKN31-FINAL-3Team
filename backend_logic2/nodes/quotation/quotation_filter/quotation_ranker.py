"""검토 통과 견적을 규격 적합성, 총금액, 납기 순으로 정렬한다.

단독 실행 예:
    python quotation_ranker.py reviewed.json --rfq rfq_requirements.json \
        --top-k 3 --output ranked.json
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from typing import Any

try:
    from .quotation_models import (
        QuotationReview,
        RankedQuotation,
        RankingResult,
        RFQRequirements,
        ReviewStatus,
        dump_json,
        load_json,
    )
except ImportError:
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
        QuotationReview,
        RankedQuotation,
        RankingResult,
        RFQRequirements,
        ReviewStatus,
        dump_json,
        load_json,
    )


def _latest_delivery(review: QuotationReview) -> date | None:
    if not review.quotation:
        return None
    # 외부 견적 공통 모델에서는 quotation_date가 공급사의 납기일이다.
    if review.quotation.quotation_date:
        return review.quotation.quotation_date

    # 기존 ERP 포털 변환 데이터와의 하위 호환용 fallback.
    dates = []
    for item in review.quotation.items:
        delivery = item.delivery_date
        if delivery:
            dates.append(delivery)
    return max(dates) if dates else None


def _required_delivery(rfq: RFQRequirements) -> date | None:
    dates = [item.required_delivery_date for item in rfq.items if item.required_delivery_date]
    return max(dates) if dates else None


def rank_quotations(
    review_data: list[QuotationReview | dict[str, Any]],
    rfq_data: RFQRequirements | dict[str, Any],
    *,
    top_k: int = 3,
) -> RankingResult:
    """결정론적 규칙으로 정렬하며 top-k 경계의 동점은 모두 포함한다."""
    if top_k < 1:
        raise ValueError("top_k는 1 이상이어야 합니다.")
    rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    reviews = [row if isinstance(row, QuotationReview) else QuotationReview.model_validate(row) for row in review_data]

    candidates: list[tuple[tuple[Decimal, int, int], QuotationReview, date | None, int | None]] = []
    excluded: list[dict[str, Any]] = []
    required_delivery = _required_delivery(rfq)
    for review in reviews:
        if review.status != ReviewStatus.ACCEPTED or not review.valid or not review.specification_compliant or not review.quotation:
            excluded.append({
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value,
                "evidence": review.rejection_evidence or [issue.evidence for issue in review.issues],
            })
            continue
        quotation = review.quotation
        delivery = _latest_delivery(review)
        late_days = max(0, (delivery - required_delivery).days) if delivery and required_delivery else None
        # 규격 통과 견적만 남은 뒤 총액을 우선하고, 동액이면 납기를 비교한다.
        delivery_sort = delivery.toordinal() if delivery else 10**9
        late_sort = late_days if late_days is not None else 10**9
        key = (quotation.total_amount, late_sort, delivery_sort)
        candidates.append((key, review, delivery, late_days))

    candidates.sort(key=lambda row: (row[0], row[1].quotation_id))
    recommended: list[RankedQuotation] = []
    previous_key: tuple[Decimal, int, int] | None = None
    current_rank = 0
    for position, (key, review, delivery, late_days) in enumerate(candidates, 1):
        if key != previous_key:
            current_rank = position
            previous_key = key
        if current_rank > top_k:
            break
        quotation = review.quotation
        assert quotation is not None
        tied = sum(1 for other, *_ in candidates if other == key) > 1
        delivery_reason = f", 최종 납기 {delivery.isoformat()}" if delivery else ", 납기 미기재"
        late_reason = f" (RFQ 대비 {late_days}일 지연)" if late_days is not None else ""
        recommended.append(RankedQuotation(
            rank=current_rank,
            quotation_id=quotation.quotation_id,
            supplier_id=quotation.supplier_id,
            supplier_name=quotation.supplier_name,
            total_amount=quotation.total_amount,
            currency=quotation.currency,
            delivery_date=delivery,
            late_days=late_days,
            tied=tied,
            reason=f"규격·수량·산식 검토 통과, 총금액 {quotation.total_amount} {quotation.currency}{delivery_reason}{late_reason}",
        ))

    return RankingResult(
        rfq_name=rfq.rfq_name,
        requested_top_k=top_k,
        recommended=recommended,
        excluded=excluded,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="검토 통과 견적 우선순위 정렬")
    parser.add_argument("input", help="검토 결과 JSON 배열")
    parser.add_argument("--rfq", required=True, help="RFQ 요구사항 JSON")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    raw_reviews = load_json(args.input)
    if isinstance(raw_reviews, dict) and "reviews" in raw_reviews:
        raw_reviews = raw_reviews["reviews"]
    if not isinstance(raw_reviews, list):
        raw_reviews = [raw_reviews]
    result = rank_quotations(raw_reviews, load_json(args.rfq), top_k=args.top_k)
    rendered = dump_json(result, args.output)
    if args.output:
        print(f"정렬 완료: {args.output} ({len(result.recommended)}개 추천)")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
