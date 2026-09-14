"""검토 통과 견적을 규격 적합성, 총금액, 납기 순으로 정렬한다.

"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
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
    from .get_supplier_quotations import get_quotations_for_rfq, get_reviewable_quotations
    from .quotation_reviewer import load_rfq_requirements, match_requirement, review_quotation
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
    from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import (
        get_quotations_for_rfq,
        get_reviewable_quotations,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
        load_rfq_requirements,
        match_requirement,
        review_quotation,
    )

from backend_logic2.repositories.deliveries import get_supplier_latest_scorecards


LOGGER = logging.getLogger(__name__)


def _delivery_metrics(
    review: QuotationReview,
    rfq: RFQRequirements,
) -> tuple[date | None, int | None]:
    """전체 최종 납기와 품목별 요구일 대비 최대 지연일을 계산한다."""
    if not review.quotation:
        return None, None
    dates: list[date] = []
    item_late_days: list[int] = []
    for item in review.quotation.items:
        delivery = item.expected_delivery_date
        if (
            delivery is None
            and review.quotation.quotation_date
            and item.lead_time_days is not None
        ):
            delivery = review.quotation.quotation_date + timedelta(days=item.lead_time_days)
        if delivery:
            dates.append(delivery)
            required = match_requirement(item, rfq)
            if required and required.required_delivery_date:
                item_late_days.append(
                    (delivery - required.required_delivery_date).days
                )
    latest_delivery = max(dates) if dates else None
    late_days = max(0, max(item_late_days)) if item_late_days else None
    return latest_delivery, late_days


def rank_quotations(
    review_data: list[QuotationReview | dict[str, Any]],
    rfq_data: RFQRequirements | dict[str, Any],
    *,
    top_k: int = 3,
    supplier_scorecards: dict[str, dict[str, Any]] | None = None,
) -> RankingResult:
    """결정론적 규칙으로 정렬하며 top-k 경계의 동점은 모두 포함한다.

    가격과 납기가 모두 같은 후보에만 최근 공급사 평가를 최종 타이브레이커로
    사용한다. 같은 조건의 후보 중 평가가 없는 신규 업체가 하나라도 있으면
    과거 평가를 정렬에 사용하지 않아 신규 업체를 감점하지 않는다.
    """
    if top_k < 1:
        raise ValueError("top_k는 1 이상이어야 합니다.")
    rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    reviews = [row if isinstance(row, QuotationReview) else QuotationReview.model_validate(row) for row in review_data]

    supplier_scorecards = supplier_scorecards or {}
    rankable_reviews: list[QuotationReview] = []
    excluded: list[dict[str, Any]] = []
    for review in reviews:
        if not (
            review.status == ReviewStatus.ACCEPTED
            and review.valid
            and review.specification_compliant
            and review.quotation is not None
        ):
            excluded.append({
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value,
                "evidence": review.rejection_evidence or [issue.evidence for issue in review.issues],
            })
            continue
        rankable_reviews.append(review)

    currencies = {review.quotation.currency for review in rankable_reviews if review.quotation}
    use_base_amount = len(currencies) > 1
    if use_base_amount and any(
        review.quotation is None or review.quotation.base_total_amount is None
        for review in rankable_reviews
    ):
        raise ValueError(
            "서로 다른 통화의 견적을 비교하려면 모든 Supplier Quotation에 "
            "ERPNext base_grand_total 환산금액이 필요합니다."
        )
    candidates: list[tuple[tuple[Decimal, int, int], QuotationReview, date | None, int | None]] = []
    for review in rankable_reviews:
        quotation = review.quotation
        if quotation is None:
            raise ValueError(f"검토 통과 견적에 원본 데이터가 없습니다: {review.quotation_id}")
        delivery, late_days = _delivery_metrics(review, rfq)
        comparison_amount = (
            quotation.base_total_amount
            if use_base_amount
            else quotation.total_amount
        )
        if comparison_amount is None:
            raise ValueError(f"견적 비교금액이 없습니다: {review.quotation_id}")
        # 규격 통과 견적만 남은 뒤 총액을 우선하고, 동액이면 납기를 비교한다.
        delivery_sort = delivery.toordinal() if delivery else 10**9
        late_sort = late_days if late_days is not None else 10**9
        key = (comparison_amount, late_sort, delivery_sort)
        candidates.append((key, review, delivery, late_days))

    grouped: dict[tuple[Decimal, int, int], list[tuple[tuple[Decimal, int, int], QuotationReview, date | None, int | None]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate[0], []).append(candidate)

    candidates = []
    ranking_keys: dict[str, tuple[Any, ...]] = {}
    scorecard_tiebreaks: set[str] = set()
    for base_key in sorted(grouped):
        group = grouped[base_key]
        scored: list[tuple[Decimal, tuple[tuple[Decimal, int, int], QuotationReview, date | None, int | None]]] = []
        for candidate in group:
            quotation = candidate[1].quotation
            if quotation is None:
                raise ValueError(f"순위 후보에 원본 견적이 없습니다: {candidate[1].quotation_id}")
            card = supplier_scorecards.get(str(quotation.supplier_id or ""))
            score = card.get("weighted_score") if isinstance(card, dict) else None
            if score is None:
                scored = []
                break
            scored.append((Decimal(str(score)), candidate))
        if scored:
            scored.sort(key=lambda row: (-row[0], row[1][1].quotation_id))
            candidates.extend(candidate for _, candidate in scored)
            for score, candidate in scored:
                quotation_id = candidate[1].quotation_id
                ranking_keys[quotation_id] = (*base_key, -score)
                scorecard_tiebreaks.add(quotation_id)
        else:
            sorted_group = sorted(group, key=lambda row: row[1].quotation_id)
            candidates.extend(sorted_group)
            for candidate in sorted_group:
                ranking_keys[candidate[1].quotation_id] = base_key
    recommended: list[RankedQuotation] = []
    previous_key: tuple[Any, ...] | None = None
    current_rank = 0
    for position, (key, review, delivery, late_days) in enumerate(candidates, 1):
        ranking_key = ranking_keys[review.quotation_id]
        if ranking_key != previous_key:
            current_rank = position
            previous_key = ranking_key
        if current_rank > top_k:
            break
        quotation = review.quotation
        if quotation is None:
            raise ValueError(f"순위 후보에 원본 견적이 없습니다: {review.quotation_id}")
        tied = sum(
            1
            for _, other_review, *_ in candidates
            if ranking_keys[other_review.quotation_id] == ranking_key
        ) > 1
        delivery_reason = f", 최종 납기 {delivery.isoformat()}" if delivery else ", 납기 미기재"
        late_reason = f" (RFQ 대비 {late_days}일 지연)" if late_days is not None else ""
        scorecard = supplier_scorecards.get(str(quotation.supplier_id or ""))
        score = scorecard.get("weighted_score") if isinstance(scorecard, dict) else None
        score_reason = (
            f", 동일 가격·납기 내 공급사 평가 {score}/5 반영"
            if review.quotation_id in scorecard_tiebreaks
            else ""
        )
        comparison_reason = (
            f", ERP 회사 기준 환산금액 {key[0]}"
            if use_base_amount
            else ""
        )
        recommended.append(RankedQuotation(
            rank=current_rank,
            quotation_id=quotation.quotation_id,
            supplier_id=quotation.supplier_id,
            supplier_name=quotation.supplier_name,
            total_amount=quotation.total_amount,
            comparison_amount=key[0],
            currency=quotation.currency,
            expected_delivery_date=delivery,
            late_days=late_days,
            tied=tied,
            reason=f"규격·수량·산식 검토 통과, 총금액 {quotation.total_amount} {quotation.currency}{comparison_reason}{delivery_reason}{late_reason}{score_reason}",
        ))

    return RankingResult(
        rfq_name=rfq.rfq_name,
        requested_top_k=top_k,
        recommended=recommended,
        excluded=excluded,
    )


def _attach_supplier_scorecards(quotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최근 공급사 평가를 붙이되 저장소 장애나 신규 업체는 중립으로 둔다."""
    suppliers = [str(row.get("supplier") or "").strip() for row in quotations]
    try:
        scorecards = get_supplier_latest_scorecards(suppliers)
    except Exception as exc:
        LOGGER.warning("Supplier Scorecard 집계 실패: %s", exc)
        scorecards = {}
    return [
        {
            **quotation,
            "supplier_scorecard": scorecards.get(str(quotation.get("supplier") or "").strip()),
        }
        for quotation in quotations
    ]


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _first_non_zero(*values: Any) -> float:
    for value in values:
        number = _number(value)
        if number:
            return number
    return 0.0


def _enrich_ranking_with_prices(
    ranking: list[dict[str, Any]],
    quotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """검증된 순위 값을 보존하고 ERP 원문의 부가 정보만 결합한다."""
    by_name = {str(row.get("name") or "").strip(): row for row in quotations}
    enriched: list[dict[str, Any]] = []
    for ranked in ranking:
        quotation_id = str(
            ranked.get("name") or ranked.get("quotation_id") or ""
        ).strip()
        quotation = by_name.get(quotation_id)
        if quotation is None:
            LOGGER.warning(
                "검증된 순위 %s에 대응하는 ERP 원문을 찾지 못해 부가 정보 보강을 생략합니다.",
                quotation_id,
            )
            quotation = {}
        items = quotation.get("items") or []
        first_item = items[0] if items else {}
        total_amount = ranked.get("total_amount")
        if total_amount is None and quotation:
            total_amount = _first_non_zero(
                quotation.get("grand_total"),
                quotation.get("rounded_total"),
                quotation.get("net_total"),
                sum(_number(item.get("amount")) for item in items),
            )
        expected_delivery_date = ranked.get("expected_delivery_date")
        if expected_delivery_date is None and first_item:
            expected_delivery_date = (
                first_item.get("expected_delivery_date")
                or first_item.get("schedule_date")
                or first_item.get("delivery_date")
            )
        enriched.append({
            **ranked,
            "name": ranked.get("name") or ranked.get("quotation_id") or quotation.get("name"),
            "supplier": ranked.get("supplier") or quotation.get("supplier"),
            "currency": ranked.get("currency") or quotation.get("currency") or "KRW",
            "rate": (
                _first_non_zero(first_item.get("rate"), first_item.get("net_rate"))
                if first_item else None
            ),
            "amount": (
                _first_non_zero(first_item.get("amount"), first_item.get("net_amount"))
                if first_item else None
            ),
            "total_amount": total_amount,
            "grand_total": ranked.get("grand_total", total_amount),
            "expected_delivery_date": expected_delivery_date,
            "lead_time_days": first_item.get("lead_time_days") if first_item else None,
            "transaction_date": quotation.get("transaction_date") or ranked.get("transaction_date"),
            "supplier_scorecard": quotation.get("supplier_scorecard") or ranked.get("supplier_scorecard"),
        })
    return enriched


def evaluate_quotations(rfq_name: str, *, top_k: int = 3) -> dict[str, Any]:
    """ERP 견적을 reviewer로 검증하고 외부 AI 없이 결정론적으로 순위를 매긴다."""
    try:
        rfq = load_rfq_requirements(rfq_name)
    except Exception as exc:
        return {"error": f"RFQ를 찾거나 읽을 수 없습니다: {rfq_name} ({exc})"}

    quotations = _attach_supplier_scorecards(get_quotations_for_rfq(rfq_name))
    if not quotations:
        return {
            "requirements": rfq.model_dump(mode="json"),
            "quotations": [],
            "ranking": [],
            "message": "제출된 견적이 아직 없습니다.",
        }

    reviewable = get_reviewable_quotations(rfq_name)
    reviews = [review_quotation(quotation, rfq) for quotation in reviewable]
    scorecards = {
        str(row.get("supplier") or ""): row["supplier_scorecard"]
        for row in quotations
        if row.get("supplier_scorecard") is not None
    }
    try:
        result = rank_quotations(
            reviews,
            rfq,
            top_k=top_k,
            supplier_scorecards=scorecards,
        )
    except ValueError as exc:
        return {
            "requirements": rfq.model_dump(mode="json"),
            "quotations": quotations,
            "ranking": [],
            "error": str(exc),
        }
    reviews_by_id = {review.quotation_id: review for review in reviews}
    ranking: list[dict[str, Any]] = []
    for ranked in result.recommended:
        review = reviews_by_id[ranked.quotation_id]
        ranking.append({
            "name": ranked.quotation_id,
            "quotation_id": ranked.quotation_id,
            "supplier": ranked.supplier_id or ranked.supplier_name,
            "supplier_name": ranked.supplier_name,
            "rank": ranked.rank,
            "fulfills_qty": all(item.quantity_compliant for item in review.item_compliance),
            "spec_match": review.specification_compliant,
            "reason": ranked.reason,
            "issues": [issue.message for issue in review.issues],
            "currency": ranked.currency,
            "total_amount": ranked.total_amount,
            "grand_total": ranked.total_amount,
            "comparison_amount": ranked.comparison_amount,
            "expected_delivery_date": (
                ranked.expected_delivery_date.isoformat()
                if ranked.expected_delivery_date
                else None
            ),
            "late_days": ranked.late_days,
            "tied": ranked.tied,
        })
    return {
        "requirements": rfq.model_dump(mode="json"),
        "quotations": quotations,
        "ranking": _enrich_ranking_with_prices(ranking, quotations),
        "excluded": result.excluded,
    }


def print_evaluation(result: dict[str, Any]) -> None:
    """워크플로 로그에 외부 AI 없는 견적 평가 결과를 간결하게 출력한다."""
    if result.get("error") or result.get("message"):
        print(result.get("error") or result.get("message"))
        return
    print(f"\n=== 견적 평가 결과 ({len(result.get('quotations') or [])}건 제출됨) ===")
    for row in result.get("ranking") or []:
        print(f"#{row.get('rank')} {row.get('supplier_name') or row.get('supplier')}: {row.get('reason')}")
    for row in result.get("excluded") or []:
        print(f"제외 {row.get('supplier_name') or row.get('quotation_id')}: {row.get('evidence')}")


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
